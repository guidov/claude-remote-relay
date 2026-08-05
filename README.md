# claude-remote-relay

Drive Claude Code sessions on **other machines** from the one you're sitting at.

Working on a Linux laptop but the Windows desktop has the GPU, the build
toolchain, or the repo you need? Say *"ask `remote` to run the test suite and
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

## Why not just call the claude.ai session URL?

The obvious first idea: your remote session already has an address —
`https://claude.ai/code/session_01…` — so why not POST to it?

Because it isn't an endpoint. It's a UI route:

```console
$ curl -si https://claude.ai/code/session_01…
HTTP/2 403
content-type: text/html; charset=UTF-8

<!DOCTYPE html><html><head><title>Just a moment...</title>…
```

Three walls, any one of them fatal:

1. **It's a web page.** `text/html` — the React app that *renders* a session.
   The real transport is an internal WebSocket the SPA opens after boot. There
   is no documented JSON API behind that path for submitting a prompt.
2. **Cloudflare blocks non-browser clients** before auth is even considered.
   That 403 is a bot challenge.
3. **It needs your claude.ai browser session** — the web app's OAuth cookies,
   not an API key. A Claude Code API credential authenticates to a different
   surface with no route to a Remote Control session.

So "talk to it over HTTP" really means *drive a logged-in Chrome and scrape the
SPA*. That breaks on every frontend deploy, needs your cookies in an automated
browser, and automating the web interface is outside what Anthropic's usage
policies allow. The URL looks callable; it is a UI route.

If the machine is reachable on your network, the CLI's own `stream-json`
protocol is a real, documented interface — which is what this uses.

**Use Remote Control itself** (`claude --remote-control`, driven from the
browser) if what you want is that specific cloud session rather than one you
control. That is what it is for.

## Design decisions

What else was considered, and why it lost:

| Option | Why not |
| --- | --- |
| Playwright against claude.ai | See above — SPA scraping, Cloudflare, cookie replay, policy |
| `claude mcp serve` | Exposes Claude Code's *tools* (Read/Edit/Bash) to a client — it does not let you send a prompt to a running conversation |
| Scheduled cloud triggers | Fire routines on a schedule; no way to drive a live session |
| Agent SDK | Correct for building a *new* agent; overkill when the CLI already speaks a streaming protocol |
| SSH + `claude -p --resume <id>` | Workable, but one process per command loses the warm prompt cache, and claude.ai session ids are not local session UUIDs |

The bridge won because `--input-format stream-json` accepts **realtime streaming
input**: one long-lived process can take prompt after prompt. That single fact
is what makes persistent remote context possible.

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

## Bidirectional: both machines driving each other

The layout above is one-way. Make it symmetric by running **both** components on
**both** machines — a bridge (so it can be driven) and the MCP server (so it can
drive). Each machine's `hosts.json` lists the other. Either side can then start
an exchange.

Set `CLAUDE_RELAY_SELF` on each machine to the name its peers know it by; it
defaults to the hostname.

### Letting them talk until one needs you

`remote_converse` shuttles messages between two machines with no human in
between: the opening goes to the first host, its reply to the second, that reply
back to the first. It stops on the **first** halt condition:

| Stop | Meaning |
| --- | --- |
| `needs_user_input` | A session emitted `NEEDS-USER-INPUT` — it wants a decision, credential, or approval from you |
| `complete` | A session emitted `CONVERSATION-COMPLETE` |
| `turn_error` | A turn errored or was interrupted |
| `error:<code>` | Transport failure, e.g. the peer went offline |
| `max_turns` | The cap (default 6) was reached |

Each side is briefed once, on its first message, on how to emit those tokens.

A denied tool call is **reported, not fatal** — the count appears in the
transcript entry and the exchange continues. A session that routes around a
sandbox restriction has not asked for a human; only it can judge that, and it
has `NEEDS-USER-INPUT` to say so. Halting on every denial killed healthy
conversations in testing.

