#!/usr/bin/env python3
"""Model-fallback self-awareness guard (cc-self bundled).

Reads the running session's transcript, extracts the ACTUAL model of the most
recent assistant turn (Claude Code records it at message.model), compares it to
the declared baseline (settings.json `model`), and — while the session is off
baseline — emits an additionalContext note on EVERY run so the assistant stays
aware of the situation, together with the exact recovery step. Everything
after that one step is driven mechanically by the recover driver (see
scripts/cc-self and skills/model-recovery/SKILL.md).

Two off-baseline situations are told apart (1.3.3):

* FELL from the baseline — this session was observed ON the baseline earlier
  and a later record is below it. That is a safety fallback; the remedy is the
  compact-first recovery (`cc-self recover --compact-file ...`), because a
  bare /model switch does not hold while the trigger content is still in
  context.
* STARTED below the baseline — no record of this session has ever been on the
  baseline (typically the declared baseline was raised to a newer release
  after the session launched, or the session was launched with an explicit
  lower --model). Nothing in context triggered anything, so a plain switch is
  the right first move: `cc-self recover --switch-only` (no compaction). If
  the session falls back again afterwards, the guard escalates to
  compact-first with the usual attempt ladder.

Two readings are recognised as STALE and never turn into a directive: a
record that predates a just-completed recover switch (driver state), and a
record that predates the current baseline declaration itself (settings.json
was written AFTER the record — e.g. the user just ran /model, so the newest
record still carries the previous model). Alias baselines ("fable", "opus" —
no version digits) are compared by model family only: a concrete id can never
string-equal an alias, and the alias's version resolution is not observable
here. Pin a concrete id in settings.json to guard versions.

Coexistence: if you also run an external copy of this guard (e.g. a personal
ops script), keep only ONE enabled or every run gets duplicate notes. This
bundled hook is disabled by creating ~/.cc-self/state/guard-disabled (default:
enabled). State files live under ~/.cc-self/state/ and are strictly
per-session (keyed by transcript id), so they never collide with an external
guard's state kept elsewhere.

Invoked as a Claude Code hook: reads hook JSON from stdin. Also runnable
standalone with a transcript path argument for testing — close stdin when you
do (`python3 model-guard.py <transcript> < /dev/null`), or sys.stdin.read()
blocks waiting for EOF.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser("~/.claude/settings.json")
STATE_DIR = os.path.expanduser("~/.cc-self/state")
DISABLE_FLAG = os.path.join(STATE_DIR, "guard-disabled")

# How long an in-flight recover state stays trusted before it is considered
# stale (driver crashed / pane died) and a re-arm is instructed instead.
INFLIGHT_FRESH_SECS = 20 * 60


def state_path(transcript):
    # State must be PER-SESSION: multiple sessions (each its own transcript,
    # possibly on different models) run this same hook. A shared state file
    # makes them clobber each other and misreport phantom switches. Key by
    # transcript id.
    key = os.path.basename(transcript or "default").rsplit(".", 1)[0]
    return os.path.join(STATE_DIR, f"model-guard.{key}.state")


def declared_baseline_full():
    # The model the session DECLARES it is, read from settings.json (`model`,
    # e.g. "claude-fable-5[1m]"). Full string — this is what /model gets.
    try:
        s = json.load(open(SETTINGS))
        return (s.get("model") or "").strip() or "claude-fable-5"
    except Exception:
        return "claude-fable-5"


def settings_mtime():
    # When the baseline declaration itself was last written. A transcript
    # record older than this predates the declaration it is compared against.
    try:
        return os.path.getmtime(SETTINGS)
    except Exception:
        return None


def strip_ctx(model_id):
    return (model_id or "").split("[")[0].strip()


def model_family(model_id):
    """'claude-fable-5-1[1m]' -> 'fable', 'opus' -> 'opus', '' -> None."""
    m = strip_ctx(model_id)
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    fam = re.split(r"-?\d", m, 1)[0].strip("-")
    return fam or None


def baseline_is_alias(baseline):
    # "fable", "opus", "sonnet", "default", "opusplan": no version digits.
    return not re.search(r"\d", baseline)


def on_baseline(live, baseline):
    """Is the live (concrete) model id on the declared baseline?

    Concrete baseline: exact id equality (context suffix already stripped).
    Alias baseline: family match only — the alias's version resolution is not
    observable from here, so a version drift under an alias cannot be
    detected; an unresolvable alias ("default") compares as on-baseline.
    """
    if baseline_is_alias(baseline):
        fam = model_family(baseline)
        if fam in (None, "default"):
            return True
        lf = model_family(live)
        return bool(lf) and (lf == fam or fam.startswith(lf) or lf.startswith(fam))
    return live == baseline


def model_label(model_id):
    """Statusline label from a model id/arg, by normalization (not a table).

    Rules — keep in lockstep with model_label() in scripts/cc-self:
    strip [..] context suffix -> strip claude- prefix -> drop -YYYYMMDD date
    suffix -> digit-digit hyphens become dots -> hyphens become spaces ->
    capitalize each word. claude-fable-5[1m]->"Fable 5",
    claude-opus-4-8->"Opus 4.8", claude-haiku-4-5-20251001->"Haiku 4.5".
    """
    base = model_id.split("[")[0].strip()
    # exceptions normalization cannot produce go here (none known today)
    exceptions = {}
    if base in exceptions:
        return exceptions[base]
    m = base
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    m = re.sub(r"-\d{8}$", "", m)
    while re.search(r"\d-\d", m):
        m = re.sub(r"(\d)-(\d)", r"\1.\2", m)
    return " ".join(w[:1].upper() + w[1:] for w in m.split("-") if w)


def latest_model(transcript_path):
    # Returns (model, record_epoch_or_None) of the newest assistant record.
    # The latest assistant turn is near the end, but a single huge tool_result
    # (e.g. a multi-thousand-line subagent report) can exceed a small tail
    # window and push the last assistant line out of view — which would make
    # us read no model and silently skip the check. So grow the window until
    # we find an assistant model or we've read the whole file.
    try:
        size = os.path.getsize(transcript_path)
    except Exception:
        return None, None
    for back in (262_144, 2_097_152, 16_777_216):
        try:
            with open(transcript_path, "rb") as f:
                f.seek(max(0, size - back))
                chunk = f.read().decode("utf-8", "ignore")
        except Exception:
            return None, None
        for line in reversed(chunk.splitlines()):
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("message", {})
            if isinstance(m, dict) and m.get("role") == "assistant":
                model = m.get("model")
                if model and model != "<synthetic>":
                    return model, record_epoch(d.get("timestamp"))
        if back >= size:
            break
    return None, None


def record_epoch(ts):
    # Transcript records stamp ISO8601 UTC ("2026-08-28T09:37:26.642Z").
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fmt_utc(epoch):
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M:%SZ")
    except Exception:
        return "?"


def recover_state(key):
    """The recover driver's per-session state (written by scripts/cc-self)."""
    path = os.path.join(STATE_DIR, f"recover-{key}.json")
    try:
        st = json.load(open(path))
        st["_mtime"] = os.path.getmtime(path)
        return st
    except Exception:
        return None


