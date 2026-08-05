# claude-remote-relay

Drive Claude Code sessions on **other machines** from the one you're sitting at.

Working on a Linux laptop but the Windows desktop has the GPU, the build
toolchain, or the repo you need? Say *"ask bloc to run the test suite and
summarize the failures"* and it happens over there, in a session that remembers
what you asked it last time.

Ships as a Claude Code plugin. Python 3.9+ standard library only — no npm, no
bun, no pip install, on either machine.

## Relationship to [vildanbina/claude-relay](https://github.com/vildanbina/claude-relay)

Different problem, and the two compose. That project connects sessions **on one
machine** through a Unix-socket hub, so peers can message each other in natural
language. This one reaches **across machines** over HTTP, so a session here can
drive a session there. Run both if you want local peer chat and remote control;
the tool names (`relay_*` vs `remote_*`) don't collide.

## Why not Playwright against claude.ai

A `claude.ai/code/session_…` URL is the **Remote Control** surface
(`claude --remote-control`), not an API. Driving it with a browser would mean
replaying your claude.ai cookies into an automated Chrome and scraping a React
UI with no stable selectors — it breaks on every frontend deploy, and automating
the web interface is outside what Anthropic's usage policies allow. If the
machine is reachable on your network, the bridge below talks to the CLI's own
`stream-json` protocol instead: faster, scriptable, and it won't rot.

## Architecture

```
      your machine                    │            remote machine
                                      │
   Claude Code                        │        claude_bridge.py
        │ stdio                       │              │ stdin/stdout
        ▼                             │              ▼  (stream-json)
  claude_relay_mcp.py ──── HTTP ──────┼────────▶  claude -p
   (MCP server)          bearer token │         (long-lived child)
        ▲                             │
        │                             │        conversation state
   relay.py (CLI)                     │        lives HERE
```

One long-lived `claude` process per bridge. Because it never exits between
requests, **context accumulates**: call three knows what calls one and two said.

| Path | Runs on | Purpose |
| --- | --- | --- |
| `bridge/claude_bridge.py` | each **remote** machine | owns the `claude` child, exposes it over HTTP |
| `mcp/claude_relay_mcp.py` | your machine | MCP server — the `remote_*` tools |
| `cli/relay.py` | anywhere | CLI client for shell scripts |
| `shared/relay_config.py` | your machine | host resolution + HTTP transport |

## Install

### 1. Start a bridge on each remote machine

Generate one token per machine:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy `bridge/claude_bridge.py` over, then (PowerShell shown; bash is the same
idea with `export`):

```powershell
$env:CLAUDE_BRIDGE_TOKEN = "<the token>"
$env:CLAUDE_BRIDGE_HOST  = "100.100.100.10"      # tailnet IP, not 0.0.0.0
$env:CLAUDE_BRIDGE_NAME  = "bloc"
$env:CLAUDE_BRIDGE_CWD   = "C:\path\to\the\repo"
python claude_bridge.py
```

Windows Firewall prompts on first bind — allow **private** networks only. To
survive logout, wrap it in NSSM or a Scheduled Task set to "Run whether user is
logged on or not".

### 2. Install the plugin on your machine

```
/plugin marketplace add guidov/claude-remote-relay
/plugin install remote-relay@claude-remote-relay
```

Or register the MCP server directly, without the plugin:

```bash
claude mcp add remote-relay \
  -e CLAUDE_RELAY_URL=http://100.100.100.10:8787 \
  -e CLAUDE_RELAY_TOKEN=<the token> \
  -- python3 /path/to/claude-remote-relay/mcp/claude_relay_mcp.py
```

### 3. Point it at your machines

For one machine, `CLAUDE_RELAY_URL` + `CLAUDE_RELAY_TOKEN` is enough. For
several, write `~/.config/claude-remote-relay/hosts.json` (see
`hosts.example.json`):

```json
{
  "default": "bloc",
  "hosts": {
    "bloc":   { "url": "http://100.100.100.10:8787",  "token": "…", "description": "Windows desktop" },
    "greyai": { "url": "http://100.100.100.20:8787", "token": "…", "description": "Linux server" }
  }
}
```

Config is searched at `$CLAUDE_RELAY_CONFIG`, then
`$CLAUDE_PLUGIN_DATA/hosts.json`, then `~/.config/claude-remote-relay/hosts.json`.
**That file holds bearer tokens — `chmod 600` it.** `hosts.json` is gitignored.

## Usage

Just talk: *"ask bloc what's failing in the test suite"*. Claude routes to the
tools on its own. Slash commands are faster when you know what you want:
`/relay-peers`, `/relay-send bloc <prompt>`, `/relay-interrupt`.

### Tools

