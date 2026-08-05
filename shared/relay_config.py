"""Host resolution and HTTP transport, shared by the MCP server and the CLI.

A "host" is one remote machine running `bridge/claude_bridge.py`. Hosts come
from a JSON config file:

    {
      "default": "bloc",
      "hosts": {
        "bloc": {
          "url": "http://100.73.225.65:8787",
          "token": "…",
          "description": "Windows desktop on the tailnet"
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.2.0"


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
