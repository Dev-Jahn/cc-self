---
name: restart
description: This skill should be used when the user asks to "restart your session", "restart yourself", "reload your session", or when the session itself determines a restart is needed (e.g. after a plugin update or harness change that requires a fresh process). Restarts the current Claude Code session in place via /exit + claude --resume, preserving the full conversation.
argument-hint: "[session-id] [-- extra-resume-flags]"
allowed-tools: Bash
version: 1.1.5
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
   The driver pins the session's current model and effort onto the resume
   command automatically. To deliberately change them (or pass any other
   resume flag), append them after `--` — they replace the pinned ones:
   ```bash
   bash <plugin-root>/scripts/cc-self restart <sid> -- --effort max
   ```
5. **End the turn promptly** with a one-line notice to the user (expected
   downtime ~15s after the turn ends, plus the driver's turn-end detection).
   Long streaming after arming only delays the sequence.
6. On the wake-up turn (message from "[cc-self restart driver]"), verify
   continuity — conversation memory intact, statusline state preserved — and
   report the result to the user.

## Notes

- The driver preserves model and effort: at arm time (while the session is
  alive) it reads them from the transcript's last assistant entry (fallback:
  `~/.claude/settings.json`) and passes `--model`/`--effort` explicitly on
  resume. A bare `--resume` would reset effort to the default, and the changed
  system prompt would break the warm prompt cache. Setting effort via the TUI
  after resume is too late for the same reason — resume-time CLI args are the
  only cache-safe path.
- Suffixed model variants are handled: transcripts record the bare API id, so
  when settings names a suffixed form of the same model (e.g.
  `claude-fable-5[1m]` for 1M context) the driver pins that form. All typed
  flag values — pinned and `--` passthrough alike — are single-quoted when
  typed, because the resume command lands in a live zsh where a bare `[1m]`
  is a glob ("no matches found" and the relaunch never runs). Pass raw values
  after `--`; the driver quotes each token itself.
- The driver preserves permission mode: it adds `--dangerously-skip-permissions`
  only if the footer showed "bypass permissions on" before exiting.
- If background tasks are running, `/exit` opens a "Background work is running"
  confirm dialog instead of exiting; the driver detects it and confirms the
  preselected "Exit and stop tasks". This is why step 3 checks for critical
  background work before arming — anything still running is stopped.
- `/exit` is staged, not typed blind: at idle the leading `/` opens the
  command palette, which can swallow same-tick characters (a live incident
  submitted the stray remainder as a fake user message). The driver paces
  the send and presses Enter only after the input box visibly holds `/exit`;
  if staging fails three times it aborts with the session left intact.
- It launches the real claude binary directly (aliases with extra flags
  would silently change session state).
- If the restart stalls, inspect `~/.cc-self.log` — every driver phase is
  logged. Manual recovery: `claude --resume <sid>` in the pane.
