#!/bin/bash
# Behavioural matrix for hooks/model-guard.py, run against a scratch HOME with
# synthetic transcripts (never the real ~/.claude). Every case is a field
# report from a live session or a review finding; keep them when refactoring.
#   bash tests/guard-matrix.sh            (exit 0 = all green)
set -e
cd "$(dirname "$0")/.."
G="$PWD/hooks/model-guard.py"
S="${TMPDIR:-/tmp}/cc-self-guard-matrix.$$"; rm -rf "$S"; mkdir -p "$S/.claude" "$S/.cc-self/state"
trap 'rm -rf "$S"' EXIT
OLD=2026-09-01T00:00:00Z
rec()  { printf '{"type":"assistant","timestamp":"%s","message":{"role":"assistant","model":"%s"}}\n' "$2" "$1"; }
mcmd() { printf '{"type":"user","timestamp":"%s","message":{"role":"user","content":"<command-name>/model</command-name>\\n<command-message>model</command-message>\\n<command-args>%s</command-args>"}}\n' "$2" "$1"; }
# shellcheck disable=SC2016  # the backticks are literal: the TUI echoes "Set model to `X`"
mout() { printf '{"type":"user","timestamp":"%s","message":{"role":"user","content":"<local-command-stdout>Set model to `%s` and saved as your default for new sessions</local-command-stdout>"}}\n' "$2" "$1"; }
note() { python3 -c 'import sys,json; d=sys.stdin.read().strip(); print(json.loads(d)["hookSpecificOutput"]["additionalContext"] if d else "")'; }
out()  { HOME="$S" python3 "$G" "$1" < /dev/null | note; }
ev()   { printf '{"transcript_path":"%s","hook_event_name":"%s"}' "$1" "$2" | HOME="$S" python3 "$G" | note; }
expect() { case "$2" in *"$1"*) echo "ok   $3" ;; *) echo "FAIL $3: got: ${2:0:400}"; exit 1 ;; esac; }
absent() { case "$2" in *"$1"*) echo "FAIL $3: unexpected '$1': ${2:0:300}"; exit 1 ;; *) echo "ok   $3" ;; esac; }
silent() { if [ -z "$2" ]; then echo "ok   $1"; else echo "FAIL $1 (not silent): ${2:0:300}"; exit 1; fi; }
age()  { python3 -c 'import os,sys,time; t=time.time()-float(sys.argv[2]); os.utime(sys.argv[1],(t,t))' "$1" "$2"; }
set_base() { printf '{"model":"%s"}' "$1" > "$S/.claude/settings.json"; age "$S/.claude/settings.json" 2592000; }
recst() { printf '{"sid":"%s","pane":"%%0","baseline":"claude-fable-5-1[1m]","label":"Fable 5.1","attempt":%s,"phase":"%s","note":"%s","pid":0,"ts":"t"}' "$1" "$2" "$3" "$4" > "$S/.cc-self/state/recover-$1.json"; }

