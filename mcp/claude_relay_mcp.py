#!/usr/bin/env python3
"""MCP stdio server exposing remote Claude Code sessions as tools.

Talks to one or more `bridge/claude_bridge.py` instances. Hosts are resolved by
`shared/relay_config.py` — either a hosts.json file or CLAUDE_RELAY_URL /
CLAUDE_RELAY_TOKEN for the single-machine case.

Implements the subset of MCP a tools-only server needs: initialize, tools/list,
tools/call. No third-party packages.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))

from relay_config import (  # noqa: E402
    VERSION, RelayError, call, load_hosts, outbound_chain, peer_health,
    read_inbound_chain, resolve, self_name,
)
from relay_converse import converse  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
HTTP_MARGIN = 30.0  # let the bridge's own timeout fire before urllib's

HOST_ARG = {
    "type": "string",
    "description": "Which configured machine to target. Omit for the default host.",
}

TOOLS = [
    {
        "name": "remote_peers",
        "description": (
            "List the remote machines this relay is configured to reach, with live "
            "health for each: whether its Claude Code process is up, what directory "
            "it is working in, and whether it is mid-turn. Call this first when you "
            "do not know which hosts exist."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remote_prompt",
        "description": (
            "Send a prompt to the Claude Code session on a remote machine. The "
            "conversation is persistent — each call continues the same session, so "
            "the remote side remembers earlier turns. Blocks until the reply is "
            "ready. For work that runs longer than a couple of minutes, pass "
            "background=true and collect the answer with remote_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The instruction to send to the remote session.",
                },
                "host": HOST_ARG,
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the turn (default 900).",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Return a job_id immediately instead of waiting. Use for long "
                        "tasks, then poll with remote_result."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "remote_converse",
        "description": (
            "Let two machines talk to each other. The opening message goes to the "
            "first host, its reply is forwarded to the second, and so on, without a "
            "human in between. The exchange stops as soon as either side signals it "
            "needs a human decision, says the conversation is complete, hits a "
            "permission denial, errors, or reaches max_turns. Returns the full "
            "transcript. Every hop is a real turn on both machines, so keep "
            "max_turns modest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exactly two configured host names, in speaking order.",
                },
                "opening": {
                    "type": "string",
                    "description": "The first message, sent to hosts[0].",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Hard cap on exchanges (default 6).",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Per-turn timeout (default 900).",
                },
            },
            "required": ["hosts", "opening"],
        },
    },
    {
        "name": "remote_result",
        "description": (
            "Fetch the outcome of a background prompt by job_id. Set wait_seconds to "
            "block until it finishes, or leave it at 0 to check and return immediately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Id from remote_prompt."},
                "host": HOST_ARG,
                "wait_seconds": {
                    "type": "number",
                    "description": "Block up to this long for completion (default 0, max 300).",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "remote_status",
        "description": (
            "Health of one remote session: process alive, session id, working "
            "directory, model, and whether a turn is in flight."
        ),
        "inputSchema": {"type": "object", "properties": {"host": HOST_ARG}},
    },
    {
        "name": "remote_transcript",
        "description": (
            "Read raw stream-json events from a remote session. Use it to watch "
            "progress on a long turn or to inspect what happened after a timeout. "
            "Pass the cursor from a previous call as `since` to get only new events."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "integer",
                          "description": "Return only events after this index (default 0)."},
                "limit": {"type": "integer",
                          "description": "Maximum events to return (default 100, max 500)."},
                "host": HOST_ARG,
            },
        },
    },
    {
        "name": "remote_interrupt",
        "description": (
            "Interrupt the turn currently running on a remote session, the way "
            "pressing Escape would. The turn ends early; the conversation survives."
        ),
        "inputSchema": {"type": "object", "properties": {"host": HOST_ARG}},
    },
    {
        "name": "remote_restart",
        "description": (
            "Restart the remote Claude Code process. With resume=true (default) it "
            "reattaches to the same conversation; resume=false starts fresh, "
            "discarding context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "resume": {"type": "boolean",
                           "description": "Keep the existing conversation (default true)."},
                "host": HOST_ARG,
            },
        },
    },
]


def require(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RelayError("bad_args", f"`{key}` is required and must be a non-empty string")
    return value


def format_turn(result: dict) -> str:
    """Render a finished turn as text for the model."""
    body = result.get("result") or result.get("assistant_text") or "(no text)"
    meta = []
    if result.get("tools_used"):
        meta.append(f"tools: {', '.join(result['tools_used'])}")
    if result.get("duration_ms") is not None:
        meta.append(f"{result['duration_ms'] / 1000:.1f}s")
    if result.get("total_cost_usd") is not None:
        meta.append(f"${result['total_cost_usd']:.4f}")
    if result.get("permission_denials"):
        meta.append(f"{len(result['permission_denials'])} permission denial(s)")
    if not result.get("ok", True):
        meta.append("turn ended with an error")
    if result.get("cursor") is not None:
        meta.append(f"cursor {result['cursor']}")
    return f"{body}\n\n---\n[{' | '.join(meta)}]" if meta else body


def run_tool(name: str, args: dict) -> str:
    host = args.get("host")

    if name == "remote_peers":
        hosts, default = load_hosts()
        chain = read_inbound_chain()
        lines = [f"This machine: {self_name()}"]
        if chain:
            lines.append(f"Currently running a relayed turn from: {' -> '.join(chain)}")
        lines.append("")
        names = sorted(hosts)
        results = peer_health(names)
        for host_name in names:
            entry, health = hosts[host_name], results[host_name]
            marker = " (default)" if host_name == default else ""
            if isinstance(health, RelayError):
                detail = f"unreachable ({health.code})"
            else:
                state = "busy" if health.get("busy") else "idle"
                detail = (f"{state} · {health.get('platform', '?')} · "
                          f"cwd {health.get('cwd', '?')} · model {health.get('model', '?')}")
            description = entry.get("description")
            lines.append(f"- {host_name}{marker}: {detail}"
                         + (f"\n    {description}" if description else ""))
        return "\n".join(lines) if lines else "no hosts configured"

    if name == "remote_prompt":
        prompt = require(args, "prompt")
        timeout = float(args.get("timeout_seconds") or 900.0)
        background = bool(args.get("background"))
        target, _ = resolve(host)
        payload = {"prompt": prompt, "timeout_seconds": timeout, "async": background,
                   "chain": outbound_chain(target)}
        result = call("POST", "/prompt", payload, host=host,
                      timeout=(30.0 if background else timeout + HTTP_MARGIN))
        if background:
            return (f"Started job {result['job_id']} on "
                    f"{host or 'the default host'} (status: {result['status']}).\n"
                    f"Collect it with remote_result.")
        return format_turn(result)

    if name == "remote_converse":
        hosts = args.get("hosts")
        if not isinstance(hosts, list) or len(hosts) != 2:
            raise RelayError("bad_args", "`hosts` must be exactly two host names")
        outcome = converse(
            [str(h) for h in hosts],
            require(args, "opening"),
            max_turns=int(args.get("max_turns") or 6),
            timeout=float(args.get("timeout_seconds") or 900.0),
        )
        lines = [f"Conversation between {' and '.join(outcome['hosts'])} "
                 f"({outcome['turns']} turns, ${outcome['total_cost_usd']:.4f})",
                 f"Ended because: {outcome['stopped_because']}", ""]
        for entry in outcome["transcript"]:
            if "error" in entry:
                lines.append(f"--- {entry['turn']}. {entry['host']} FAILED ---")
                lines.append(entry["error"])
            else:
                lines.append(f"--- {entry['turn']}. {entry['host']} ---")
                lines.append(entry["text"])
            lines.append("")
        return "\n".join(lines)

    if name == "remote_result":
        job_id = require(args, "job_id")
        wait = float(args.get("wait_seconds") or 0)
        result = call("GET", "/job", host=host, timeout=wait + HTTP_MARGIN,
                      params={"id": job_id, "wait": wait})
        status = result.get("status")
        if status in ("queued", "running"):
            return (f"Job {job_id} is still {status} after "
                    f"{result.get('elapsed_s')}s. Poll again, or interrupt it.")
        if result.get("error"):
            error = result["error"]
            return f"Job {job_id} failed: [{error.get('code')}] {error.get('message')}"
        return format_turn(result.get("result") or {})

    if name == "remote_status":
        return json.dumps(call("GET", "/health", host=host, timeout=20.0), indent=2)

    if name == "remote_transcript":
        params = {"since": int(args.get("since") or 0),
                  "limit": int(args.get("limit") or 100)}
        data = call("GET", "/transcript", host=host, timeout=30.0, params=params)
        return json.dumps(data, indent=2)[:60000]

    if name == "remote_interrupt":
        return json.dumps(call("POST", "/interrupt", {}, host=host, timeout=20.0))

    if name == "remote_restart":
        payload = {"resume": bool(args.get("resume", True))}
        return json.dumps(call("POST", "/restart", payload, host=host, timeout=60.0),
                          indent=2)

    raise RelayError("bad_args", f"unknown tool: {name}")


def respond(message_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        message_id = message.get("id")

        # Notifications carry no id and take no response.
        if message_id is None:
            continue

        if method == "initialize":
            respond(message_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claude-remote-relay", "version": VERSION},
            })
        elif method == "tools/list":
            respond(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                text = run_tool(params.get("name", ""), params.get("arguments") or {})
                respond(message_id, {"content": [{"type": "text", "text": text}]})
            except RelayError as exc:
                respond(message_id, {
                    "content": [{"type": "text", "text": f"Error {exc}"}],
                    "isError": True,
                })
            except Exception as exc:  # surfaced to the model rather than killing the server
                respond(message_id, {
                    "content": [{"type": "text", "text": f"Error [internal] {exc}"}],
                    "isError": True,
                })
        elif method == "ping":
            respond(message_id, {})
        else:
            respond(message_id, error={"code": -32601,
                                       "message": f"unknown method: {method}"})


if __name__ == "__main__":
    main()
