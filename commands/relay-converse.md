---
description: Let two machines talk to each other until one needs you
argument-hint: <host-a> <host-b> <opening message>
allowed-tools: mcp__remote-relay__remote_converse, mcp__remote-relay__remote_peers
---

Start a conversation between two machines with `mcp__remote-relay__remote_converse`.

Read `$ARGUMENTS` as: the first two words are the host names, in speaking order; everything after them is the opening message. Pass them as `hosts` and `opening`. Use `mcp__remote-relay__remote_peers` first only if a name looks wrong.

Leave `max_turns` at its default unless the user asked for a specific number — each turn costs real tokens on both machines.

Report the transcript and say plainly why it stopped. If it stopped because a session needs the user, lead with what it is waiting on.
