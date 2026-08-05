#!/usr/bin/env python3
"""HTTP bridge owning one long-lived Claude Code process.

Runs on the machine whose Claude Code you want to drive. Speaks the
`--input-format stream-json` protocol on stdin/stdout of a child `claude`
process and exposes it over HTTP.

Conversation state lives in the child, so successive prompts continue one
conversation rather than starting fresh each time.

Requires Python 3.9+ and the `claude` CLI on PATH. No third-party packages.

    export CLAUDE_BRIDGE_TOKEN=<long-random-string>
    python3 claude_bridge.py

Environment:
    CLAUDE_BRIDGE_TOKEN    shared secret for the Authorization header
    CLAUDE_BRIDGE_TOKEN_FILE  path to a file holding it; preferred over the var
    CLAUDE_BRIDGE_HOST     bind address (default 127.0.0.1; set to the tailnet IP to expose)
    CLAUDE_BRIDGE_PORT     bind port (default 8787)
    CLAUDE_BRIDGE_NAME     label reported by /health (default: hostname)
    CLAUDE_BRIDGE_CWD      working directory for the child (default: this process's cwd)
    CLAUDE_BRIDGE_MODEL    --model value (default: the CLI's own default)
    CLAUDE_BRIDGE_PERMISSION_MODE   --permission-mode (default acceptEdits)
    CLAUDE_BRIDGE_ARGS     extra args for the child, shell-quoted
    CLAUDE_BRIDGE_CLAUDE_BIN  full path to `claude` (for a service PATH that omits it)
    CLAUDE_BRIDGE_LOG      write a dated log here (a service discards stdout)
    CLAUDE_BRIDGE_LOG_MAX_BYTES  rotate to .1 past this size (default 5MB)
"""

from __future__ import annotations

import hmac
import json
import os
import platform
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))

from relay_config import write_inbound_chain  # noqa: E402

VERSION = "0.7.0"


def _load_token() -> str:
    """Prefer a token file, so the secret is not in the environment block.

    An env var is inherited by every child this bridge spawns — including the
    `claude` process and everything it shells out to. A path is not a secret;
    the file it points at can be ACL'd to one account.
    """
    path = os.environ.get("CLAUDE_BRIDGE_TOKEN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get("CLAUDE_BRIDGE_TOKEN", "")


TOKEN = _load_token()
HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8787"))
NAME = os.environ.get("CLAUDE_BRIDGE_NAME") or socket.gethostname()
CHILD_CWD = os.environ.get("CLAUDE_BRIDGE_CWD") or os.getcwd()
MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "")
PERMISSION_MODE = os.environ.get("CLAUDE_BRIDGE_PERMISSION_MODE", "acceptEdits")
EXTRA_ARGS = shlex.split(os.environ.get("CLAUDE_BRIDGE_ARGS", ""))
STREAM = os.environ.get("CLAUDE_BRIDGE_STREAM", "1") not in ("0", "false", "no")
# A service manager usually discards stdout, and for an always-on bridge the log
# is the only forensics there is. Writing it ourselves avoids needing a shell
# wrapper just to redirect.
LOG_FILE = os.environ.get("CLAUDE_BRIDGE_LOG")
LOG_MAX_BYTES = int(os.environ.get("CLAUDE_BRIDGE_LOG_MAX_BYTES", "5000000"))
_LOG_LOCK = threading.Lock()

BRIDGE_STARTED_AT = time.time()