echo '# no declared baseline'
echo '{}' > "$S/.claude/settings.json"; rec claude-fable-5-1 1 > "$S/a.jsonl"; silent "A1 nothing declared, first sight" "$(out "$S/a.jsonl")"
rec claude-opus-5 2 >> "$S/a.jsonl"; expect "No baseline is declared" "$(out "$S/a.jsonl")" "A2 transition reported once"; silent "A3 silent after" "$(out "$S/a.jsonl")"
echo '# sources, direction, evidence'
set_base 'claude-opus-5[1m]'; rec claude-fable-5-1 $OLD > "$S/b.jsonl"; silent "B live above baseline" "$(out "$S/b.jsonl")"
set_base 'claude-fable-5-1[1m]'; rec claude-sonnet-5 $OLD > "$S/b2.jsonl"; o="$(out "$S/b2.jsonl")"
expect "If this is a fallback and not a deliberate choice, recover now (attempt 1)" "$o" "B2 never seen on baseline → conditional"; expect "--baseline 'claude-fable-5-1[1m]'" "$o" "B2 directive carries --baseline"
silent "C CC_SELF_BASELINE beats settings" "$(CC_SELF_BASELINE='claude-sonnet-5' out "$S/b2.jsonl")"
expect "from CC_SELF_BASELINE" "$(CC_SELF_BASELINE=claude-fable-5 ev "$S/b2.jsonl" PostToolUse)" "F source named"
set_base 'claude-fable-5-1[1m]'; rec claude-fable-5-1 1 > "$S/d.jsonl"; silent "D1 on baseline" "$(out "$S/d.jsonl")"
rec claude-opus-5 2 >> "$S/d.jsonl"; o="$(out "$S/d.jsonl")"; expect "saw the session on Fable 5.1 before" "$o" "D2 switched away"; expect "Recover now (attempt 1)" "$o" "D3 unconditional"
printf 'claude-fable-5-1' > "$S/.cc-self/state/model-guard.e.state"; rec claude-opus-5 1 > "$S/e.jsonl"; expect "saw the session on Fable 5.1" "$(out "$S/e.jsonl")" "E 1.3.x bare-id state"
printf '{"last":"claude-fable-5-1","seen_on":"claude-fable-5-1"}' > "$S/.cc-self/state/model-guard.e2.state"; rec claude-opus-5 1 > "$S/e2.jsonl"; expect "saw the session on Fable 5.1" "$(out "$S/e2.jsonl")" "E2 1.4.0 string seen_on"
echo '# model families'
set_base 'sonnet'; rec claude-sonnet-5 $OLD > "$S/1.jsonl"; silent "1 alias sonnet (as /model writes it)" "$(out "$S/1.jsonl")"
set_base 'claude-haiku-4-5'; rec claude-haiku-4-5-20251001 $OLD > "$S/2.jsonl"; silent "2 dated snapshot" "$(out "$S/2.jsonl")"
set_base 'opusplan'; rec claude-opus-5 $OLD > "$S/3.jsonl"; silent "3 opusplan" "$(out "$S/3.jsonl")"
set_base 'fable'; rec claude-opus-5 $OLD > "$S/s2.jsonl"; out "$S/s2.jsonl" >/dev/null; rec claude-fable-5-1 2026-09-01T00:00:05Z >> "$S/s2.jsonl"; silent "S alias fable, transition opus→fable" "$(out "$S/s2.jsonl")"
echo '# recover states'
set_base 'claude-fable-5-1[1m]'; rec claude-opus-5 $OLD > "$S/4.jsonl"; recst 4 1 FAILED-SWITCH x; o="$(out "$S/4.jsonl")"; expect "operator's stated intent" "$o" "4 FAILED record = intent"; expect "Recover now (attempt 2)" "$o" "4 attempt 2"
rec claude-opus-5 $OLD > "$S/5.jsonl"; recst 5 1 DONE verified; o="$(out "$S/5.jsonl")"; expect "almost certainly STALE" "$o" "5 pre-switch reading = stale"; absent "what a safety fallback looks like" "$o" "5 stale: no interpretation"
rec claude-opus-5 $OLD > "$S/9.jsonl"; recst 9 2 CLOSED ""; expect "closed by operator decision" "$(out "$S/9.jsonl")" "9 CLOSED"
rec claude-opus-5 $OLD > "$S/x.jsonl"; recst x 4 DONE verified; age "$S/.cc-self/state/recover-x.json" 172800; o="$(out "$S/x.jsonl")"; absent "attempt 5" "$o" "X 2-day-old DONE ignored"; expect "(attempt 1)" "$o" "X attempt restarts"
rec claude-opus-5 $OLD > "$S/6.jsonl"; printf '{"last":5,"seen_on":null,"runs":"x","choice":"bad"}' > "$S/.cc-self/state/model-guard.6.state"; expect "This session runs as Opus 5" "$(out "$S/6.jsonl")" "6 corrupt state survives"
echo '# /model typed in the session'
set_base 'claude-fable-5-1[1m]'; { rec claude-opus-5 $OLD; mcmd 'claude-fable-5-1[1m]' 2026-09-01T00:00:10Z; mout 'Fable 5.1' 2026-09-01T00:00:10Z; } > "$S/p.jsonl"
o="$(out "$S/p.jsonl")"; expect "runtime model is settling" "$o" "P1 /model newer than last record"; absent "ecover now" "$o" "P1 no directive"
rec claude-fable-5-1 2026-09-01T00:00:20Z >> "$S/p.jsonl"; silent "P2 settled on baseline" "$(out "$S/p.jsonl")"
rec claude-fable-5-1 $OLD > "$S/u.jsonl"; silent "U0 on env baseline" "$(CC_SELF_BASELINE='claude-fable-5-1[1m]' out "$S/u.jsonl")"
{ mcmd 'sonnet' 2026-09-01T00:00:10Z; mout 'Sonnet 5' 2026-09-01T00:00:10Z; rec claude-sonnet-5 2026-09-01T00:00:20Z; } >> "$S/u.jsonl"
silent "U1 confirmed choice becomes the baseline" "$(CC_SELF_BASELINE='claude-fable-5-1[1m]' out "$S/u.jsonl")"
rec claude-haiku-4-5 2026-09-01T00:00:30Z >> "$S/u.jsonl"; o="$(CC_SELF_BASELINE='claude-fable-5-1[1m]' out "$S/u.jsonl")"
expect "Declared baseline: Sonnet 5 (sonnet, from /model in this session)" "$o" "U2 fallback below the choice"; expect "saw the session on Sonnet 5 before" "$o" "U2 switched"; expect "--baseline sonnet\`" "$o" "U2 recovers to the choice"; expect "/model Sonnet 5 switch" "$o" "U2 target label"
rec claude-sonnet-5 2026-09-01T00:00:40Z >> "$S/u.jsonl"; silent "U3 back on the choice" "$(CC_SELF_BASELINE='claude-fable-5-1[1m]' out "$S/u.jsonl")"
printf '{"model":"claude-fable-5-1[1m]"}' > "$S/.claude/settings.json"; o="$(out "$S/u.jsonl")"
expect "from ~/.claude/settings.json model" "$o" "U4 settings written later wins"; expect "saw the session on Fable 5.1 before" "$o" "U4 earlier observation kept"; expect "Recover now" "$o" "U4 flagged"
set_base 'claude-fable-5-1[1m]'; { rec claude-fable-5-1 $OLD; mcmd '' 2026-09-01T00:00:10Z; mout 'Opus 5 (1M context)' 2026-09-01T00:00:12Z; rec claude-opus-5 2026-09-01T00:00:20Z; } > "$S/k.jsonl"; silent "K1 picker echo confirms" "$(out "$S/k.jsonl")"
rec claude-haiku-4-5 2026-09-01T00:00:30Z >> "$S/k.jsonl"; expect "Declared baseline: Opus 5 (claude-opus-5, from /model in this session)" "$(out "$S/k.jsonl")" "K1b picker choice as baseline"
{ rec claude-fable-5-1 $OLD; mcmd '' 2026-09-01T00:00:10Z; mout 'Fable 5.1' 2026-09-01T00:00:12Z; rec claude-fable-5 2026-09-01T00:00:20Z; } > "$S/k2.jsonl"; expect "This session runs as Fable 5 " "$(out "$S/k2.jsonl")" "K2 echo label must match exactly"
{ rec claude-fable-5-1 $OLD; mcmd '' 2026-09-01T00:00:10Z; mout 'Default (recommended)' 2026-09-01T00:00:12Z; rec claude-opus-5 2026-09-01T00:00:20Z; } > "$S/k3.jsonl"; o="$(out "$S/k3.jsonl")"; expect "ecover now" "$o" "K3 'Default' never confirms"; expect "from ~/.claude/settings.json model" "$o" "K3 baseline unchanged"
echo '# note cadence'
set_base 'claude-fable-5-1[1m]'; rec claude-opus-5 $OLD > "$S/r.jsonl"; expect "ecover now" "$(ev "$S/r.jsonl" PostToolUse)" "R1 first run full"
for i in 2 3 4 5 6 7 8 9 10; do o="$(ev "$S/r.jsonl" PostToolUse)"; case "$o" in "[model-guard] Still Opus 5"*) ;; *) echo "FAIL run $i not short: ${o:0:200}"; exit 1 ;; esac; done; echo "ok   R2-10 one line"
expect "ecover now" "$(ev "$S/r.jsonl" PostToolUse)" "R11 full"; expect "ecover now" "$(ev "$S/r.jsonl" UserPromptSubmit)" "R prompt always full"
recst r 1 COMPACT_SUBMITTED ""; expect "already in flight" "$(ev "$S/r.jsonl" PostToolUse)" "R driver in flight → full every run"
echo ALL GREEN
