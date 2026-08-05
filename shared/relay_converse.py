"""Run a conversation between two Claude Code sessions until one needs a human.

The orchestrator shuttles messages: the opening goes to the first host, its
reply to the second, that reply back to the first, and so on. It stops on the
first halt condition rather than running forever.

Driving the exchange from one place — rather than letting each side call the
other and recurse — is what keeps it bounded. Every hop is a real turn costing
real tokens, so the stop conditions matter more than the happy path.
"""

from __future__ import annotations

from relay_config import RelayError, call, outbound_chain

NEEDS_USER = "NEEDS-USER-INPUT"
COMPLETE = "CONVERSATION-COMPLETE"

BRIEFING = f"""\
[relay conversation with {{peer}}]

You are talking to another Claude Code session, on a different machine. Your \
reply is forwarded to it verbatim, and its reply comes back to you. There is no \
human in the loop right now.

End your reply with one of these tokens on its own line when it applies:
  {NEEDS_USER} — you need a decision, credential, or approval from the human
  {COMPLETE}   — the exchange has reached its natural end

Otherwise just reply normally and the conversation continues. Be concise; every \
exchange costs a full turn on both machines.

---
{{message}}"""


def halt_reason(text: str) -> str | None:
    upper = text.upper()
    if NEEDS_USER in upper:
        return "needs_user_input"
    if COMPLETE in upper:
        return "complete"
    return None


def converse(hosts: list[str], opening: str, max_turns: int = 6,
             timeout: float = 900.0, on_turn=None) -> dict:
    """Shuttle messages between two hosts until something says stop.

    Returns the transcript plus why it ended. `on_turn(entry)` is called after
    each reply so a caller can stream progress.
    """
    if len(hosts) != 2:
        raise RelayError("bad_args", "converse needs exactly two hosts")
    if hosts[0] == hosts[1]:
        raise RelayError("bad_args", "the two hosts must be different")

    transcript: list[dict] = []
    briefed: set[str] = set()
    message = opening
    stopped = "max_turns"
    total_cost = 0.0

    for turn in range(max_turns):
        target = hosts[turn % 2]
        peer = hosts[(turn + 1) % 2]

        if target in briefed:
            payload_prompt = message
        else:
            payload_prompt = BRIEFING.format(peer=peer, message=message)
            briefed.add(target)

        try:
            result = call("POST", "/prompt",
                          {"prompt": payload_prompt, "timeout_seconds": timeout,
                           "chain": outbound_chain(target)},
                          host=target, timeout=timeout + 30)
        except RelayError as exc:
            transcript.append({"turn": turn + 1, "host": target,
                               "error": f"[{exc.code}] {exc.message}"})
            stopped = f"error:{exc.code}"
            break

        text = result.get("result") or result.get("assistant_text") or ""
        cost = result.get("total_cost_usd") or 0.0
        total_cost += cost
        entry = {
            "turn": turn + 1,
            "host": target,
            "text": text,
            "cost_usd": cost,
            "duration_ms": result.get("duration_ms"),
            "tools_used": result.get("tools_used", []),
        }
        transcript.append(entry)
        if on_turn:
            on_turn(entry)

        if not result.get("ok", True):
            stopped = "turn_error"
            break
        if result.get("permission_denials"):
            # The remote side wanted to do something its permission mode forbids;
            # that needs a human, not another lap.
            stopped = "permission_denied"
            break
        reason = halt_reason(text)
        if reason:
            stopped = reason
            break

        message = text

    return {
        "transcript": transcript,
        "stopped_because": stopped,
        "turns": len(transcript),
        "total_cost_usd": round(total_cost, 4),
        "hosts": hosts,
    }