```bash
relay.py converse remote local --max-turns 6 \
  --opening "Compare your checkouts of the parser and agree which is ahead."
```

From a session, just ask: *"have `remote` and `local` work out which checkout is
ahead, and stop when they need me."*

**Every hop is a real turn on a real machine, billed on both.** Keep `max_turns`
low. The CLI prints running cost, and the tool reports the total.

### Watching them type

Turn-granular output means nothing appears until a hop finishes — which can be
half a minute of silence. `--stream` renders each machine's text as it is
produced:

```bash
relay.py converse remote local --stream --max-turns 4 --opening "…"
```

```
local │ Hello local2 — what's the working directory you're operating in?
  [local done: 3.8s, $0.1121]

  · local2 runs Bash
local2 │ I'm operating in `/home/guido/relay-workspace2`. It is not a git repo…
  [local2 done: 10.5s, $0.1323, 1 denied]
```

A streamer follows whichever machine holds the turn. Tool calls appear as they
are invoked, so a long silent stretch is visibly *work* rather than a hang.

To watch one machine on its own — useful in a second terminal while something
else drives it:

```bash
relay.py --host remote stream            # add --thinking for reasoning
```

This is a **human-facing** feature. `remote_converse` over MCP still returns one
transcript at the end, because a tool result cannot stream into a model's
context mid-call. Use the CLI when you want to watch.

**How it works.** The bridge runs its child with `--include-partial-messages`
and exposes `GET /stream` as server-sent events. Partial deltas are fanned out
to watchers but deliberately kept **out** of the history deque — one streamed
turn would otherwise evict the real events that `/transcript` and turn
summarising depend on. Each watcher gets a bounded queue; one too slow to keep
up drops frames rather than stalling the session. Set `CLAUDE_BRIDGE_STREAM=0`
to disable partial messages entirely.

### Loop protection

Two machines that can each drive the other can bounce a prompt back and forth
forever. Every request carries the chain of machines that already relayed it:

```
orchestrator -> alpha -> beta        # beta may not now relay to alpha
```

A bridge publishes the chain of the turn it is running to a state file
(`~/.cache/claude-remote-relay/inbound.json`), which that machine's own MCP
server reads before forwarding — that is how an inbound chain reaches the
outbound side across two processes. Forwarding to a machine already in the chain
fails with `relay_loop`; exceeding `CLAUDE_RELAY_MAX_DEPTH` (default 3) fails
with `max_depth_exceeded`. Markers older than an hour are ignored, so a crashed
bridge cannot poison later local calls.