def load_state(path):
    """Per-session guard state: {"last": <model id>, "seen_baseline": bool}.

    Pre-1.3.3 state files held the bare last model id — read those as
    {"last": id} (seen_baseline unknown → False, i.e. the conservative
    "started below" reading until a baseline record is observed).
    """
    try:
        raw = open(path).read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {"last": raw}


def save_state(path, st):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except Exception:
        pass


def emit(event, note):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": note,
        }
    }))


def recovery_directive(rec, live_name, base_name, live_ts=None, seen_baseline=True):
    """The recovery instruction appended to every off-baseline note.

    The model's ONLY job is filling the compact template (or, for a session
    that merely started below the baseline, arming a plain switch);
    `cc-self recover` does the rest deterministically (submit /compact, wait
    it out, switch /model, approve the dialog after seeing it, wake,
    transcript-verify).
    """
    phase = (rec or {}).get("phase", "")
    note = str((rec or {}).get("note", ""))
    attempt_prev = int((rec or {}).get("attempt", 0) or 0)
    fresh = rec and (time.time() - rec.get("_mtime", 0) < INFLIGHT_FRESH_SECS)
    inflight = ("ARMED", "COMPACT_SUBMITTED", "COMPACT_DONE", "TURN_IDLE", "MODEL_DIALOG")
    landed = ("SWITCHED", "VERIFY_WAKE", "DONE")

    # Terminal: an operator explicitly closed this recovery (cc-self recover
    # --close). State the fact, instruct nothing.
    if phase == "CLOSED":
        return (" Recovery was closed by operator decision (state CLOSED) — "
                "not instructing a re-arm.")

    # Stale-read protection (found live by clawd, 2026-08-28): on the first
    # hook firings of the post-switch wake turn, the newest assistant record
    # still predates the switch (UserPromptSubmit fires before the turn's
    # first assistant message exists; transcript flush can lag a few tool
    # calls more). Re-arming from that record nearly triggered a needless
    # attempt 5 on a recovery that had in fact HELD. If the record is older
    # than the recover state's last write, the off-baseline reading proves
    # nothing about the current runtime — unless the driver itself saw a
    # post-wake off-baseline record ("re-fell"), which IS post-switch truth.
    if (phase in landed and fresh and live_ts is not None
            and live_ts <= rec.get("_mtime", 0)
            and not note.startswith("re-fell")):
        return (" NOTE: this reading comes from a transcript record that "
                f"predates the just-completed switch (recover attempt "
                f"{attempt_prev} state {phase}: {note or 'no note'}) — it is "
                "almost certainly STALE. Do NOT re-arm from this note alone; "
                "a post-switch assistant record will settle it within a few "
                "tool calls, and this guard falls silent if the recovery held.")

    if phase in inflight and fresh:
        return (f" A recovery attempt {attempt_prev} is already in flight "
                f"(phase {phase}) — do not re-arm; end the turn and let the "
                f"driver finish.")

    # No recovery ever armed for this session and it was never seen on the
    # baseline: a plain switch, not a compaction — nothing in context
    # triggered a fallback. Attempt 0 keeps the compact-first ladder at 1+.
    if not rec and not seen_baseline:
        return (
            f" Switch without compacting (attempt 0): run `bash {PLUGIN_ROOT}"
            f"/scripts/cc-self recover --switch-only` and end the turn. The "
            f"driver waits for the turn to end, submits /model {base_name}, "
            f"approves the confirm dialog only if one appears, wakes the "
            f"session and verifies the live model from the transcript. If the "
            f"session falls back again after that, this guard escalates to the "
            f"compact-first recovery (attempt 1).")

    attempt_next = attempt_prev + 1 if rec else 1
    parts = []
    if phase in landed and attempt_prev == 0:
        parts.append(
            f" A plain switch (attempt 0) reached phase {phase} but the "
            f"session has since fallen back to {live_name} — the content in "
            f"context re-triggers the fallback, so a bare switch cannot hold; "
            f"compact-first is required now.")
    elif phase in landed:
        parts.append(
            f" A prior recovery (attempt {attempt_prev}) reached phase {phase} "
            f"but the session fell back to {live_name} again — the compact did "
            f"not neutralize enough. Identify the concrete phrasings that "
            f"survived into the previous compact summary and re-abstract them "
            f"into broader, more neutral categories this time.")
    elif phase.startswith("FAILED-") or (phase in inflight and not fresh):
        parts.append(
            f" A prior recovery attempt {attempt_prev} did not complete "
            f"(state {phase or 'stale'}); check ~/.cc-self.log, then re-arm.")
    parts.append(
        f" Recover now (attempt {attempt_next}), per the cc-self "
        f"model-recovery skill: write a compact instruction file — preserve "
        f"all work context, abstract the fallback-trigger content into "
        f"neutral terms (the higher the attempt, the stronger the "
        f"abstraction) — then run `bash {PLUGIN_ROOT}/scripts/cc-self recover "
        f"--compact-file <path> --attempt {attempt_next}` and end the turn. "
        f"The driver handles everything after that (compact, /model "
        f"{base_name} switch, dialog, verification, wake).")
    if attempt_next >= 3:
        parts.append(
            " Also report the repeated fallback to the user — the report is "
            "in addition to retrying, never instead of it.")
    return "".join(parts)


