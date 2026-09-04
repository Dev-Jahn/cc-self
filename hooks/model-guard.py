#!/usr/bin/env python3
"""Model-fallback self-awareness guard (cc-self bundled).

Reads the running session's transcript, extracts the ACTUAL model of the most
recent assistant turn (Claude Code records it at message.model), compares it to
the DECLARED baseline, and — while the session is off baseline — emits an
additionalContext note on EVERY run so the assistant stays aware of the
divergence, together with the exact recovery step: write a compact instruction
file and run `cc-self recover`. Everything after that one step is driven
mechanically by the recover driver (see scripts/cc-self and
skills/model-recovery/SKILL.md).

The baseline is only ever something the user declared, in this order:
CC_SELF_BASELINE in the session's environment (per session — hooks inherit the
launch environment), then `model` in ~/.claude/settings.json. With neither
there is NO baseline, hence no divergence to measure: the guard then reports a
mid-session model transition once (that is observed, not inferred) and
instructs nothing. There is deliberately no built-in default — a hard-coded
one made the guard demand recovery to a model the user never declared, on
every tool call, and contradict `cc-self recover`, which refuses without a
declared baseline. Every note names the source of its baseline and says
whether the session was ever seen on it (a mid-session move away from the
baseline is what a fallback looks like; a session never seen on it may simply
have been launched on another model).

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
Baseline and live id compare by model family: an alias ("sonnet", the form
/model writes into settings.json), a dated snapshot ("claude-haiku-4-5-20251001",
the form the API answers with) and the bare id all name the same model.
"""
import json
import os
import re
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser("~/.claude/settings.json")
STATE_DIR = os.path.expanduser("~/.cc-self/state")
DISABLE_FLAG = os.path.join(STATE_DIR, "guard-disabled")
# Per-process opt-out (e.g. a scratch session driven by cc-self's own tests):
# launch claude with CC_SELF_GUARD_DISABLED=1 — hooks inherit its environment.
DISABLE_ENV = "CC_SELF_GUARD_DISABLED"
# Per-session baseline declaration (beats settings.json): for a session
# deliberately run on another model than the machine-wide default.
BASELINE_ENV = "CC_SELF_BASELINE"

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


def declared_baseline():
    """(baseline as declared, source) — or (None, None) when nothing declares one.

    The full string (e.g. "claude-fable-5[1m]") is what /model gets. Sources:
    CC_SELF_BASELINE in the environment, then settings.json `model`. No
    built-in default: with no declaration there is no baseline.
    """
    env = (os.environ.get(BASELINE_ENV) or "").strip()
    if env:
        return env, BASELINE_ENV
    try:
        s = json.load(open(SETTINGS))
        m = (s.get("model") or "").strip()
        if m:
            return m, "~/.claude/settings.json model"
    except Exception:
        pass
    return None, None


def same_model(a, b):
    """Declared baseline vs live id — either side may be a bare id
    ("claude-sonnet-5"), a dated snapshot ("claude-haiku-4-5-20251001"), a
    suffixed form ("claude-fable-5[1m]") or an alias ("sonnet", "opus",
    "haiku", "fable", "opusplan"). Keep in lockstep with same_model() in
    scripts/cc-self."""
    def norm(x):
        return re.sub(r"-\d{8}$", "", str(x or "").split("[")[0].strip())
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    for alias, ident in ((a, b), (b, a)):
        if "-" in alias:
            continue
        fams = ("opus", "sonnet") if alias == "opusplan" else (alias,)
        if any(ident == f"claude-{f}" or ident.startswith(f"claude-{f}-") for f in fams):
            return True
    return False


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
            if d.get("isSidechain"):
                continue   # subagents may legitimately run on other models
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
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def recover_state(key):
    """The recover driver's per-session state (written by scripts/cc-self)."""
    path = os.path.join(STATE_DIR, f"recover-{key}.json")
    try:
        st = json.load(open(path))
        st["_mtime"] = os.path.getmtime(path)
        return st
    except Exception:
        return None


def driver_alive(rec):
    """True while the driver that wrote this state is still running (it
    records its pid on its first write; a pid-less ARMED state is the short
    window before the driver starts)."""
    try:
        pid = int((rec or {}).get("pid", 0) or 0)
    except Exception:
        pid = 0
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def load_state(path):
    """{"last": newest model seen, "seen_on": the baseline this session was
    last observed running on}. 1.3.x state files hold a bare model id."""
    st = {"last": None, "seen_on": None}
    try:
        raw = open(path).read().strip()
    except Exception:
        return st
    if not raw:
        return st
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            for k in ("last", "seen_on"):
                v = d.get(k)
                st[k] = v.strip() if isinstance(v, str) and v.strip() else None
            return st
    except Exception:
        pass
    st["last"] = raw
    return st


