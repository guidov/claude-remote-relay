---
description: Send a prompt to a remote Claude Code session
argument-hint: [host] <prompt>
allowed-tools: mcp__remote-relay__remote_prompt, mcp__remote-relay__remote_peers
---

Send `$ARGUMENTS` to a remote session with `mcp__remote-relay__remote_prompt`.

If the first word of `$ARGUMENTS` names a configured host, pass it as `host` and send the rest as the prompt. Otherwise send the whole of `$ARGUMENTS` as the prompt and let the default host handle it. Use `mcp__remote-relay__remote_peers` only if you need to check whether that first word is a host name.

Report the remote session's reply verbatim. Do not add commentary.
