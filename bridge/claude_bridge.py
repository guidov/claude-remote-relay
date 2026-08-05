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
    CLAUDE_BRIDGE_TOKEN    required; shared secret for the Authorization header
    CLAUDE_BRIDGE_HOST     bind address (default 127.0.0.1; set to the tailnet IP to expose)
    CLAUDE_BRIDGE_PORT     bind port (default 8787)
    CLAUDE_BRIDGE_NAME     label reported by /health (default: hostname)
    CLAUDE_BRIDGE_CWD      working directory for the child (default: this process's cwd)
    CLAUDE_BRIDGE_MODEL    --model value (default: the CLI's own default)
    CLAUDE_BRIDGE_PERMISSION_MODE   --permission-mode (default acceptEdits)
    CLAUDE_BRIDGE_ARGS     extra args for the child, shell-quoted
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

from relay_config import write_inbound_chain  # noqa: E402

VERSION = "0.3.1"

TOKEN = os.environ.get("CLAUDE_BRIDGE_TOKEN", "")
HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "8787"))
NAME = os.environ.get("CLAUDE_BRIDGE_NAME") or socket.gethostname()
CHILD_CWD = os.environ.get("CLAUDE_BRIDGE_CWD") or os.getcwd()
MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "")
PERMISSION_MODE = os.environ.get("CLAUDE_BRIDGE_PERMISSION_MODE", "acceptEdits")
EXTRA_ARGS = shlex.split(os.environ.get("CLAUDE_BRIDGE_ARGS", ""))

DEFAULT_TURN_TIMEOUT = 900.0
EVENT_HISTORY = 4000
JOB_HISTORY = 200


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


def child_argv() -> list[str]:
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("`claude` not found on PATH")
    argv = [
        exe,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", PERMISSION_MODE,
    ]
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
        self.start()

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
        # Unblock a caller waiting on a turn that can no longer complete.
        self.results.put({"__child_exited__": True, "exit_code": proc.poll()})

    def _read_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr
        for line in proc.stderr:
            if line.strip():
                log(f"child stderr: {line.rstrip()[:400]}")

    def _record(self, event: dict) -> None:
        with self.state:
            self.cursor += 1
            index = self.cursor
            self.events.append((index, event))
            sid = event.get("session_id")
            if sid:
                self.session_id = sid
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
                "pid": proc.pid if proc else None,
                "exit_code": proc.poll() if proc else None,
                "session_id": self.session_id,
                "busy": self.busy,
                "pending_jobs": queued,
                "cursor": self.cursor,
                "cwd": CHILD_CWD,
                "model": MODEL or "(cli default)",
                "permission_mode": PERMISSION_MODE,
                "uptime_s": round(time.time() - self.started_at, 1),
            }


SESSION: Session | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"ClaudeBridge/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log(f"{self.address_string()} {fmt % args}")

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
