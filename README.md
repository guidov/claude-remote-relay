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

### Talking to one machine, live

`chat` is the interactive loop — type a prompt, watch the reply arrive as it is
typed, repeat. Ctrl-C interrupts the running turn without leaving; Ctrl-D exits.

```bash
relay --host remote chat
```

```
[chat with remote — Ctrl-D to exit, Ctrl-C to interrupt a turn]

remote> which listening ports are mine?
  · remote runs Bash
remote │ 22 is sshd, 8787 is the relay bridge you're talking over…
  [15.8s · $0.5280]

remote>
```

For a single prompt, `send --stream` does the same thing and exits:

```bash
relay --host remote send --stream "run the tests and summarize failures"
```

To watch a machine *without* driving it — a second terminal, while something
else sends the prompts:

```bash
relay --host remote stream            # add --thinking for reasoning
```

`stream` shows **both sides**: prompts arriving (tagged with the machine that
sent them) and the replies as they are typed. The child process never echoes
its own input, so the bridge publishes each prompt itself as a `relay.prompt`
event — without it a watcher would see only half the conversation.

```
xxx → run the tests and summarize failures
  · remote runs Bash
remote │ Two failures, both in the parser…
  [remote done: 12.4s, $0.31]
```

`chat` only renders its own turns — while it sits at the prompt it is blocked on
your keyboard, not watching. Use `stream` in a second terminal to see traffic
driven by anyone else.

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

## Running it as a service

A bridge started from a terminal — or from a Claude Code session — dies with
whatever started it. That inverts the point of the tool: the machine is only
reachable when someone is already sitting at it. For a peer you actually want to
drive, run the bridge detached.

**Decide first whether you want this.** An always-on bridge is a standing remote
code execution surface on that machine, with `acceptEdits` and no one watching.
That is a real change from "it exists while I'm here". Scope `CLAUDE_BRIDGE_CWD`
to one directory, keep the bind address on the tailnet, and consider
`--permission-mode plan` or a stricter mode for an unattended host.

### Windows (Task Scheduler)

Scheduled tasks do not inherit a shell's environment, so persist the settings at
user scope first, then point a task at the script:

```powershell
# SetEnvironmentVariable, not setx: setx truncates at 1024 chars and mangles
# trailing backslashes, which a Windows path is a natural candidate for.
[Environment]::SetEnvironmentVariable("CLAUDE_BRIDGE_TOKEN_FILE", "$HOME\.claude-relay-token", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_BRIDGE_HOST", "100.100.100.10", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_BRIDGE_NAME", "remote", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_BRIDGE_CWD",  "C:\path\to\the\project", "User")

$settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `   # default is 3 days, then killed
  -MultipleInstances IgnoreNew `             # never fight over the port
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$action  = New-ScheduledTaskAction -Execute "python" `
             -Argument "$HOME\claude-remote-relay\bridge\claude_bridge.py"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "claude-relay-bridge" `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
```

Three things that bite if you skip them:

- **`ExecutionTimeLimit` defaults to 3 days**, after which the task is killed.
  A bridge that dies silently after 72 hours is the same invisible failure as an
  amnesiac restart, arriving by another road. `[TimeSpan]::Zero` is unlimited.
- **`MultipleInstances IgnoreNew`** stops a second instance fighting for 8787 if
  the trigger fires while one is already up. It is also what makes the repeating
  heal trigger below safe.
- **`claude` must be findable from the service's PATH**, not just your shell's.
  Check with `[Environment]::GetEnvironmentVariable("PATH","User")` before
  registering. The bridge falls back to the usual install locations and you can
  name it outright with **`CLAUDE_BRIDGE_CLAUDE_BIN`**, which is the reliable
  answer for any service. This is not Windows-specific: a non-interactive Linux
  shell gets a PATH without `~/.local/bin`, so `shutil.which("claude")` returns
  `None` on a machine where `claude` is plainly installed.

Verify the environment actually reached the task by reading `permission_mode`
back from `/health`: it can only be right if inheritance worked.

Use `-AtStartup` with stored credentials if you need it up before anyone logs in.

#### Restart-on-failure does not restart a dead process

Task Scheduler's `RestartCount` / `RestartInterval` apply when the task **fails
to launch** — not when the launched process exits. Kill the bridge and the
action has already succeeded: the task records a result and considers itself
finished. It never comes back. This is *not* the counterpart to systemd's
`Restart=on-failure`, which genuinely restarts.

Measured: `RestartCount 3 / RestartInterval PT1M`, bridge killed, port still
down after 150s, task state `Ready`, last result `0xFFFFFFFF`.

Add a **repeating trigger** instead, and let `MultipleInstances IgnoreNew` do
the work — if the bridge is up the launch is dropped, if it died the trigger
starts it:

```powershell
$logon  = New-ScheduledTaskTrigger -AtLogOn
$heal   = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes 1)
Set-ScheduledTask -TaskName "claude-relay-bridge" -Trigger $logon, $heal
```

Recovery is then bounded by the repetition interval — measured at ~50s to heal.
NSSM gives real service semantics if you would rather have them; Task Scheduler
avoids the extra dependency but needs this pattern to be self-healing at all.

Clearing a recovery policy wants both fields gone: setting `RestartCount = 0`
while `RestartInterval` is still set fails with `The task XML is missing a
required element or attribute (53,8):Count`. Drop both, or re-register clean.

#### Give it somewhere to log

A service manager discards stdout, so an always-on bridge has no forensics —
you cannot see `resuming saved session` or a child's stderr. Set
**`CLAUDE_BRIDGE_LOG`** and the bridge writes its own dated log, no shell
wrapper needed:

```powershell
[Environment]::SetEnvironmentVariable("CLAUDE_BRIDGE_LOG", "$HOME\claude-relay.log", "User")
```

It rotates to `.1` at `CLAUDE_BRIDGE_LOG_MAX_BYTES` (default 5 MB), so it will
not grow without bound on a machine nobody is watching.

An unhandled exception is written there too, with its traceback — a bridge that
dies at boot under a service manager otherwise leaves nothing to debug.

#### Starting before the network is ready

A service can start before its tailnet address exists, and the bind then fails
with `EADDRNOTAVAIL` (POSIX) or `WinError 10049` — the process dies seconds
after boot having never served. The bridge retries the bind for
`CLAUDE_BRIDGE_BIND_RETRY_S` (default 180) instead of exiting.

The bind also happens **before** the `claude` child is spawned, so a bind that
does fail cannot leave an orphaned child behind with nothing to serve it.

### Linux (systemd user unit)

A tested unit ships at [`contrib/claude-relay-bridge.service`](contrib/claude-relay-bridge.service).
Copy it, edit the `Environment=` lines, and:

```bash
cp contrib/claude-relay-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-relay-bridge
loginctl enable-linger "$USER"    # survive logout; without this it stops
```

Three choices in that file worth knowing:

- **`Restart=always`, not `on-failure`.** A clean exit still leaves the peer
  unreachable, and there is no such thing as a bridge that is meant to stop on
  its own. An explicit `systemctl stop` is still honoured.
- **`CLAUDE_BRIDGE_CLAUDE_BIN`** is set outright. A systemd user unit gets a
  minimal PATH with no `~/.local/bin`, which is exactly where `claude` usually
  lives.
- **`CLAUDE_BRIDGE_LOG`** is set, because `systemctl --user` capture is not
  where you will look first and an always-on bridge needs its own forensics.

Binding to a tailnet address before Tailscale is up just fails and retries every
`RestartSec`, so no extra ordering is needed.

Over SSH, `systemctl --user` needs `export XDG_RUNTIME_DIR=/run/user/$(id -u)`
in a non-interactive shell.

### The conversation survives a restart

A bridge saves its `session_id` and resumes it on startup, so a service restart
does not hand the next caller an amnesiac peer. State lives beside the loop
chain (`~/.cache/claude-remote-relay/session-<port>.json`) and is keyed by port,
so several bridges on one machine keep separate conversations.

Two guards, because both failure modes are silent otherwise:

- A saved session is **ignored if `CLAUDE_BRIDGE_CWD` has changed** — resuming it
  elsewhere would hand the peer a conversation about a different project.
- If the id no longer exists (session store pruned), the child exits with
  `No conversation found`; the bridge logs it, clears the file and starts fresh.
  Without that, a stale id would be retried on every restart and the bridge
  would never come up.

`restart --fresh` clears the saved id too, so a deliberate reset is not undone
by the next reboot.

`/health` answers "did my context survive?" directly, via **`session_state`**:

| `session_state` | `session_id` | `resuming` | Meaning |
| --- | --- | --- | --- |
| `fresh` | `null` | `null` | New conversation, nothing to resume |
| `resuming` | `null` | the saved id | Restarted, resume armed, awaiting first turn |
| `active` | the id | `null` | Child has confirmed the session |

`session_id` comes from the child's init event, which does not arrive until the
first prompt — so it is blank in the window right after a restart, which is
precisely when an operator looks. On its own that reads as the amnesia
persistence exists to prevent. `resuming` is known at startup and says otherwise.

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

Put it on your PATH once — the entry points resolve their imports through
`realpath`, so a symlink works from anywhere:

```bash
ln -s /path/to/claude-remote-relay/cli/relay.py ~/.local/bin/relay
```

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
- Linux -> Linux across the tailnet between two different machines, streamed,
  alongside the Windows <-> Linux path
- SSE streaming: token deltas and tool markers arriving live, and a full
  `converse --stream` exchange rendered as it was typed
- streaming **across a real tailnet**, Windows <-> Linux: a 40s idle SSE
  connection held with 15s keepalives, then a live two-machine exchange

Run in anger on Windows 11 (`claude.exe`, PowerShell tooling) driven from Linux
over Tailscale. Still unexercised: the `.cmd` shim wrapper in `child_argv`, since
that install used `claude.exe` directly.

`/health` names the bridge and its child separately — **`bridge_pid`** /
**`child_pid`**, **`bridge_uptime_s`** / **`child_uptime_s`**. They are two
processes with different lifetimes: a `restart` resets the child's while the
bridge's keeps climbing. A single `pid` or `uptime_s` field invites matching the
wrong one against `ps` / `Get-Process` and reading a correct answer as a stale
one.

Watcher disconnects are caught as **`ConnectionError`**, not a tuple of specific
subclasses. "The peer went away" is `BrokenPipeError` or `ConnectionResetError`
on POSIX but `ConnectionAbortedError` (WinError 10053) on Windows; they are
siblings, so any tuple of two misses the third and the traceback escapes to
`socketserver` — once per watcher hangup, i.e. every `stream` Ctrl-C.

## Security

The bridge runs Claude Code with file and shell access on the remote machine.
Anyone who can reach the port and holds the token can make it edit files and run
commands. Treat it as remote code execution, because it is.

- Token required, 24 characters minimum, compared with `hmac.compare_digest`.
- **Prefer `CLAUDE_BRIDGE_TOKEN_FILE` over `CLAUDE_BRIDGE_TOKEN`**, especially for
  an always-on service. An environment variable is inherited by every child the
  bridge spawns — the `claude` process and everything it shells out to — so the
  secret sits in each of their environment blocks. A *path* is not a secret, and
  the file it names can be locked to one account. This is strictly better than a
  config file, which an env var is not.
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