| Tool | What it does |
| --- | --- |
| `remote_peers` | List configured machines with live health |
| `remote_prompt` | Send a prompt; `background: true` returns a job id instead of blocking |
| `remote_result` | Collect a background job by id |
| `remote_status` | Health of one machine |
| `remote_transcript` | Raw event stream; watch a long turn |
| `remote_interrupt` | Escape, effectively |
| `remote_restart` | Restart the process, keeping context by default |

### Shell

```bash
export CLAUDE_RELAY_URL=http://100.100.100.10:8787
export CLAUDE_RELAY_TOKEN=<token>

relay.py peers
relay.py send "run the test suite and summarize failures"
relay.py --host greyai send "check disk usage"
git diff | relay.py send -                        # pipe stdin
JOB=$(relay.py send -b "full regression run")     # background
relay.py result "$JOB" --wait 600
relay.py watch --follow                           # tail a running turn
relay.py interrupt
relay.py restart --fresh                          # drop context
```

Exit codes: `0` success · `1` the remote turn errored · `2` relay-level failure
· `3` job still running.

## How it works

The bridge spawns and keeps alive:

```
claude -p --input-format stream-json --output-format stream-json --verbose \
       --permission-mode acceptEdits [--model …]
```

Each prompt is one NDJSON line on the child's stdin:

```json
{"type":"user","message":{"role":"user","content":"…"}}
```

The child streams `system`, `assistant`, and `user` events, then exactly one
`result` event ending the turn. The bridge folds that window into a single JSON
response. Turns are serialized behind a lock; every submission becomes a *job*,
so `background: true` is the same code path as a blocking call minus the wait.

`/interrupt` writes a control message on the same channel:

```json
{"type":"control_request","request_id":"…","request":{"subtype":"interrupt"}}
```

The child acks with `control_response` and ends the turn with `is_error: true`;
partial output survives in `assistant_text`. If the child dies, the next prompt
restarts it with `--resume <session_id>`, so the conversation outlives a crash.

## Error codes

| Code | Meaning |
| --- | --- |
| `unauthorized` | Bad or missing bearer token |
| `host_unknown` | No host by that name in the config |
| `host_unreachable` | Machine offline, or the bridge isn't running |
| `no_hosts` | Nothing configured; set `CLAUDE_RELAY_URL` or write hosts.json |
| `config_invalid` | hosts.json is malformed or defines no hosts |
| `child_dead` | The `claude` process exited unexpectedly |
| `turn_timeout` | No result within the timeout; the turn may still be running |
| `no_turn_in_flight` | Interrupt sent with nothing running |
| `job_not_found` | No job with that id (they age out after 200) |
| `bad_args` | Missing or wrong-typed argument |
| `bad_msg` | Malformed JSON body |

## Verified behaviour

Tested end to end against a live bridge:

- prompt → reply, with context persisting across separate HTTP calls
- background submit → poll → collect, across two *different* MCP processes
  (job state lives on the bridge, not the client)
- interrupt mid-turn, partial text preserved
- `--resume` recovering full context after the child was killed outright
- bearer-token rejection, unknown host, unreachable host, unknown job — each
  surfacing its documented code
- MCP `initialize` / `tools/list` / `tools/call` handshake

Not yet exercised: the Windows-specific paths (the `.cmd` shim wrapper, the
firewall prompt). The bridge has only been run on Linux so far.

## Security

The bridge runs Claude Code with file and shell access on the remote machine.
Anyone who can reach the port and holds the token can make it edit files and run
commands. Treat it as remote code execution, because it is.

- Token required, 24 characters minimum, compared with `hmac.compare_digest`.
- Bind to a **specific** address — tailnet IP or `127.0.0.1`, never `0.0.0.0`.
  The bridge warns when you bind to all interfaces.
- Over Tailscale, WireGuard encrypts the traffic. Over plain LAN this is
  **cleartext HTTP** — prefer the tailnet address, or front it with TLS.
- `CLAUDE_BRIDGE_CWD` is the blast radius. Point it at one project directory.
- `bypassPermissions` removes every approval prompt on a machine nobody is
  watching. The default is `acceptEdits` for that reason.

## Limitations

- One conversation per bridge. Run a second instance on another port for a
  second project.
- Prompts serialize; a second one queues behind the first.
- Approval prompts can't be answered remotely. A tool needing more than the
  permission mode grants is denied and reported in `permission_denials`.
- No streaming into the caller's context — `remote_transcript` polls instead.

## Development

```bash
python3 scripts/check_version.py    # version consistent across all 4 files
```

Bump the version in `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `shared/relay_config.py`, and
`bridge/claude_bridge.py` together; the script exists because nothing else
enforces it.

## License

MIT
