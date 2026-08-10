---
name: restart
description: This skill should be used when the user asks to "restart your session", "restart yourself", "reload your session", or when the session itself determines a restart is needed (e.g. after a plugin update or harness change that requires a fresh process). Restarts the current Claude Code session in place via /exit + claude --resume, preserving the full conversation.
argument-hint: "[session-id]"
allowed-tools: Bash
version: 1.0.0
---

# Restart Own Session

The session cannot restart itself directly — its own death kills its Bash tool.
The bundled script delegates a driver to the tmux server, which outlives the
session: it waits for the current turn to end, types `/exit`, relaunches
`claude --resume <sid>` in the same pane, and types a wake-up message so the
resumed session reports back.

## Procedure

1. Resolve the tool path from this skill's base directory:
   `../../scripts/cc-self` (i.e. `<plugin-root>/scripts/cc-self`). Invoke it
   through `bash` — plugin files may lose the executable bit on install.
2. Resolve the session id: use the argument if provided, otherwise run
   `bash <plugin-root>/scripts/cc-self sid` (uses `$CLAUDE_SESSION_ID`, falling
   back to the newest transcript in `~/.claude/projects/<encoded-cwd>/`).
3. Sanity-check before arming:
   - `$TMUX_PANE` is set (the session must run inside tmux)
   - `~/.claude/projects/<encoded-cwd>/<sid>.jsonl` exists
   - No critical background task is mid-flight (it would be killed)
4. Arm the driver:
   ```bash
   bash <plugin-root>/scripts/cc-self restart <sid>
   ```
5. **End the turn promptly** with a one-line notice to the user (expected
   downtime ~15s after the turn ends, plus the driver's turn-end detection).
   Long streaming after arming only delays the sequence.
6. On the wake-up turn (message from "[cc-self restart driver]"), verify
   continuity — conversation memory intact, statusline state preserved — and
   report the result to the user.

## Notes

- The driver preserves permission mode: it adds `--dangerously-skip-permissions`
  only if the footer showed "bypass permissions on" before exiting.
- It launches the real claude binary directly (aliases with extra flags like
  `--effort` would silently change session state).
- If the restart stalls, inspect `~/.cc-self.log` — every driver phase is
  logged. Manual recovery: `claude --resume <sid>` in the pane.
