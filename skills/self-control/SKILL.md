---
name: self-control
description: This skill should be used when the session needs to perform a user-side action on its own Claude Code TUI — run a slash command on itself ("/reload-plugins", "/effort low", "/model", "/plugin install ..."), answer its own confirmation dialogs, observe its own screen, or when the user asks to "change your own effort", "reload plugins yourself", "install this plugin yourself", "control your own session", "type into your own TUI", or "restart your session" (for restart, prefer the cc-self:restart skill).
version: 1.0.0
---

# cc-self — Session Self-Control

## How it works

The Bash tool runs inside the same tmux pane as the Claude Code TUI. Sending
keystrokes to that pane (`$TMUX_PANE`) with `tmux send-keys` is typing into the
session's own input box. The bundled `scripts/cc-self` wraps this safely.

Resolve the tool path from this skill's base directory: `../../scripts/cc-self`
(i.e. `<plugin-root>/scripts/cc-self`). Invoke it as `bash <plugin-root>/scripts/cc-self …`
— plugin files are not guaranteed to keep the executable bit after install, so
calling it through `bash` avoids a permission error. Requires tmux; fails
cleanly outside it. The table below abbreviates this as `cc-self`.

## Subcommands

| Command | Effect |
|---|---|
| `cc-self type "/reload-plugins"` | Type text + Enter |
| `cc-self type -n "text"` | Type without Enter (stage only) |
| `cc-self key Enter` | Send one special key (Escape, Up, C-u, ...) |
| `cc-self clear` | Empty the input box (backspace burst) |
| `cc-self peek [N]` | Show last N lines of own screen (default 20) |
| `cc-self pane` | Show target pane info |
| `cc-self sid` | Print current session id |
| `cc-self restart [sid]` | Restart own session (see cc-self:restart skill) |
| `--pane %N` | Target another pane's session instead of self — for operating that session's TUI on the user's behalf (peek, dialogs, slash commands). **Not a messaging channel**: never `type` content addressed to another session's model. Its input box submits under the USER's name (autofill can even pre-stage text you never sent), so a typed "message" becomes a forged user turn. Session-to-session delivery belongs to messaging tools (SendMessage, khala), not this one. |

## Core patterns

**Run a slash command on self:**
```bash
bash <plugin-root>/scripts/cc-self type -w "/reload-plugins"
```
Bare commands typed mid-turn are queued and execute when the turn ends.
**Slash commands never start a model turn by themselves** — their local-command
record is only delivered at the next real invocation. To continue working after
the command, use `-w/--wake`: it arms a detached waker under the tmux server
that waits for the current turn to end (and queued commands to drain), then
types a wake-up message, which starts a turn. (A wake message cannot simply be
queued inline: mid-turn, the TUI injects queued plain text into the running
turn while slash commands stay queued — the wake would be consumed early.) Commands **with arguments** (e.g.
`/effort low`) execute immediately even mid-turn and may open a confirmation
dialog.

**Mid-turn execution caveats** (verified empirically): the immediate/dialog
path leaves **no transcript record** — the user sees nothing in the
conversation, only side effects (this is why `~/.cc-self.log` matters), and a
command can occasionally be dropped silently. For plugin/marketplace
management prefer the headless CLI (`claude plugin install|update ...`,
`claude plugin marketplace add ...`) and verify side effects on disk; reserve
TUI typing for live-session-only commands (/effort, /model, /reload-plugins).

**Handle a dialog — never press keys blind:**
```bash
cc-self type "/effort low"   # opens confirm dialog immediately
sleep 1; cc-self peek 8      # LOOK: which option is highlighted?
cc-self key Enter            # confirm only after seeing the dialog
```
Use `key Up` / `key Down` to move between options; `key Escape` cancels a
dialog (safe when a dialog is open — do not send Escape otherwise: it
interrupts the session's own running turn).

**Verify state:** the statusline (visible via `peek`) shows model/effort/mode.
Check it after any state-changing command.

## Caveats

- **vim keybindings**: handled automatically — when the pane shows
  `-- NORMAL --`, the script sends `i` first. Typing into NORMAL mode gets
  eaten as vim commands ("cc" is change-line).
- **Effort/model switches invalidate the conversation cache** — the full
  history is re-read on the next message. Do not toggle casually.
- **Guardrails**: `type` refuses nothing but sends text only; control keys go
  through `key` deliberately. Do not use this tool to self-approve permission
  dialogs unless the user has explicitly delegated that.
- Every send is logged to `~/.cc-self.log` (override with `$CC_SELF_LOG`).