def session_state_path() -> Path:
    """Where this bridge remembers its conversation across process restarts.

    Keyed by port so several bridges on one machine do not share a session.
    """
    explicit = os.environ.get("CLAUDE_BRIDGE_SESSION_FILE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "claude-remote-relay" / f"session-{PORT}.json"


def read_saved_session() -> str | None:
    path = session_state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # A session belongs to the directory it ran in; resuming it elsewhere would
    # hand the peer a conversation about a different project.
    if data.get("cwd") != CHILD_CWD:
        return None
    session_id = data.get("session_id")
    return str(session_id) if session_id else None


def write_saved_session(session_id: str | None) -> None:
    path = session_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if session_id:
        path.write_text(json.dumps({"session_id": session_id, "cwd": CHILD_CWD,
                                    "written_at": time.time()}), encoding="utf-8")
    elif path.exists():
        path.unlink()


DEFAULT_TURN_TIMEOUT = 900.0
EVENT_HISTORY = 4000
JOB_HISTORY = 200
SUBSCRIBER_BACKLOG = 2000  # per-watcher; a stalled watcher drops rather than stalls


class BridgeError(Exception):
    """An error with a machine-readable code, mirrored in the README's table."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def log(msg: str) -> None:
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
    if not LOG_FILE:
        return
    # Dated in the file: a service log outlives the day its lines were written.
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    with _LOG_LOCK:
        path = Path(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size + len(line) > LOG_MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def claude_executable() -> str:
    """Locate `claude`, tolerating the stripped PATH a service process gets.

    Services and non-interactive shells do not source a login profile, so the
    per-user install directory is usually missing from PATH even though the
    binary is right there. Failing here looks like a bridge bug: the process
    starts, then cannot spawn a child.
    """
    explicit = os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN")
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"CLAUDE_BRIDGE_CLAUDE_BIN points at nothing: {explicit}")
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    fallbacks = [home / ".local/bin/claude", Path("/usr/local/bin/claude"),
                 home / ".local/bin/claude.exe",
                 home / "AppData/Local/Programs/claude/claude.exe"]
    for candidate in fallbacks:
        if candidate.is_file():
            log(f"claude not on PATH; using {candidate}")
            return str(candidate)
    raise SystemExit(
        "`claude` not found on PATH and not in the usual install locations. "
        "Set CLAUDE_BRIDGE_CLAUDE_BIN to its full path — a service PATH often "
        "omits per-user directories such as ~/.local/bin."
    )


def child_argv() -> list[str]:
    exe = claude_executable()
    argv = [
        exe,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", PERMISSION_MODE,
    ]
    if STREAM:
        # Token-level deltas, so a watcher can see text as it is produced.
        argv.append("--include-partial-messages")
    if MODEL:
        argv += ["--model", MODEL]
    argv += EXTRA_ARGS
    # CreateProcess cannot execute .cmd/.bat shims directly, which is how npm
    # installs `claude` on Windows. Route those through the command processor.
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c"] + argv
    return argv


class Job:
    """One submitted turn. Runs on a worker thread so callers may poll instead of block."""

    def __init__(self, prompt: str, timeout: float,
                 chain: list[str] | None = None) -> None:
        self.id = f"job-{uuid.uuid4().hex[:12]}"
        self.prompt = prompt
        self.timeout = timeout
        # Machines that already relayed this request, so the session we are
        # about to run can refuse to bounce it back to one of them.
        self.chain = chain or []
        self.status = "queued"          # queued | running | done | error
        self.result: dict | None = None
        self.error: dict | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.done = threading.Event()

    def snapshot(self, include_result: bool = True) -> dict:
        data = {
            "job_id": self.id,
            "status": self.status,
            "prompt": self.prompt[:200],
            "created_at": self.created_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.created_at, 1),
        }
        if include_result and self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data


class Session:
    """A single child process plus the bookkeeping to turn it into request/response."""

    def __init__(self) -> None:
        self.turn_lock = threading.Lock()     # serializes turns; one prompt in flight
        self.state = threading.Lock()         # guards the fields below
        self.proc: subprocess.Popen[str] | None = None
        self.session_id: str | None = None
        self.events: deque[tuple[int, dict]] = deque(maxlen=EVENT_HISTORY)
        self.cursor = 0
        self.results: queue.Queue[dict] = queue.Queue()
        self.busy = False
        self.started_at = 0.0
        self.jobs: dict[str, Job] = {}
        self.job_order: deque[str] = deque(maxlen=JOB_HISTORY)
        self.subscribers: set[queue.Queue] = set()
        self._attempted_resume: str | None = None
        self._saw_init = False
        # Pick up where the previous bridge process left off, so a service
        # restart does not silently hand the next caller an amnesiac peer.
        resume = read_saved_session()
        if resume:
            log(f"resuming saved session {resume}")
        self.start(resume=resume)

    # ---- live watchers ---------------------------------------------------

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_BACKLOG)
        with self.state:
            self.subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self.state:
            self.subscribers.discard(channel)

    def _fanout(self, item: dict) -> None:
        with self.state:
            channels = list(self.subscribers)
        for channel in channels:
            try:
                channel.put_nowait(item)
            except queue.Full:
                pass  # a watcher too slow to keep up loses frames, not the session

    # ---- child lifecycle -------------------------------------------------

    def start(self, resume: str | None = None) -> None:
        argv = child_argv()
        if resume:
            argv += ["--resume", resume]
        log(f"starting child in {CHILD_CWD}" + (f" (resuming {resume})" if resume else ""))
        proc = subprocess.Popen(
            argv,
            cwd=CHILD_CWD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self.state:
            self.proc = proc
            self.started_at = time.time()
            self._attempted_resume = resume
            self._saw_init = False
            if not resume:
                self.session_id = None
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()

    def stop(self) -> None:
        with self.state:
            proc, self.proc = self.proc, None
        if not proc:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def restart(self, resume: bool = True) -> None:
        sid = self.session_id if resume else None
        if not resume:
            write_saved_session(None)  # --fresh must not be undone by a reboot
        self.stop()
        self.start(resume=sid)

    def alive(self) -> bool:
        with self.state:
            return self.proc is not None and self.proc.poll() is None

    # ---- reader threads --------------------------------------------------

    def _read_stdout(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log(f"non-JSON on stdout: {line[:200]}")
                continue
            self._record(event)
        log(f"child stdout closed (exit={proc.poll()})")
        with self.state:
            current = self.proc is proc
            stale_resume = current and self._attempted_resume and not self._saw_init
        if stale_resume:
            # The saved id no longer exists (session store pruned, or a
            # different machine wrote it). Without this the bridge would try the
            # same dead id on every restart and never come up.
            log(f"resume of {self._attempted_resume} failed; starting a fresh session")
            write_saved_session(None)
            self.start(resume=None)
            return
        # Unblock a caller waiting on a turn that can no longer complete.
        self.results.put({"__child_exited__": True, "exit_code": proc.poll()})

    def _read_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr
        for line in proc.stderr:
            if line.strip():
                log(f"child stderr: {line.rstrip()[:400]}")

    def _record(self, event: dict) -> None:
        # Partial deltas are for live watchers only. Keeping them out of the
        # history deque stops a single streamed turn from evicting the real
        # events that /transcript and turn summarising depend on.
        if event.get("type") == "stream_event":
            self._fanout({"index": None, "event": event})
            return
        with self.state:
            self.cursor += 1
            index = self.cursor
            self.events.append((index, event))
            sid = event.get("session_id")
            changed = bool(sid) and sid != self.session_id
            if sid:
                self.session_id = sid
            if event.get("type") == "system" and event.get("subtype") == "init":
                # Proof the child came up; a failed --resume never gets here.
                self._saw_init = True
        if changed:
            write_saved_session(sid)
        self._fanout({"index": index, "event": event})
        if event.get("type") == "result":
            self.results.put({"index": index, "event": event})

    # ---- turns -----------------------------------------------------------

    def submit(self, prompt: str, timeout: float,
               chain: list[str] | None = None) -> Job:
        job = Job(prompt, timeout, chain)
        with self.state:
            self.jobs[job.id] = job
            self.job_order.append(job.id)
            # deque eviction drops the id; drop the job with it.
            if len(self.jobs) > JOB_HISTORY:
                live = set(self.job_order)
                for stale in [j for j in self.jobs if j not in live]:
                    del self.jobs[stale]
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def _run_job(self, job: Job) -> None:
        try:
            job.result = self._run_turn(job)
            job.status = "done" if job.result.get("ok", True) else "error"
        except BridgeError as exc:
            job.status = "error"
            job.error = {"code": exc.code, "message": exc.message}
        except Exception as exc:  # never leave a job wedged in `running`
            job.status = "error"
            job.error = {"code": "internal", "message": str(exc)}
        finally:
            job.finished_at = time.time()
            job.done.set()

    def _run_turn(self, job: Job) -> dict:
        if not self.alive():
            log("child not running; restarting before prompt")
            self.restart(resume=True)

        with self.turn_lock:
            job.status = "running"
            with self.state:
                self.busy = True
                start_index = self.cursor
                proc = self.proc
            try:
                if not proc or not proc.stdin:
                    raise BridgeError("child_dead",
                                      "child process is not accepting input", 503)
                # Drain results from any previously abandoned turn.
                while not self.results.empty():
                    self.results.get_nowait()

                # Visible to this machine's own MCP server for the length of the
                # turn, so a session driven by a peer cannot relay back to it.
                write_inbound_chain(job.chain)

                message = {"type": "user",
                           "message": {"role": "user", "content": job.prompt}}
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()

                deadline = time.time() + job.timeout
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise BridgeError(
                            "turn_timeout",
                            f"no result within {job.timeout:.0f}s; the turn may still be "
                            f"running (poll /transcript, or POST /interrupt)",
                            504,
                        )
                    try:
                        item = self.results.get(timeout=min(remaining, 5.0))
                    except queue.Empty:
                        continue
                    if item.get("__child_exited__"):
                        raise BridgeError(
                            "child_dead",
                            f"child exited (code {item.get('exit_code')}) mid-turn",
                            503,
                        )
                    if item["index"] > start_index:
                        return self._summarize(item["event"], start_index)
            finally:
                write_inbound_chain(None)
                with self.state:
                    self.busy = False

    def _summarize(self, result: dict, start_index: int) -> dict:
        """Fold the turn's events into one response."""
        texts: list[str] = []
        tools: list[str] = []
        with self.state:
            window = [(i, e) for i, e in self.events if i > start_index]
        for _, event in window:
            if event.get("type") != "assistant":
                continue
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tools.append(block.get("name", "?"))
        return {
            "ok": not result.get("is_error", False),
            "result": result.get("result", ""),
            "assistant_text": "\n\n".join(texts),
            "tools_used": tools,
            "session_id": result.get("session_id"),
            "num_turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
            "total_cost_usd": result.get("total_cost_usd"),
            "stop_reason": result.get("stop_reason"),
            "permission_denials": result.get("permission_denials", []),
            "cursor": self.cursor,
        }

    def get_job(self, job_id: str) -> Job:
        with self.state:
            job = self.jobs.get(job_id)
        if not job:
            raise BridgeError("job_not_found", f"no job with id {job_id}", 404)
        return job

    def list_jobs(self, limit: int) -> list[dict]:
        with self.state:
            ids = list(self.job_order)[-limit:]
            return [self.jobs[i].snapshot(include_result=False)
                    for i in reversed(ids) if i in self.jobs]

    def interrupt(self) -> dict:
        with self.state:
            proc = self.proc
            busy = self.busy
        if not proc or not proc.stdin or proc.poll() is not None:
            raise BridgeError("child_dead", "no running child", 503)
        if not busy:
            raise BridgeError("no_turn_in_flight", "no turn is currently running", 409)
        request = {
            "type": "control_request",
            "request_id": f"bridge-{uuid.uuid4().hex[:12]}",
            "request": {"subtype": "interrupt"},
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        return {"ok": True, "detail": "interrupt sent"}

    def transcript(self, since: int, limit: int) -> dict:
        with self.state:
            window = [{"index": i, "event": e} for i, e in self.events if i > since]
            return {"cursor": self.cursor, "events": window[:limit]}

    def health(self) -> dict:
        with self.state:
            proc = self.proc
            queued = sum(1 for j in self.jobs.values()
                         if j.status in ("queued", "running"))
            return {
                "ok": proc is not None and proc.poll() is None,
                "name": NAME,
                "version": VERSION,
                "platform": platform.system(),
                # Two different processes; naming only one of them "pid" invites
                # matching the wrong one against Get-Process / ps.
                "bridge_pid": os.getpid(),
                "child_pid": proc.pid if proc else None,
                "exit_code": proc.poll() if proc else None,
                # The child emits no init event until it has input, so
                # session_id stays blank right after a restart — exactly when an
                # operator checks, and exactly the moment it reads as amnesia.
                # The id we are resuming is known at startup; say so.
                "session_id": self.session_id,
                "resuming": None if self.session_id else self._attempted_resume,
                "session_state": ("active" if self.session_id else
                                  "resuming" if self._attempted_resume else "fresh"),
                "busy": self.busy,
                "pending_jobs": queued,
                "cursor": self.cursor,
                "cwd": CHILD_CWD,
                "model": MODEL or "(cli default)",
                "permission_mode": PERMISSION_MODE,
                # started_at is reset by start(), so this tracks the child, not
                # the server. A restart makes it drop while the bridge keeps going.
                "child_uptime_s": round(time.time() - self.started_at, 1),
                "bridge_uptime_s": round(time.time() - BRIDGE_STARTED_AT, 1),
            }


SESSION: Session | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"ClaudeBridge/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def handle_one_request(self) -> None:
        """Serve a request, treating a dropped socket as normal.

        Clients that abandon a keep-alive connection after a completed response
        (PowerShell's Invoke-RestMethod does this on every call) would otherwise
        print a traceback per probe, which reads like a fault and is not one.
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            # Every spelling of "the peer went away": BrokenPipeError and
            # ConnectionResetError on POSIX, ConnectionAbortedError (WinError
            # 10053) on Windows. They are siblings, so a tuple of two misses one.
            self.close_connection = True

    # ---- helpers ---------------------------------------------------------

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else ""
        return bool(TOKEN) and hmac.compare_digest(presented, TOKEN)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, exc: BridgeError) -> None:
        self._send(exc.status, exc.payload())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BridgeError("bad_msg", f"invalid JSON body: {exc}") from exc

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._fail(BridgeError("unauthorized", "bad or missing bearer token", 401))
        return False

    def _stream(self, session: Session) -> None:
        """Server-sent events: every event as the child produces it.

        Held open until the client disconnects. Includes `stream_event` deltas,
        which is what makes text visible as it is typed rather than per turn.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        channel = session.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    item = channel.get(timeout=15.0)
                except queue.Empty:
                    # A comment frame doubles as a keepalive and as the write
                    # that surfaces a client which has gone away.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(item, separators=(",", ":"))
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except ConnectionError:
            pass  # watcher hung up; normal, in any of its platform spellings
        finally:
            session.unsubscribe(channel)

    # ---- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._guard():
            return
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        assert SESSION
        try:
            if path == "/health":
                self._send(200, SESSION.health())
            elif path == "/transcript":
                since = int(params.get("since", 0))
                limit = min(int(params.get("limit", 100)), 500)
                self._send(200, SESSION.transcript(since, limit))
            elif path == "/stream":
                self._stream(SESSION)
            elif path == "/jobs":
                limit = min(int(params.get("limit", 20)), JOB_HISTORY)
                self._send(200, {"jobs": SESSION.list_jobs(limit)})
            elif path == "/job":
                job_id = params.get("id", "")
                if not job_id:
                    raise BridgeError("bad_args", "`id` query parameter is required")
                job = SESSION.get_job(job_id)
                wait = float(params.get("wait", 0) or 0)
                if wait > 0:
                    job.done.wait(timeout=min(wait, 300.0))
                self._send(200, job.snapshot())
            else:
                raise BridgeError("not_found", f"no route {path}", 404)
        except BridgeError as exc:
            self._fail(exc)

    def do_POST(self) -> None:
        if not self._guard():
            return
        path, _, _ = self.path.partition("?")
        assert SESSION
        try:
            body = self._body()
            if path == "/prompt":
                prompt = body.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise BridgeError("bad_args", "`prompt` must be a non-empty string")
                timeout = float(body.get("timeout_seconds") or DEFAULT_TURN_TIMEOUT)
                chain = body.get("chain") or []
                if not isinstance(chain, list):
                    raise BridgeError("bad_args", "`chain` must be a list of names")
                job = SESSION.submit(prompt, timeout, [str(c) for c in chain])
                if body.get("async"):
                    self._send(202, job.snapshot())
                    return
                job.done.wait(timeout=timeout + 15)
                if job.error:
                    status = 504 if job.error["code"] == "turn_timeout" else 503
                    raise BridgeError(job.error["code"], job.error["message"], status)
                self._send(200, {**(job.result or {}), "job_id": job.id})
            elif path == "/interrupt":
                self._send(200, SESSION.interrupt())
            elif path == "/restart":
                SESSION.restart(resume=bool(body.get("resume", True)))
                self._send(200, SESSION.health())
            else:
                raise BridgeError("not_found", f"no route {path}", 404)
        except BridgeError as exc:
            self._fail(exc)


def main() -> None:
    global SESSION
    if not TOKEN:
        raise SystemExit("CLAUDE_BRIDGE_TOKEN must be set to a long random string")
    if len(TOKEN) < 24:
        raise SystemExit("CLAUDE_BRIDGE_TOKEN is too short; use at least 24 characters")
    SESSION = Session()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"claude-remote-relay bridge {VERSION} ({NAME}) on http://{HOST}:{PORT}")
    log(f"child cwd: {CHILD_CWD}")
    if HOST in ("0.0.0.0", "::"):
        log("WARNING: bound to all interfaces; prefer the tailnet IP or 127.0.0.1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        SESSION.stop()


if __name__ == "__main__":
    main()