def save_state(path, st):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps({"last": st["last"], "seen_on": st["seen_on"]}))
        os.replace(tmp, path)
    except Exception:
        pass


def stale_reading(rec, live_ts):
    """True when the off-baseline reading predates a just-completed switch
    (found live by clawd, 2026-08-28): on the first hook firings of the
    post-switch wake turn, the newest assistant record still predates the
    switch (UserPromptSubmit fires before the turn's first assistant message
    exists; transcript flush can lag a few tool calls more). Such a reading
    proves nothing about the current runtime — unless the driver itself saw a
    post-wake off-baseline record ("re-fell"), which IS post-switch truth."""
    import time
    if not rec or live_ts is None:
        return False
    phase = rec.get("phase", "")
    alive = driver_alive(rec)
    window = INFLIGHT_FRESH_SECS if int(rec.get("pid", 0) or 0) > 0 else 120
    fresh = alive or time.time() - rec.get("_mtime", 0) < window
    return (phase in ("SWITCHED", "VERIFY_WAKE", "DONE") and fresh
            and live_ts <= rec.get("_mtime", 0)
            and not str(rec.get("note", "")).startswith("re-fell"))


def recovery_directive(rec, live_name, base_name, live_ts=None, switched=True):
    """The recovery instruction appended to every off-baseline note.

    The model's ONLY job is filling the compact template; `cc-self recover`
    does the rest deterministically (submit /compact, wait it out, switch
    /model, approve the dialog after seeing it, wake, transcript-verify).
    `switched` is the evidence grade: True when this session was seen on the
    baseline before (a mid-session move away from it), False when the guard
    never saw it there — then the instruction is conditional, because a
    deliberate launch on another model measures exactly the same.
    """
    import time
    phase = (rec or {}).get("phase", "")
    note = str((rec or {}).get("note", ""))
    attempt_prev = int((rec or {}).get("attempt", 0) or 0)
    attempt_next = attempt_prev + 1 if rec else 1
    alive = driver_alive(rec)
    # A pid-less state is the short window between arming and the driver's
    # first write (or a CLI that died before detaching): trust it briefly.
    window = INFLIGHT_FRESH_SECS if int((rec or {}).get("pid", 0) or 0) > 0 else 120
    fresh = rec and (alive or time.time() - rec.get("_mtime", 0) < window)
    inflight = ("ARMED", "COMPACT_SUBMITTED", "COMPACT_DONE",
                "SWITCH_SUBMITTED", "MODEL_DIALOG")
    landed = ("SWITCHED", "VERIFY_WAKE", "DONE")

    # Terminal: an operator explicitly closed this recovery (cc-self recover
    # --close). State the fact, instruct nothing.
    if phase == "CLOSED":
        return (" Recovery was closed by operator decision (state CLOSED) — "
                "not instructing a re-arm.")

    # Stale-read protection: re-arming from a pre-switch record nearly
    # triggered a needless attempt 5 on a recovery that had in fact HELD.
    if stale_reading(rec, live_ts):
        return (" NOTE: this reading comes from a transcript record that "
                f"predates the just-completed switch (recover attempt "
                f"{attempt_prev} state {phase}: {note or 'no note'}) — it is "
                "almost certainly STALE. Do NOT re-arm from this note alone; "
                "a post-switch assistant record will settle it within a few "
                "tool calls, and this guard falls silent if the recovery held.")

    if phase in inflight and fresh:
        return (f" A recovery attempt {attempt_prev} is already in flight "
                f"(phase {phase}{', driver alive' if alive else ''}) — do NOT "
                f"re-arm, do NOT type /compact, /model or any wake yourself; "
                f"end the turn and let the driver finish (cc-self refuses a "
                f"second arm while the driver lives).")
    if phase in landed and alive:
        return (f" Recovery attempt {attempt_prev} is completing (phase "
                f"{phase}, driver alive) — do not re-arm yet; the driver "
                f"verifies the switch from the transcript and this guard "
                f"settles it on the next records.")

    parts = []
    if phase in landed:
        parts.append(
            f" A prior recovery (attempt {attempt_prev}) reached phase {phase} "
            f"but the session fell back to {live_name} again — the compact did "
            f"not neutralize enough. Identify the concrete phrasings that "
            f"survived into the previous compact summary and re-abstract them "
            f"into broader, more neutral categories this time.")
    elif phase.startswith("FAILED-") or (phase in inflight and not fresh):
        parts.append(
            f" A prior recovery attempt {attempt_prev} did not complete "
            f"(state {phase or 'stale'}: {note or 'no note'}); see "
            f"`cc-self recover --status` and ~/.cc-self.log for the cause, "
            f"then re-arm.")
    lead = ("Recover now" if switched else
            "If this is a fallback and not a deliberate choice, recover now")
    parts.append(
        f" {lead} (attempt {attempt_next}), per the cc-self "
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
    if os.path.exists(DISABLE_FLAG) or os.environ.get(DISABLE_ENV):
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
    # Transcript truth only: the hook input's `model` (SessionStart) is the
    # CONFIGURED model, not what the API actually answered with.
    live, live_ts = (None, None)
    if transcript:
        live, live_ts = latest_model(transcript)
    if not live:
        sys.exit(0)

    baseline_full, source = declared_baseline()
    baseline = baseline_full.split("[")[0].strip() if baseline_full else None
    spath = state_path(transcript)
    st = load_state(spath)
    last = st["last"]
    changed = (last is not None and last != live)
    # "Seen on the baseline" is this guard's own observation: the previous
    # run's reading (covers 1.3.x bare-id state files) or this one.
    if baseline and (same_model(baseline, last) or same_model(baseline, live)):
        st["seen_on"] = baseline
    st["last"] = live
    save_state(spath, st)
    key = os.path.basename(transcript or "?").rsplit(".", 1)[0]

    if baseline is None:
        # Nothing declared: no divergence to measure. Report an observed
        # transition once, instruct nothing, otherwise stay silent.
        if not changed:
            sys.exit(0)
        log_event(key, last, live, event)
        emit(event, (
            f"[model-guard] Runtime model changed: {model_label(last)} → "
            f"{model_label(live)} (id {live}), read from this session's "
            f"transcript. No baseline is declared ({BASELINE_ENV} unset, no "
            f"`model` in ~/.claude/settings.json), so the guard measures no "
            f"divergence and instructs nothing; declare one to enable "
            f"recovery. Do not claim to be {model_label(last)}."))
        sys.exit(0)

    # Stay silent ONLY when we're on the declared baseline. Being OFF baseline
    # is a PERSISTENT hazard, so we re-flag on EVERY run until it's restored —
    # a single note can be missed (buried under a large tool result); re-
    # flagging every run makes a missed note self-healing.
    if same_model(baseline, live):
        sys.exit(0)

    # Append a fallback-history line only on an actual TRANSITION (or first
    # sight) — off-baseline re-flags every run by design, so without this
    # guard the log gains one identical "X -> X" row per tool call.
    if changed or last is None:
        log_event(key, last, live, event)

    live_name = model_label(live)
    base_name = model_label(baseline_full)
    rec = recover_state(key)
    # Facts only, each with its evidence: the live model from the transcript,
    # the baseline from a named source, and whether this guard ever saw the
    # session on that baseline. The guard measures live != baseline and
    # nothing else — it cannot see WHY they differ, so it does not assert a
    # cause it did not observe (a deliberate --model launch measures exactly
    # like a safety fallback, and an inaccurate note gets dismissed wholesale).
    if changed:
        obs = (f"[model-guard] Runtime model changed: {model_label(last)} → "
               f"{live_name} (id {live}), read from this session's transcript.")
    else:
        obs = (f"[model-guard] This session runs as {live_name} (id {live}, "
               f"from this session's transcript).")
    obs += f" Declared baseline: {base_name} ({baseline_full}, from {source})."
    if stale_reading(rec, live_ts):
        # The reading predates a just-completed switch: state the two facts
        # and the staleness, interpret nothing.
        emit(event, obs + recovery_directive(rec, live_name, base_name, live_ts))
        sys.exit(0)
    declare = (f"The user declares a session meant to run on {live_name} with "
               f"{BASELINE_ENV}={live} at launch or `model` in "
               f"~/.claude/settings.json, and this note stops — that is the "
               f"user's call, not yours.")
    switched = same_model(baseline, st["seen_on"])
    if switched:
        obs += (f" This guard saw the session on {base_name} before this "
                f"reading, so this is a mid-session move away from the "
                f"declared baseline — what a safety fallback looks like. "
                f"{declare}")
    elif rec and rec.get("phase") != "CLOSED":
        # A recovery toward this baseline was armed for this session: the
        # operator has already called it a fallback.
        switched = True
        obs += (f" A recovery toward {base_name} was armed for this session "
                f"(attempt {rec.get('attempt', '?')}), so the baseline is the "
                f"operator's stated intent.")
    else:
        obs += (f" This guard has not seen the session on {base_name}: a "
                f"safety fallback and a session deliberately run on "
                f"{live_name} measure the same here. {declare}")
    note = obs + f" Do not claim to be {base_name}."
    note += recovery_directive(rec, live_name, base_name, live_ts, switched)
    emit(event, note)
    sys.exit(0)


def log_event(key, last, live, event):
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%FT%TZ")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "model-guard-events.log"), "a") as ev:
            ev.write(f"{ts} session={key[:8]} {last or '(none)'} -> {live} event={event}\n")
    except Exception:
        pass


def emit(event, note):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": note,
        }
    }))


if __name__ == "__main__":
    main()