`remote_converse` does not rely on this: one orchestrator drives both sides in a
bounded loop, which is inherently safer than mutual recursion. The chain is the
backstop for the case where a session calls `remote_prompt` on its own.

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
$env:CLAUDE_BRIDGE_NAME  = "remote"
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
  "default": "remote",
  "hosts": {
    "remote": { "url": "http://100.100.100.10:8787", "token": "…", "description": "the machine with the GPU and the build toolchain" },
    "local":  { "url": "http://100.100.100.20:8787", "token": "…", "description": "the laptop you sit at" }
  }
}
```

Host names are yours to choose. The examples throughout use `remote` and
`local`; in code spans those are **host names**, while plain "remote" and
"local" in prose mean the ordinary words. Hostnames of your own (`workshop`,
`gpu-box`) avoid the ambiguity entirely.

Config is searched at `$CLAUDE_RELAY_CONFIG`, then
`$CLAUDE_PLUGIN_DATA/hosts.json`, then `~/.config/claude-remote-relay/hosts.json`.
**That file holds bearer tokens — `chmod 600` it.** `hosts.json` is gitignored.

## Usage

Just talk: *"ask `remote` what's failing in the test suite"*. Claude routes to the
tools on its own. Slash commands are faster when you know what you want:
`/relay-peers`, `/relay-send remote <prompt>`, `/relay-interrupt`.

### Tools

| Tool | What it does |
| --- | --- |
| `remote_peers` | List configured machines with live health |
| `remote_prompt` | Send a prompt; `background: true` returns a job id instead of blocking |
| `remote_converse` | Let two machines talk to each other until one needs you |
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
relay.py --host local send "check disk usage"
git diff | relay.py send -                        # pipe stdin
JOB=$(relay.py send -b "full regression run")     # background
relay.py result "$JOB" --wait 600
relay.py converse remote local --opening "agree on a plan" --max-turns 4
relay.py converse remote local --stream --opening "…"   # watch them type
relay.py --host remote stream                     # watch one machine, live
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
| `relay_loop` | Refused to forward to a machine already in the relay chain |
| `max_depth_exceeded` | Chain hit `CLAUDE_RELAY_MAX_DEPTH` (default 3) |
| `bad_args` | Missing or wrong-typed argument |
| `bad_msg` | Malformed JSON body |

## Protocol reference

The `stream-json` shapes this depends on, confirmed against Claude Code 2.1.222.
Useful if you extend the bridge.

**Sending** — one NDJSON line on the child's stdin. `content` accepts a plain
string; the block-array form works too:

```json
{"type":"user","message":{"role":"user","content":"…"}}
```

**Receiving** — NDJSON on stdout. Event types seen in one turn, in order:

| `type` | Notes |
| --- | --- |
| `rate_limit_event` | Emitted at startup; carries reset time and overage status |
| `system` (`subtype: init`) | Session id, cwd, model, tool list, MCP server states |
| `assistant` | Content blocks: `text`, `tool_use`. Multiple per turn |
| `user` | Tool results echoed back |
| `control_response` | Ack for a `control_request` |
| `result` | **Exactly one, ends the turn.** `result`, `is_error`, `session_id`, `total_cost_usd`, `duration_ms`, `permission_denials` |

`result` is the turn boundary — that is what makes request/response framing
possible over a streaming protocol.

**Interrupting** — same stdin channel:

```json
{"type":"control_request","request_id":"…","request":{"subtype":"interrupt"}}
```

The child replies `{"type":"control_response","response":{"subtype":"success",…}}`
and then emits a `result` with `is_error: true`. Text produced before the
interrupt is still in the `assistant` events, which is why the bridge keeps
`assistant_text` separately from `result`.

**Partial messages.** With `--include-partial-messages` the child also emits
`stream_event` wrappers around the standard Anthropic streaming shapes —
`message_start`, `content_block_start`, `content_block_delta`
(`delta.text_delta.text` is the typed text), `content_block_stop`,
`message_delta`, `message_stop`. The bridge's `/stream` frames are
double-wrapped: `{"index":N,"event":{"type":"stream_event","event":{…}}}`.

Required flags: `--input-format stream-json` needs `--print` and
`--output-format stream-json`; `--verbose` is what surfaces the `system` init
event carrying the session id.

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
- two bridges conversing: messages alternating between them, each side's reply
  forwarded verbatim to the other
- halting on `NEEDS-USER-INPUT` (a session refused a destructive request and
  escalated) and on `CONVERSATION-COMPLETE`
- loop protection: relay-back blocked, onward relay allowed, depth limit
  enforced, stale chain markers expired by TTL
- a bridge publishing its inbound chain during a turn and clearing it after
- SSE streaming: token deltas and tool markers arriving live, and a full
  `converse --stream` exchange rendered as it was typed
- streaming **across a real tailnet**, Windows <-> Linux: a 40s idle SSE
  connection held with 15s keepalives, then a live two-machine exchange

Run in anger on Windows 11 (`claude.exe`, PowerShell tooling) driven from Linux
over Tailscale. Still unexercised: the `.cmd` shim wrapper in `child_argv`, since
that install used `claude.exe` directly.

`/health` reports **`bridge_pid`** and **`child_pid`** separately — they are two
processes, and a single `pid` field invites matching the wrong one against
`ps` / `Get-Process`.

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
