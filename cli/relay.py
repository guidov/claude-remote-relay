#!/usr/bin/env python3
"""Command-line client for remote Claude Code bridges.

For scripting remote sessions without going through MCP.

    export CLAUDE_RELAY_URL=http://100.100.100.10:8787
    export CLAUDE_RELAY_TOKEN=<token>
    # …or configure several machines in ~/.config/claude-remote-relay/hosts.json

    relay.py peers
    relay.py send "run the test suite and summarize failures"
    relay.py send --host remote --timeout 1800 "refactor the parser"
    relay.py send --background "full regression run"      # prints a job id
    relay.py result job-abc123 --wait 120
    echo "review this diff" | relay.py send -
    relay.py watch --follow
    relay.py interrupt
    relay.py restart --fresh

Exit status is 1 when the remote turn itself reported an error, and 2 on a
relay-level failure, so shell pipelines can branch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))

from relay_config import (  # noqa: E402
    RelayError, call, load_hosts, outbound_chain, peer_health, read_inbound_chain,
    resolve, self_name,
)
from relay_converse import converse  # noqa: E402
from relay_stream import Streamer, watch as watch_stream  # noqa: E402


def print_turn(result: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("result") or result.get("assistant_text") or "(no text)")
        if result.get("tools_used"):
            print(f"\n[tools: {', '.join(result['tools_used'])}]", file=sys.stderr)
        cost = result.get("total_cost_usd")
        if cost is not None:
            print(f"[{result.get('duration_ms', 0) / 1000:.1f}s | ${cost:.4f} | "
                  f"cursor {result.get('cursor')}]", file=sys.stderr)
    return 0 if result.get("ok", True) else 1


def cmd_converse(args: argparse.Namespace) -> int:
    def show(entry: dict) -> None:
        print(f"--- {entry['turn']}. {entry['host']} "
              f"({entry['duration_ms'] / 1000:.1f}s, ${entry['cost_usd']:.4f}) ---")
        print(entry["text"])
        print()

    def footer(entry: dict) -> None:
        # The text already arrived live; only the accounting is still news.
        denials = entry.get("permission_denials") or 0
        note = f", {denials} denied" if denials else ""
        print(f"  [{entry['host']} turn {entry['turn']}: "
              f"{entry['duration_ms'] / 1000:.1f}s, ${entry['cost_usd']:.4f}{note}]\n",
              file=sys.stderr)

    if args.json:
        hook = None
    elif args.stream:
        hook = footer
    else:
        hook = show
    outcome = converse(args.hosts, args.opening, max_turns=args.max_turns,
                       timeout=args.timeout, on_turn=hook, stream=args.stream)
    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        print(f"[{outcome['turns']} turns · ${outcome['total_cost_usd']:.4f} · "
              f"ended: {outcome['stopped_because']}]", file=sys.stderr)
    return 0 if outcome["stopped_because"] in ("complete", "needs_user_input") else 1


def cmd_peers(args: argparse.Namespace) -> int:
    hosts, default = load_hosts()
    print(f"this machine: {self_name()}")
    chain = read_inbound_chain()
    if chain:
        print(f"running a relayed turn from: {' -> '.join(chain)}")
    print()
    names = sorted(hosts)
    warnings: list[tuple[str, str]] = []
    results = peer_health(names)
    for name in names:
        entry, health = hosts[name], results[name]
        marker = " (default)" if name == default else ""
        if isinstance(health, RelayError):
            status = f"unreachable ({health.code})"
        else:
            state = "busy" if health.get("busy") else "idle"
            status = (f"{state:<4} {health.get('platform', '?'):<8} "
                      f"cwd={health.get('cwd', '?')}")
        print(f"{name}{marker}\n    {entry['url']}\n    {status}")
        if entry.get("description"):
            print(f"    {entry['description']}")
        # The loop guard matches a chain entry (a peer's self_name) against the
        # alias we forward to. If they disagree, a bounced prompt is not caught
        # as a loop — it runs an extra hop and dies as max_depth_exceeded, which
        # names the wrong problem.
        if not isinstance(health, RelayError):
            theirs = health.get("self_name")
            if theirs and theirs != name:
                warnings.append((name, theirs))
    for alias, theirs in warnings:
        print(f"\nwarning: alias '{alias}' but that machine calls itself "
              f"'{theirs}'.\n  Loop protection compares the two, so set "
              f"CLAUDE_RELAY_SELF={alias} there (or rename the alias to "
              f"'{theirs}').", file=sys.stderr)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    if not prompt.strip():
        sys.exit("empty prompt")
    target, _ = resolve(args.host)
    if args.stream and not args.background:
        result = run_turn_streamed(target, prompt, args.timeout)
        if result is None:
            return 1
        turn_footer(result)
        return 0 if result.get("ok", True) else 1
    payload = {"prompt": prompt, "timeout_seconds": args.timeout,
               "async": args.background, "chain": outbound_chain(target)}
    result = call("POST", "/prompt", payload, host=args.host,
                  timeout=(30.0 if args.background else args.timeout + 30))
    if args.background:
        print(result["job_id"])
        print(f"[queued on {args.host or 'default'}; collect with: "
              f"relay.py result {result['job_id']}]", file=sys.stderr)
        return 0
    return print_turn(result, args.json)


def cmd_result(args: argparse.Namespace) -> int:
    result = call("GET", "/job", host=args.host, timeout=args.wait + 30,
                  params={"id": args.job_id, "wait": args.wait})
    status = result.get("status")
    if status in ("queued", "running"):
        print(f"{args.job_id} is {status} ({result.get('elapsed_s')}s elapsed)",
              file=sys.stderr)
        return 3
    if result.get("error"):
        error = result["error"]
        print(f"job failed: [{error.get('code')}] {error.get('message')}", file=sys.stderr)
        return 1
    return print_turn(result.get("result") or {}, args.json)


def cmd_jobs(args: argparse.Namespace) -> int:
    data = call("GET", "/jobs", host=args.host, timeout=20.0,
                params={"limit": args.limit})
    for job in data["jobs"]:
        print(f"{job['job_id']}  {job['status']:<8} {job['elapsed_s']:>7.1f}s  "
              f"{job['prompt'][:60]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(call("GET", "/health", host=args.host, timeout=20.0), indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Tail a remote session's events, so a long turn is visible as it runs."""
    since = args.since
    while True:
        data = call("GET", "/transcript", host=args.host, timeout=30.0,
                    params={"since": since, "limit": 200})
        for entry in data["events"]:
            event = entry["event"]
            kind = event.get("type")
            if kind == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        print(block["text"])
                    elif block.get("type") == "tool_use":
                        print(f"  · {block.get('name')}", file=sys.stderr)
            elif kind == "result":
                state = "error" if event.get("is_error") else "done"
                print(f"[turn {state} | ${event.get('total_cost_usd', 0):.4f}]",
                      file=sys.stderr)
        since = data["cursor"]
        if not args.follow:
            return 0
        time.sleep(args.interval)


