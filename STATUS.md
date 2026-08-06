# Status — 2026-08-05

Working across three machines on a Tailscale network. 2,143 lines, no
third-party dependencies on either side.

| Machine | OS | Role | Version | Deployment |
| --- | --- | --- | --- | --- |
| `xxx` | Linux | drives, and is drivable | 0.9.0 | `setsid` (dies on reboot) |
| `bloc` | Windows 11 | drivable; receive-only | 0.8.1 | Task Scheduler, logon + 1-min heal trigger |
| `greyai` | Linux | drivable; self-drive only | 0.8.1 | systemd user unit, lingering |

## Verified on real hardware

- Cross-machine prompting, Windows <-> Linux and Linux <-> Linux over the tailnet
- Persistent conversation: same `session_id` survives SIGKILL, service restart,
  and hours of idle
- Token-level streaming both directions, including the prompt that caused it
- Interactive `chat`, and `converse` running two machines against each other
  until one emits `NEEDS-USER-INPUT`
- Loop protection: relay-back blocked, onward relay allowed, depth limit,
  TTL expiry, and depth-0 self-call permitted
- Stale-session fallback (planted a dead id; bridge cleared it and came up)
- Boot-time bind retry, and fatal exceptions logged with traceback
- systemd `Restart=always` genuinely restarts; Task Scheduler's
  `RestartCount` does **not** — a repeating trigger is required instead

## Gaps

**No automated tests.** Everything above was verified by hand, once, and is
now unguarded. The pure-logic pieces — `outbound_chain`, `read_saved_session`,
log rotation, the alias/`self_name` drift check — test without a network and
are the highest-value thing to add.

**The plugin install path has never been run.** `/plugin marketplace add` ->
`/plugin install` is the README's headline instruction and nobody has executed
it. Manifests exist and `scripts/check_version.py` keeps their versions in
step, but the flow itself is unverified.

**The MCP surface lags the CLI** — 8 tools against 11 commands. `chat` and
`stream` are inherently human-facing, but `jobs` has no tool equivalent. Nearly
all recent exercise has gone through the CLI.

## Known and parked

- `bloc`'s `-AtLogOn` fired correctly after a reboot but the bridge died at
  startup; 0.8.1 makes the next boot decisive. Its task action still routes
  through `cmd.exe` for stderr capture, which breaks `Stop-ScheduledTask` —
  revert once in-process logging proves itself.
- `bloc` and `greyai` predate `self_name` in `/health`, so `relay peers`
  cannot yet check their naming.
- The `.cmd` shim branch in `child_argv` has never executed (that install
  resolves `claude.exe`).
- On Windows a child has been seen outliving a killed bridge; on Linux it exits
  within a second. A restarted Windows bridge can briefly race an old child.
- `bloc` and `greyai` hold no peer credentials by choice, so neither can drive
  the others. Adding them means copying bridge tokens onto those hosts.
