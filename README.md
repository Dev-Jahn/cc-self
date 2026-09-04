# cc-self

Self-control for Claude Code sessions running in **tmux**: the session types
into its own TUI. Slash commands, dialog navigation, screen observation, and
full in-place self-restart — no extra daemons, just tmux primitives.

## How it works

The session's Bash tool runs inside the same tmux pane as the Claude Code TUI.
`tmux send-keys` to `$TMUX_PANE` is literally typing into the session's own
input box; `capture-pane` lets it see its own screen; `run-shell -b` gives it
a process that survives its own death (used for restart).

## Install

```
/plugin marketplace add Dev-Jahn/cc-self        # or a local path
/plugin install cc-self@cc-self-marketplace
```

## What you get

- **`self-control` skill** (auto-triggers): teaches the session to run
  user-side slash commands on itself (`/reload-plugins`, `/effort`, `/model`,
  `/plugin install …`), answer its own confirmation dialogs (peek → key), and
  verify its own state via the statusline.
- **`/cc-self:restart` command**: the session restarts itself in place —
  a driver delegated to the tmux server waits for the turn to end, types
  `/exit`, relaunches `claude --resume <session-id>` in the same pane, and
  wakes the resumed session with a message so it reports back. Conversation
  history is fully preserved. Downtime ≈ 15 seconds. The session's current
  model and effort are captured before exit and pinned onto the resume
  command (`--model`/`--effort`) so effort survives the restart and the
  prompt cache stays warm; `restart <sid> -- <flags>` appends extra resume
  flags verbatim (replacing the pinned ones they name, e.g. `-- --effort max`).
- **`model-recovery` skill + `model-guard` hook + `cc-self recover`**:
  deterministic recovery from model safety-fallbacks. The bundled hook
  re-flags on every run while the session runs below the baseline declared in
  `~/.claude/settings.json`, embedding the exact recovery step (and stays
  silent when no `model` is declared there — the default-model setting leaves
  no baseline to be below). The model's
  only job is writing a compact instruction file (preserve context, abstract
  the trigger content); `cc-self recover` then submits `/compact` and a
  detached driver waits out the compaction, switches `/model` back, approves
  the confirm dialog only after seeing it (a switch the TUI applies without a
  dialog is accepted once the command is consumed), wakes the session, and
  verifies the live model from the session transcript (statuslines are
  user-configurable, so screen checks are advisory only) — a state machine
  with per-session state in `~/.cc-self/state/recover-<sid>.json`, no blind
  keypress at any step. The guard tells a session that FELL from the baseline
  (compact-first) apart from one that merely STARTED below it — e.g. the
  baseline was raised to a newer release after launch — which gets a plain
  `cc-self recover --switch-only` first, and it treats a record older than
  the current settings.json (the user just ran `/model`) as stale rather than
  as a fallback.
  Sessions with an always-blocking Stop hook integrate via the yield-valve
  flag (`~/.cc-self/state/yield-<sid>`, see the model-recovery skill). If an
  external copy of the guard also runs, keep only one enabled
  (`~/.cc-self/state/guard-disabled` disables the bundled one).
- **`scripts/cc-self`**: the underlying CLI, usable directly from Bash.

## Requirements

- The Claude Code session must run **inside tmux** (`$TMUX_PANE` set)
- macOS or Linux, `bash`, `tmux`

## Safety design

- `type` sends text only; control keys (Escape, C-c, …) require the explicit
  `key` subcommand — prevents accidentally interrupting the session's own turn.
- vim keybinding mode is detected (`-- NORMAL --` on screen) and handled by
  entering INSERT first.
- Restart preserves permission mode (adds `--dangerously-skip-permissions`
  only when the session already showed bypass mode) and launches the real
  claude binary, bypassing user aliases that would alter flags.
- Every send is appended to `~/.cc-self.log` for audit.

Intended for trusted, self-administered machines (personal agent hosts).
Anyone who can write to the tmux socket can already type into your session —
this plugin does not change that boundary; it just makes the session itself a
first-class operator of it.