def run_turn_streamed(host: str, prompt: str, timeout: float) -> dict | None:
    """One prompt, rendered live. Returns the finished turn, or None if interrupted."""
    streamer = Streamer(host).start()
    try:
        return call("POST", "/prompt",
                    {"prompt": prompt, "timeout_seconds": timeout,
                     "chain": outbound_chain(host)},
                    host=host, timeout=timeout + 30)
    except KeyboardInterrupt:
        # Ctrl-C during a turn means "stop that", not "quit the client".
        streamer.stop()
        streamer = None
        print("\n[interrupting…]", file=sys.stderr)
        try:
            call("POST", "/interrupt", {}, host=host, timeout=20.0)
        except RelayError as exc:
            print(f"  ({exc})", file=sys.stderr)
        return None
    finally:
        if streamer:
            streamer.stop()


def turn_footer(result: dict) -> None:
    denials = len(result.get("permission_denials") or [])
    bits = [f"{(result.get('duration_ms') or 0) / 1000:.1f}s",
            f"${result.get('total_cost_usd') or 0:.4f}"]
    if denials:
        bits.append(f"{denials} denied")
    if not result.get("ok", True):
        bits.append("turn errored")
    print(f"  [{' · '.join(bits)}]\n", file=sys.stderr)


def cmd_chat(args: argparse.Namespace) -> int:
    """An interactive prompt against one machine, with its output rendered live."""
    name, _ = resolve(args.host)
    print(f"[chat with {name} — Ctrl-D to exit, Ctrl-C to interrupt a turn]",
          file=sys.stderr)
    while True:
        try:
            prompt = input(f"\n{name}> ")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not prompt.strip():
            continue
        try:
            result = run_turn_streamed(name, prompt, args.timeout)
        except RelayError as exc:
            print(f"relay error {exc}", file=sys.stderr)
            continue
        if result:
            turn_footer(result)