def main():
    if os.path.exists(DISABLE_FLAG):
        sys.exit(0)
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    inp = {}
    if raw.strip():
        try:
            inp = json.loads(raw)
        except Exception:
            inp = {}
    transcript = inp.get("transcript_path") or (sys.argv[1] if len(sys.argv) > 1 else None)
    event = inp.get("hook_event_name", "manual")
    # Prefer a model field if the harness ever provides one directly.
    live, live_ts = inp.get("model"), None
    if not live and transcript:
        live, live_ts = latest_model(transcript)
    if not live:
        sys.exit(0)

    baseline_full = declared_baseline_full()
    baseline = strip_ctx(baseline_full)
    spath = state_path(transcript)
    st = load_state(spath)
    last = st.get("last")
    changed = (last is not None and last != live)
    key = os.path.basename(transcript or "?").rsplit(".", 1)[0]

    # Stay silent ONLY when we're on the declared baseline. Being OFF baseline
    # is a PERSISTENT hazard, so we re-flag on EVERY run until it's restored —
    # a single note can be missed (buried under a large tool result); re-
    # flagging every run makes a missed note self-healing.
    if on_baseline(live, baseline):
        st["last"] = live
        st["seen_baseline"] = True
        save_state(spath, st)
        sys.exit(0)

    # Baseline-change stale window (observed live 2026-09-02): the user ran
    # /model (or edited settings.json) AFTER the newest assistant record was
    # written, so that record still carries the previous model. Comparing it
    # against the new declaration says nothing about the live model — the
    # switch takes effect on the next assistant message. Two guards diagnosed
    # a "safety fallback" from exactly this window and a needless compaction
    # followed. State the staleness, instruct nothing, leave state untouched.
    smt = settings_mtime()
    if live_ts is not None and smt is not None and live_ts <= smt:
        emit(event, (
            f"[model-guard] STALE reading: the newest assistant record (model "
            f"{live}, {fmt_utc(live_ts)}) predates the current baseline "
            f"declaration in settings.json ({baseline_full}, written "
            f"{fmt_utc(smt)}) — e.g. /model was just used. It proves nothing "
            f"about the live model and is NOT a fallback diagnosis: do not arm "
            f"any recovery from it. The next assistant record settles it; this "
            f"guard is silent once the session is on the baseline."))
        sys.exit(0)

    seen_baseline = bool(st.get("seen_baseline"))
    st["last"] = live
    save_state(spath, st)
    # Append a fallback-history line only on an actual TRANSITION (or first
    # sight) — off-baseline re-flags every run by design, so without this
    # guard the log gains one identical "X -> X" row per tool call.
    if changed or last is None:
        try:
            ts = datetime.now(timezone.utc).strftime("%FT%TZ")
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "model-guard-events.log"), "a") as ev:
                ev.write(f"{ts} session={key[:8]} {last or '(none)'} -> {live} "
                         f"event={event} seen_baseline={seen_baseline}\n")
        except Exception:
            pass

    live_name = model_label(live)
    base_name = model_label(baseline_full)
    if changed:
        prev_name = model_label(last)
        note = (f"[model-guard] Runtime model changed: {prev_name} → {live_name} "
                f"(actual id: {live}). Your system prompt still declares {base_name}; "
                f"that is stale. You are now running as {live_name}.")
    elif seen_baseline:
        note = (f"[model-guard] You are actually running as {live_name} "
                f"(actual id: {live}), not the {base_name} your system prompt "
                f"declares. A safety fallback switched you. Do not claim to be "
                f"{base_name}.")
    else:
        note = (f"[model-guard] You are running as {live_name} (actual id: "
                f"{live}); settings.json declares {base_name} as the baseline, "
                f"and no record of this session has been on it since launch — "
                f"the session STARTED below the baseline (e.g. the baseline was "
                f"raised to a newer release after launch). That is not a "
                f"diagnosed safety fallback, but the baseline still has to be "
                f"restored. Do not claim to be {base_name}.")
    note += recovery_directive(recover_state(key), live_name, base_name, live_ts, seen_baseline)
    emit(event, note)
    sys.exit(0)


if __name__ == "__main__":
    main()
