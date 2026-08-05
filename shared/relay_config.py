"""Host resolution and HTTP transport, shared by the MCP server and the CLI.

A "host" is one remote machine running `bridge/claude_bridge.py`. Hosts come
from a JSON config file:

    {
      "default": "remote",
      "hosts": {
        "remote": {
          "url": "http://100.100.100.10:8787",
          "token": "…",
          "description": "the machine with the GPU and the build toolchain"
        }
      }
    }

Searched in order:
  1. $CLAUDE_RELAY_CONFIG
  2. $CLAUDE_PLUGIN_DATA/hosts.json      (when running as a Claude Code plugin)
  3. ~/.config/claude-remote-relay/hosts.json

If no file is found, $CLAUDE_RELAY_URL and $CLAUDE_RELAY_TOKEN define a single
host named "default", so the single-machine setup needs no config file at all.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.4.0"

# Relay-loop protection. When two machines can each drive the other, a prompt
# can bounce A -> B -> A forever, burning a full turn's tokens per hop. Every
# request carries the chain of machines that have already relayed it; a machine
# refuses to forward to somewhere already in that chain.
MAX_DEPTH = int(os.environ.get("CLAUDE_RELAY_MAX_DEPTH", "3"))
CHAIN_TTL = 3600.0  # a stale inbound marker must not poison later local calls


def self_name() -> str:
    """This machine's name, as it appears in a relay chain."""
    return os.environ.get("CLAUDE_RELAY_SELF") or socket.gethostname()


def state_path() -> Path:
    """Where a bridge records the chain of the turn it is currently running.

    The bridge and the MCP server are separate processes on the same machine,
    so an inbound chain reaches the outbound side through this file.
    """
    explicit = os.environ.get("CLAUDE_RELAY_STATE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "claude-remote-relay" / "inbound.json"


def read_inbound_chain() -> list[str]:
    """The chain of the relayed turn this process is running inside, if any.

    Empty when the user is driving locally rather than being driven by a peer.
    """
    path = state_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if time.time() - float(data.get("written_at", 0)) > CHAIN_TTL:
        return []
    chain = data.get("chain")
    return [str(c) for c in chain] if isinstance(chain, list) else []


def write_inbound_chain(chain: list[str] | None) -> None:
    """Record (or clear) the chain of the turn a bridge is about to run."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if chain:
        path.write_text(json.dumps({"chain": chain, "written_at": time.time()}),
                        encoding="utf-8")
    elif path.exists():
        path.unlink()


def peer_health(hosts: list[str], timeout: float = 5.0) -> dict[str, dict | RelayError]:
    """Health for several hosts at once.

    Checked in parallel and with a short timeout: a listing that waits serially
    on every offline machine is unusable as soon as one is asleep.
    """
    import concurrent.futures

    def probe(name: str) -> dict | RelayError:
        try:
            return call("GET", "/health", host=name, timeout=timeout)
        except RelayError as exc:
            return exc

    if not hosts:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), 8)) as pool:
        return dict(zip(hosts, pool.map(probe, hosts)))


def outbound_chain(target: str) -> list[str]:
    """Chain to attach when forwarding to `target`. Raises if that would loop.

    Only the *inbound* chain can prove a loop: it names machines that already
    relayed this request. Testing against our own name too would reject a
    legitimate depth-0 call to our own bridge, which is a separate session
    rather than a bounce.
    """
    inbound = read_inbound_chain()
    chain = inbound + [self_name()]
    if target in inbound:
        raise RelayError(
            "relay_loop",
            f"refusing to relay to '{target}': it already relayed this request "
            f"({' -> '.join(chain)}). Something is bouncing a prompt back and forth.",
        )
    if len(chain) >= MAX_DEPTH:
        raise RelayError(
            "max_depth_exceeded",
            f"relay chain {' -> '.join(chain)} is at the limit of {MAX_DEPTH} hops "
            f"(raise CLAUDE_RELAY_MAX_DEPTH if this is intentional).",
        )
    return chain


class RelayError(Exception):
    """An error with a machine-readable code, mirrored in the README's table."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def config_path() -> Path | None:
    explicit = os.environ.get("CLAUDE_RELAY_CONFIG")
    if explicit:
        return Path(explicit)
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    candidates = []
    if plugin_data:
        candidates.append(Path(plugin_data) / "hosts.json")
    candidates.append(Path.home() / ".config" / "claude-remote-relay" / "hosts.json")
    return next((p for p in candidates if p.is_file()), None)


def load_hosts() -> tuple[dict[str, dict], str | None]:
    """Return (hosts, default_name)."""
    path = config_path()
    if path:
        if not path.is_file():
            raise RelayError("config_missing", f"config file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RelayError("config_invalid", f"{path} is not valid JSON: {exc}") from exc
        hosts = raw.get("hosts") or {}
        if not isinstance(hosts, dict) or not hosts:
            raise RelayError("config_invalid", f"{path} defines no hosts")
        for name, entry in hosts.items():
            if not isinstance(entry, dict) or not entry.get("url"):
                raise RelayError("config_invalid", f"host '{name}' has no url")
        return hosts, raw.get("default") or next(iter(hosts))

    url = os.environ.get("CLAUDE_RELAY_URL")
    token = os.environ.get("CLAUDE_RELAY_TOKEN")
    if url:
        return {"default": {"url": url, "token": token or "",
                            "description": "from CLAUDE_RELAY_URL"}}, "default"
    raise RelayError(
        "no_hosts",
        "no hosts configured: set CLAUDE_RELAY_URL and CLAUDE_RELAY_TOKEN, or "
        "create ~/.config/claude-remote-relay/hosts.json",
    )


def resolve(host: str | None = None) -> tuple[str, dict]:
    hosts, default = load_hosts()
    name = host or default
    if name not in hosts:
        raise RelayError(
            "host_unknown",
            f"no host named '{name}'. Configured: {', '.join(sorted(hosts))}",
        )
    return name, hosts[name]


def call(method: str, path: str, payload: dict | None = None,
         host: str | None = None, timeout: float = 120.0,
         params: dict | None = None) -> dict:
    """One HTTP call against a host's bridge. Raises RelayError with a code."""
    name, entry = resolve(host)
    url = entry["url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {entry.get('token', '')}",
            "Content-Type": "application/json",
            "User-Agent": f"claude-remote-relay/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(body).get("error", {})
            raise RelayError(error.get("code", f"http_{exc.code}"),
                             f"{name}: {error.get('message', body[:400])}") from exc
        except json.JSONDecodeError:
            raise RelayError(f"http_{exc.code}", f"{name}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RelayError(
            "host_unreachable",
            f"cannot reach {name} at {entry['url']} ({exc.reason}). "
            f"Is claude_bridge.py running and the machine online?",
        ) from exc
    except TimeoutError as exc:
        raise RelayError("host_unreachable", f"{name} timed out after {timeout:.0f}s") from exc