def cmd_stream(args: argparse.Namespace) -> int:
    name, _ = resolve(args.host)
    print(f"[streaming {name} — Ctrl-C to stop]", file=sys.stderr)
    return watch_stream(name, show_thinking=args.thinking)


def cmd_interrupt(args: argparse.Namespace) -> int:
    print(json.dumps(call("POST", "/interrupt", {}, host=args.host, timeout=20.0)))
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    print(json.dumps(call("POST", "/restart", {"resume": not args.fresh},
                          host=args.host, timeout=60.0), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="configured host name (default: the config's default)")
    sub = parser.add_subparsers(dest="command", required=True)

    peers = sub.add_parser("peers", help="list configured machines and their health")
    peers.set_defaults(func=cmd_peers)

    send = sub.add_parser("send", help="send a prompt and print the reply")
    send.add_argument("prompt", help="prompt text, or - to read stdin")
    send.add_argument("--timeout", type=float, default=900.0)
    send.add_argument("--background", "-b", action="store_true",
                      help="return a job id immediately")
    send.add_argument("--stream", "-s", action="store_true",
                      help="render the reply live, as it is typed")
    send.add_argument("--json", action="store_true", help="print the full result object")
    send.set_defaults(func=cmd_send)

    chat = sub.add_parser("chat", help="interactive session with one machine, rendered live")
    chat.add_argument("--timeout", type=float, default=900.0)
    chat.set_defaults(func=cmd_chat)

    result = sub.add_parser("result", help="collect a background job")
    result.add_argument("job_id")
    result.add_argument("--wait", type=float, default=0.0,
                        help="block up to N seconds for completion")
    result.add_argument("--json", action="store_true")
    result.set_defaults(func=cmd_result)

    jobs = sub.add_parser("jobs", help="list recent jobs")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.set_defaults(func=cmd_jobs)

    talk = sub.add_parser("converse", help="let two machines talk to each other")
    talk.add_argument("hosts", nargs=2, metavar="HOST",
                      help="the two machines, in speaking order")
    talk.add_argument("--opening", required=True, help="first message, sent to HOST 1")
    talk.add_argument("--max-turns", type=int, default=6, dest="max_turns")
    talk.add_argument("--timeout", type=float, default=900.0)
    talk.add_argument("--stream", "-s", action="store_true",
                      help="render each machine's text live, as it is typed")
    talk.add_argument("--json", action="store_true")
    talk.set_defaults(func=cmd_converse)

    status = sub.add_parser("status", help="show remote session health")
    status.set_defaults(func=cmd_status)

    watch = sub.add_parser("watch", help="print remote events")
    watch.add_argument("--since", type=int, default=0)
    watch.add_argument("--follow", "-f", action="store_true")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.set_defaults(func=cmd_watch)

    live = sub.add_parser("stream", help="watch a machine type, live")
    live.add_argument("--thinking", action="store_true", help="also show reasoning")
    live.set_defaults(func=cmd_stream)

    interrupt = sub.add_parser("interrupt", help="stop the running turn")
    interrupt.set_defaults(func=cmd_interrupt)

    restart = sub.add_parser("restart", help="restart the remote process")
    restart.add_argument("--fresh", action="store_true", help="discard conversation context")
    restart.set_defaults(func=cmd_restart)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RelayError as exc:
        print(f"relay error {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
