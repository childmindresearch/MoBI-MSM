#!/usr/bin/env python3
"""Spot-check one XDF recording against the MoBI-MSM protocol.

Run this straight after a session, with the participant still in the room. It
answers the two questions an RA has at that moment: is this recording usable,
and is there something to fix before the next person sits down.

The file is the only input. Nothing here reads the JSONL mirror, the database,
or the content files, so it works on a copied-off recording on any machine.

What it can and cannot conclude
-------------------------------
The flow is server-owned and forward-only -- ``Session.submit`` raises
FlowError on anything that is not the step the session is actually on -- so the
task *cannot* skip a step. A marker missing from the middle of an otherwise
intact block is therefore a recorder dropout, not a protocol violation, and is
reported as such. Markers that are out of order or duplicated cannot come from
a dropout at all, and are reported more seriously.

The protocol itself is imported from ``app/`` rather than restated here, so
editing the flow updates this check with it.

    .venv/bin/python scripts/check_xdf.py sub-TEST001_ses-01.xdf
    .venv/bin/python scripts/check_xdf.py rec.xdf --timeline
    .venv/bin/python scripts/check_xdf.py rec.xdf --no-coverage

Exit status: 0 clean, 1 notes only, 2 something to fix or an unusable file.
"""

from __future__ import annotations

import argparse
import difflib
import json
import statistics
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.session import LLM_STEPS, STEP_ORDER  # noqa: E402

# Severity, in the order an RA acts on it.
UNUSABLE = "UNUSABLE"   # this recording is not a complete, analysable session
FIX = "FIX"             # fix before the next participant
NOTE = "NOTE"           # record it and move on

# The server-owned events that make up the protocol sequence. Everything else --
# client render/scroll/focus events, rejections, failures -- is counted
# separately and never breaks the replay.
FLOW_EVENTS = frozenset({
    "block_started", "submission_recorded", "llm_request_sent", "llm_response_received",
})

LABEL_W = 12
DIFFERENT_CLOCK_S = 86400.0
"""A stream whose timestamps sit this far from the markers' is not late, it is
on another clock entirely. LSL's local_clock counts from an arbitrary epoch --
host boot, in practice -- so a source that stamps with wall-clock time instead
lands about 1.8e9 seconds away, and no amount of clock-offset correction moves
it back."""

UNIX_EPOCH_RANGE = (1.4e9, 2.6e9)
"""Timestamps in this range are seconds since 1970, i.e. time.time() where
lsl_local_clock() was meant to be."""

GAP_NOTE_S = 180.0
"""Longest quiet stretch between markers before it is worth mentioning. A
participant composing a 15-word minimum response is legitimately slow; three
minutes of nothing is someone who left the chair."""


# --- findings -------------------------------------------------------------

@dataclass
class Report:
    findings: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def add(self, level: str, headline: str, *detail: str) -> None:
        self.findings.append((level, headline, [d for d in detail if d]))

    def count(self, level: str) -> int:
        return sum(1 for lvl, _, _ in self.findings if lvl == level)

    def exit_code(self) -> int:
        if self.count(UNUSABLE) or self.count(FIX):
            return 2
        return 1 if self.findings else 0

    def verdict(self) -> str:
        bits = []
        if self.count(UNUSABLE):
            bits.append(f"{plural(self.count(UNUSABLE), 'problem')} making this recording "
                        f"unusable")
        if self.count(FIX):
            bits.append(f"{self.count(FIX)} to fix before the next run")
        if self.count(NOTE):
            bits.append(f"{self.count(NOTE)} to note")
        return " · ".join(bits) if bits else "clean — protocol followed, recording complete"

    def print_findings(self) -> None:
        if not self.findings:
            return
        print()
        order = {UNUSABLE: 0, FIX: 1, NOTE: 2}
        for level, headline, detail in sorted(self.findings, key=lambda f: order[f[0]]):
            head = textwrap.wrap(headline, width=68) or [""]
            print(f"{level:<10}{head[0]}")
            for more in head[1:]:
                print(f"{'':<10}{more}")
            for d in detail:
                for seg in textwrap.wrap(d, width=68) or [""]:
                    print(f"{'':<10}{seg}")


# --- markers --------------------------------------------------------------

@dataclass
class Marker:
    ts: float
    """The XDF timestamp, loaded *without* pyxdf's clock synchronisation."""
    rec: dict[str, Any]

    @property
    def event(self) -> str:
        return str(self.rec.get("event", "?"))

    @property
    def session_id(self) -> str | None:
        sid = self.rec.get("session_id")
        return str(sid) if sid else None

    @property
    def block(self) -> int | None:
        b = self.rec.get("block_index")
        return b if isinstance(b, int) else None

    def get(self, key: str, default: Any = None) -> Any:
        return self.rec.get(key, default)


def load_markers(pyxdf, path: Path, stream_id: int) -> tuple[dict[str, Any], list[Marker], list[str]]:
    """Load one marker stream, unsynchronised and undejittered.

    pyxdf synchronises clocks by default, which rewrites every timestamp from
    the offset records. app/markers.py deliberately stamps each sample with the
    same ``t_lsl`` it writes into the payload, and that equality is the thing
    this script checks and reports on -- so the raw timestamps are what we want.
    Clock-reset handling is off for the same reason: a reset should surface as
    non-monotonic time here, not be quietly repaired.
    """
    streams, _ = pyxdf.load_xdf(
        str(path),
        select_streams=[stream_id],
        synchronize_clocks=False,
        dejitter_timestamps=False,
        handle_clock_resets=False,
    )
    if not streams:
        return {}, [], []
    stream = streams[0]

    markers: list[Marker] = []
    bad: list[str] = []
    for values, ts in zip(stream["time_series"], stream["time_stamps"]):
        raw = values[0] if isinstance(values, (list, tuple)) else values
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            bad.append(str(raw)[:80])
            continue
        if not isinstance(rec, dict) or "event" not in rec:
            bad.append(str(raw)[:80])
            continue
        markers.append(Marker(float(ts), rec))
    return stream, markers, bad


# --- the protocol ---------------------------------------------------------

def expected_sequence(block_index: int) -> list[str]:
    """The flow markers one block must produce, in order, from STEP_ORDER.

    Block 12 ends at the Activation Score and goes straight to the End Screen
    (``Session._advance_from``), so it has no gate.
    """
    is_final = block_index == config.REAL_BLOCK_COUNT
    seq = ["block_started"]
    for step in STEP_ORDER:
        if step == "vignette":
            continue          # rendered by open_block; nothing is submitted for it
        if step in LLM_STEPS:
            seq += [f"{step} request", f"{step} reply"]
            continue
        if step == "gate" and is_final:
            continue
        seq.append(f"submit {step}")
    return seq


def actual_sequence(markers: list[Marker]) -> list[str]:
    """The flow markers a block actually produced, labelled to match.

    A turn that errored out leaves an ``llm_request_sent`` with no reply and is
    followed by a retry. That is a working system recovering, not a broken
    sequence, so the failed request is withdrawn from the comparison and
    counted separately.
    """
    seq: list[str] = []
    pending_llm: str | None = None
    for m in markers:
        if m.event == "block_started":
            seq.append("block_started")
        elif m.event == "submission_recorded":
            seq.append(f"submit {m.get('step', '?')}")
        elif m.event == "llm_request_sent":
            pending_llm = str(m.get("step", "llm_?"))
            seq.append(f"{pending_llm} request")
        elif m.event == "llm_response_received":
            seq.append(f"{pending_llm or 'llm_?'} reply")
            pending_llm = None
        elif m.event == "llm_request_failed":
            if pending_llm and seq and seq[-1] == f"{pending_llm} request":
                seq.pop()
            pending_llm = None
    return seq


def diff_sequence(expected: list[str], actual: list[str]) -> tuple[list[str], list[str]]:
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    missing: list[str] = []
    extra: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            missing += expected[i1:i2]
        if tag in ("insert", "replace"):
            extra += actual[j1:j2]
    return missing, extra


# --- formatting -----------------------------------------------------------

def line(label: str, text: str) -> None:
    print(f"{label:<{LABEL_W}}{text}")


def fmt_duration(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(secs)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"


def fmt_clock(t_unix: float | None) -> str:
    if not t_unix:
        return "?"
    return datetime.fromtimestamp(float(t_unix), tz=timezone.utc).strftime("%H:%M:%S")


def fmt_date(t_unix: float | None) -> str:
    if not t_unix:
        return "?"
    return datetime.fromtimestamp(float(t_unix), tz=timezone.utc).strftime("%Y-%m-%d")


IRREGULAR_PLURALS = {"reply": "replies", "fixation cross": "fixation crosses"}


def pack(items: list[str], width: int = 66, lead: str = "") -> list[str]:
    """Wrap a list onto lines without ever splitting one item."""
    lines: list[str] = []
    current = lead.rstrip()
    for i, item in enumerate(items):
        piece = item + ("," if i < len(items) - 1 else "")
        candidate = f"{current} {piece}" if current else piece
        if current and len(candidate) > width:
            lines.append(current)
            current = piece
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {IRREGULAR_PLURALS.get(word, word + 's')}"


def blocks_phrase(blocks: list[int]) -> str:
    """"block 3" or "blocks 1-4, 7"."""
    return f"{'block' if len(blocks) == 1 else 'blocks'} {_ranges(blocks)}"


def counted(items: Counter, limit: int = 4) -> str:
    parts = [f"{k}×{v}" if v > 1 else str(k) for k, v in items.most_common(limit)]
    if len(items) > limit:
        parts.append(f"+{len(items) - limit} more")
    return ", ".join(parts)


class Out:
    """Summary lines, buffered so the verdict can be printed above them."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def line(self, label: str, text: str) -> None:
        self.rows.append((label, text))

    def cont(self, text: str) -> None:
        self.rows.append(("", text))

    def flush(self) -> None:
        for label, text in self.rows:
            line(label, text)


# --- checks ---------------------------------------------------------------

def check_recording(out: Out, rep: Report, path: Path, inventory: list[dict[str, Any]],
                    stream_id: int, stream: dict[str, Any], markers: list[Marker],
                    bad: list[str]) -> None:
    span = markers[-1].ts - markers[0].ts if len(markers) > 1 else 0.0
    others = [s for s in inventory if int(s["stream_id"]) != stream_id]
    out.line("Recording", f"{path.name} · {plural(len(inventory), 'stream')} · "
                          f"{len(markers):,} markers over {fmt_duration(span)}")
    if others:
        for seg in pack([f"{s['name']} ({s['type'] or '?'}, {s['channel_count']} ch)"
                         for s in others], lead="alongside "):
            out.cont(seg)

    if bad:
        rep.add(UNUSABLE, f"{plural(len(bad), 'sample')} in the marker stream "
                          f"{'is' if len(bad) == 1 else 'are'} not a JSON object.",
                f"first: {bad[0]}",
                "A string channel cannot corrupt a payload in transit; treat the file as damaged.")

    # The pinning from app/markers.py: push_sample is given the same t_lsl that
    # goes into the payload, so these two numbers are meant to be identical.
    deltas = [abs(m.ts - float(m.get("t_lsl")))
              for m in markers if isinstance(m.get("t_lsl"), (int, float))]
    if not deltas:
        rep.add(NOTE, "No marker carries t_lsl, so the timestamps cannot be cross-checked.")
    elif max(deltas) == 0:
        out.line("Timestamps", "pinned to t_lsl exactly (Δ 0 µs) · loaded unsynchronised")
    else:
        worst_us = max(deltas) * 1e6
        out.line("Timestamps", f"max Δ from t_lsl {worst_us:.1f} µs · loaded unsynchronised")
        if worst_us > 1.0:
            rep.add(NOTE, f"Recorded timestamps differ from the payload t_lsl by up to "
                          f"{worst_us:.1f} µs.",
                    "They are pinned to the same number by app/markers.py, so this was most "
                    "likely collected by a build predating that change.")

    offsets = list(stream.get("clock_values") or [])
    if offsets:
        median_ms = statistics.median(offsets) * 1000
        if abs(median_ms) >= 1.0:
            out.line("Clock", f"recorder offset median {median_ms:+.2f} ms "
                              f"({plural(len(offsets), 'sample')})")
            rep.add(NOTE, f"The recorder's clock sits {median_ms:+.2f} ms from the task's.",
                    "Loading this file with pyxdf's default synchronize_clocks=True will shift "
                    "the timestamps off the t_lsl values in the payloads.")

    backwards = [(a, b) for a, b in zip(markers, markers[1:]) if b.ts < a.ts]
    if backwards:
        a, b = backwards[0]
        rep.add(UNUSABLE, f"Time runs backwards at {plural(len(backwards), 'point')} in the stream.",
                f"first: {a.event} at {a.ts:.3f} followed by {b.event} at {b.ts:.3f}",
                "A dropout cannot reorder markers. This is a clock reset, or two outlets "
                "advertising the same source_id into one recording.")

    pairs = list(zip(markers, markers[1:]))
    if pairs:
        # Keyed rather than sorted on the tuple: two gaps of identical length
        # would otherwise fall through to comparing Marker objects.
        a, b = max(pairs, key=lambda pair: pair[1].ts - pair[0].ts)
        dt = b.ts - a.ts
    else:
        dt = 0.0
    if dt >= GAP_NOTE_S:
        where = f"block {a.block}" if a.block is not None else "outside a block"
        rep.add(NOTE, f"Longest quiet stretch was {fmt_duration(dt)}, in {where}.",
                f"between {a.event} and {b.event}")


def check_session(out: Out, rep: Report, markers: list[Marker]) -> tuple[bool, bool]:
    """Identity and the two recording boundaries. Returns (head_cut, tail_cut)."""
    started = [m for m in markers if m.event == "session_started"]
    completed = [m for m in markers if m.event == "session_completed"]
    head_cut = not started
    tail_cut = not completed

    sid = next((m.session_id for m in markers if m.session_id), None)
    pid = next((m.get("participant_id") for m in markers if m.get("participant_id")), None)
    first, last = markers[0], markers[-1]
    day = fmt_date(first.get("t_unix"))
    out.line("Session", f"{pid or '?'} · {(sid or '?')[:12]}… · {day} "
                        f"{fmt_clock(first.get('t_unix'))} → {fmt_clock(last.get('t_unix'))} UTC")

    if head_cut:
        rep.add(FIX, "Recording began mid-session — there is no session_started marker.",
                f"The first marker in the file is {first.event} at {fmt_clock(first.get('t_unix'))} UTC"
                + (f" (block {first.block})." if first.block is not None else "."),
                "Hit Record in LabRecorder before the participant enters their ID.")
    if tail_cut:
        rep.add(UNUSABLE, "There is no session_completed marker.",
                f"The last marker is {last.event} at {fmt_clock(last.get('t_unix'))} UTC"
                + (f", in block {last.block}." if last.block is not None else "."),
                "Either Record was stopped early or the run was abandoned — the file alone "
                "cannot tell the two apart.")

    if started:
        rec = started[0]
        font = rec.get("ui_font")
        rig = (f"{rec.get('px_per_degree', '?')} px/deg · "
               f"{rec.get('screen_w_px', '?')}×{rec.get('screen_h_px', '?')} @ "
               f"{rec.get('viewing_distance_mm', '?')} mm · font {font or '?'}")
        out.line("Rig", rig)
        if rec.get("font_installed") is False or font == "SYSTEM-FALLBACK":
            rep.add(FIX, "The session ran on a system-font fallback, not the pinned webfont.",
                    "Every size in the interface is computed against that font's metrics, so "
                    "the visual-angle geometry in this recording is not valid for eye tracking.",
                    "Install the font and clear MOBI_ALLOW_SYSTEM_FONT before the next run.")

    return head_cut, tail_cut


def check_blocks(out: Out, rep: Report, markers: list[Marker],
                 head_cut: bool, tail_cut: bool) -> list[Marker]:
    """Replay every block against STEP_ORDER. Returns the block_started markers."""
    flow = [m for m in markers if m.event in FLOW_EVENTS or m.event == "llm_request_failed"]
    per_block: dict[int, list[Marker]] = {}
    for m in flow:
        if m.block is not None:
            per_block.setdefault(m.block, []).append(m)

    all_blocks = list(range(config.PRACTICE_BLOCK_INDEX, config.REAL_BLOCK_COUNT + 1))
    seen = sorted(per_block)
    if not seen:
        rep.add(UNUSABLE, "No block markers at all — this file contains no task blocks.")
        out.line("Blocks", "none")
        return []

    first_seen, last_seen = seen[0], seen[-1]
    absent_head = [b for b in all_blocks if b < first_seen]
    absent_tail = [b for b in all_blocks if b > last_seen]
    absent_mid = [b for b in all_blocks if first_seen < b < last_seen and b not in per_block]

    started = [m for m in flow if m.event == "block_started"]
    out.line("Blocks", f"{len(seen)}/{len(all_blocks)} present · "
                       f"{'practice + ' if config.PRACTICE_BLOCK_INDEX in per_block else ''}"
                       f"{len([b for b in seen if b != config.PRACTICE_BLOCK_INDEX])} real")

    if absent_head:
        level, why = ((NOTE, "not in this file, because Record started late") if head_cut else
                      (UNUSABLE, "missing, and the session did start from the beginning"))
        rep.add(level, f"{blocks_phrase(absent_head).capitalize()} "
                       f"{'is' if len(absent_head) == 1 else 'are'} {why}.")
    if absent_tail:
        level, why = ((NOTE, f"{'was' if len(absent_tail) == 1 else 'were'} never reached "
                             f"before the file ends") if tail_cut else
                      (UNUSABLE, f"{'is' if len(absent_tail) == 1 else 'are'} missing from a "
                                 f"session that reported completing"))
        rep.add(level, f"{blocks_phrase(absent_tail).capitalize()} {why}.")
    if absent_mid:
        rep.add(UNUSABLE, f"{blocks_phrase(absent_mid).capitalize()} "
                          f"{'is' if len(absent_mid) == 1 else 'are'} absent from the middle "
                          f"of the run.",
                "The flow cannot skip a block, so the recorder lost this stretch entirely.")

    dropouts: list[str] = []
    for block in seen:
        expected = expected_sequence(block)
        actual = actual_sequence(per_block[block])
        missing, extra = diff_sequence(expected, actual)
        boundary = (block == first_seen and head_cut) or (block == last_seen and tail_cut)

        if extra:
            reordered = sorted(missing) == sorted(extra)
            rep.add(UNUSABLE,
                    f"Block {block} has markers that do not fit the protocol sequence.",
                    ("They are the right markers in the wrong order." if reordered
                     else f"unexpected: {counted(Counter(extra))}"),
                    "" if reordered else f"missing: {counted(Counter(missing))}" if missing else "",
                    "A dropout can only remove markers, never add or reorder them, so this is "
                    "not a recording fault.")
        elif missing and not boundary:
            dropouts.append(f"block {block}: {counted(Counter(missing), limit=6)}")
        elif missing and boundary:
            rep.add(NOTE, f"Block {block} is partial ({plural(len(missing), 'marker')} short), "
                          f"which is the recording boundary rather than a fault.")

    if dropouts:
        rep.add(FIX, f"{plural(len(dropouts), 'block')} lost markers mid-session.",
                *dropouts,
                "The server's flow cannot skip a step, so these were dropped by the recorder, "
                "not by the task. Check the LSL link before the next participant.")
    return started


def _ranges(numbers: list[int]) -> str:
    """"1-4, 7" rather than "1, 2, 3, 4, 7"."""
    if not numbers:
        return ""
    out: list[str] = []
    start = prev = numbers[0]
    for n in numbers[1:] + [None]:  # type: ignore[list-item]
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(out)


def check_design(out: Out, rep: Report, started: list[Marker]) -> None:
    """Blocks 1-12 must cover every Category × Stance cell exactly once."""
    real = [m for m in started if m.block not in (None, config.PRACTICE_BLOCK_INDEX)]
    if not real:
        return

    cells = Counter((str(m.get("category")), str(m.get("stance"))) for m in real)
    categories = sorted({c for c, _ in cells})
    stances = sorted({s for _, s in cells})
    vignettes = Counter(str(m.get("vignette")) for m in real)

    complete = (len(real) == config.REAL_BLOCK_COUNT
                and len(categories) * len(stances) == config.REAL_BLOCK_COUNT
                and all(n == 1 for n in cells.values())
                and len(cells) == config.REAL_BLOCK_COUNT)

    # Within each stance, half the blocks' replies end on a question and half
    # do not (question_N in the assignment file).
    flagged = [m for m in real if m.get("ends_with_question") is not None]
    if len(flagged) == config.REAL_BLOCK_COUNT:
        per_stance: dict[str, Counter] = {}
        for m in flagged:
            per_stance.setdefault(str(m.get("stance")), Counter())[bool(m.get("ends_with_question"))] += 1
        unbalanced = [f"{s}: {c[True]} question / {c[False]} statement"
                      for s, c in sorted(per_stance.items()) if c[True] != c[False]]
        if unbalanced:
            rep.add(UNUSABLE, "Question and statement endings are not split evenly within each stance.",
                    *unbalanced,
                    "This participant's question_N columns in the assignment file are not a valid design.")

    shape = f"{len(categories)} categories × {len(stances)} stances"
    if complete:
        out.line("Design", f"{shape}, each cell exactly once")
        return

    if len(real) < config.REAL_BLOCK_COUNT:
        out.line("Design", f"{shape} over the {len(real)} real blocks whose block_started "
                           f"survives here — too few to confirm the crossing")
        return

    out.line("Design", f"{shape} — NOT a complete crossing")
    repeated = [f"{c}/{s} ×{n}" for (c, s), n in cells.items() if n > 1]
    empty = [f"{c}/{s}" for c in categories for s in stances if (c, s) not in cells]
    rep.add(UNUSABLE, "The 12 real blocks do not cross Category × Stance exactly once.",
            f"repeated: {', '.join(repeated)}" if repeated else "",
            f"never assigned: {', '.join(empty)}" if empty else "",
            "This participant's row in the assignment file is not a valid design.")

    dupes = [f"{k} ×{n}" for k, n in vignettes.items() if n > 1]
    if dupes:
        rep.add(UNUSABLE, f"A vignette was shown more than once: {', '.join(dupes)}.")


def check_llm(out: Out, rep: Report, markers: list[Marker]) -> None:
    replies = [m for m in markers if m.event == "llm_response_received"]
    failures = [m for m in markers if m.event == "llm_request_failed"]
    if not replies:
        # No turns at all is only a finding if some were started. A file that
        # simply does not reach an LLM turn is already explained by the block
        # checks above.
        if any(m.event == "llm_request_sent" for m in markers):
            rep.add(UNUSABLE, "LLM turns were requested but not one reply was recorded.")
        return

    latencies = [float(m.get("latency_ms", 0)) / 1000 for m in replies]
    out.line("LLM", f"{plural(len(replies), 'turn')} · median {statistics.median(latencies):.1f} s "
                    f"· max {max(latencies):.1f} s")

    # Every reply is meant to appear a fixed hold after the thinking indicator.
    # The browser's thinking_hidden marker says whether it did; a turn that
    # overran showed a longer wait than the others.
    hold_s = config.LLM_THINKING_HOLD_MS / 1000
    hidden = [m for m in markers if m.event == "thinking_hidden" and not m.get("failed")]
    if hidden:
        overran = [m for m in hidden if m.get("overran")]
        if overran:
            worst = max(float(m.get("reply_after_ms", 0)) for m in overran) / 1000
            out.cont(f"{len(overran)} overran the fixed {hold_s:.0f} s thinking hold")
            rep.add(NOTE, f"{plural(len(overran), 'reply')} arrived after the fixed {hold_s:.0f} s "
                          f"thinking hold, so those trials showed a longer wait than the others.",
                    f"slowest {worst:.1f} s · blocks "
                    f"{', '.join(str(m.block) for m in overran[:6])}",
                    "That is a difference across blocks the experiment did not ask for. Rewrite "
                    "rounds add a full model call each; see MOBI_LLM_EFFORT and "
                    "MOBI_LLM_MAX_REWRITES in app/config.py.")
        else:
            out.cont(f"all {len(hidden)} held the fixed {hold_s:.0f} s")
        off = [m for m in hidden if not m.get("overran")
               and abs(float(m.get("actual_hold_ms", 0)) - config.LLM_THINKING_HOLD_MS)
               > config.FIXATION_CROSS_TOLERANCE_MS]
        if off:
            rep.add(NOTE, f"{plural(len(off), 'thinking hold')} drifted more than "
                          f"{config.FIXATION_CROSS_TOLERANCE_MS} ms from {hold_s:.0f} s.",
                    "Browser timers were throttled, as in a backgrounded or non-fullscreen window.")
    else:
        out.cont("no thinking_hidden markers, so the fixed hold cannot be confirmed")

    # The reported latency is measured inside the server; the interval between the
    # two markers is measured by the recorder. The first can never exceed the
    # second, so if it does, the two clocks disagree.
    pending: Marker | None = None
    impossible: list[str] = []
    for m in markers:
        if m.event == "llm_request_sent":
            pending = m
        elif m.event == "llm_response_received" and pending is not None:
            recorded = m.ts - pending.ts
            reported = float(m.get("latency_ms", 0)) / 1000
            if reported > recorded + 0.05:
                impossible.append(f"block {m.block}: reported {reported:.2f} s inside a "
                                  f"{recorded:.2f} s gap")
            pending = None
    if impossible:
        rep.add(UNUSABLE, f"{plural(len(impossible), 'LLM turn')} "
                          f"{'reports' if len(impossible) == 1 else 'report'} taking longer "
                          f"than the recording says elapsed.",
                *impossible[:3],
                "The task's clock and the recorder's disagree, so marker times cannot be "
                "trusted against the other streams.")

    models = Counter(str(m.get("model")) for m in replies)
    if len(models) > 1:
        rep.add(FIX, f"{len(models)} different models answered within one session.",
                counted(models),
                "A fallback fired mid-run. MOBI_LLM_FALLBACK_MODELS should be empty for a "
                "study that must not silently change models.")
    else:
        out.cont(f"model {next(iter(models))}")

    rewritten = [m for m in replies if int(m.get("rewrites") or 0) > 0]
    if rewritten:
        out.cont(f"{len(rewritten)} needed a rewrite to comply")
    over_cap = [m for m in replies if m.get("over_cap")]
    if over_cap:
        rep.add(NOTE, f"{plural(len(over_cap), 'reply')} still broke the word cap or the block's "
                      f"question rule after {config.MAX_REWRITES} rewrites, and "
                      f"{'was' if len(over_cap) == 1 else 'were'} delivered as is.",
                f"blocks {', '.join(str(m.block) for m in over_cap[:6])}")
    over = [m for m in replies if int(m.get("word_count", 0)) > config.MAX_LLM_WORDS]
    if over:
        rep.add(FIX, f"{plural(len(over), 'reply')} "
                     f"{'was' if len(over) == 1 else 'were'} delivered over the "
                     f"{config.MAX_LLM_WORDS}-word cap.")
    mismatched = [m for m in replies if m.get("ends_with_question") is not None
                  and bool(m.get("elicited")) != bool(m.get("ends_with_question"))]
    if mismatched:
        rep.add(NOTE, f"{plural(len(mismatched), 'reply')} did not match the block's question "
                      f"condition.",
                f"blocks {', '.join(str(m.block) for m in mismatched[:6])}",
                "Those turns ended on a question in a statement block, or the reverse.")
    if failures:
        rep.add(NOTE, f"{plural(len(failures), 'LLM request')} failed and "
                      f"{'was' if len(failures) == 1 else 'were'} retried.",
                str(failures[0].get("error", ""))[:100])


def check_fixation(out: Out, rep: Report, markers: list[Marker]) -> None:
    held = [m for m in markers if m.event == "fixation_cross_hidden"]
    shown = [m for m in markers if m.event == "fixation_cross_shown"]
    expected = config.REAL_BLOCK_COUNT + 1
    if not held:
        out.line("Fixation", "no fixation-cross markers in this file")
        rep.add(FIX, "The inter-block fixation cross never fired.",
                "It is the drift-correction reference for gaze; without it there is no known "
                "screen position to calibrate against.")
        return

    drifts = [abs(float(m.get("drift_ms", 0))) for m in held]
    out_of_tol = [m for m in held if m.get("within_tolerance") is False]
    out.line("Fixation", f"{len(held)}/{expected} crosses · "
                         f"{len(held) - len(out_of_tol)} within "
                         f"±{config.FIXATION_CROSS_TOLERANCE_MS} ms · max drift "
                         f"{max(drifts):.0f} ms")

    if out_of_tol:
        blocks = sorted({m.block for m in out_of_tol if m.block is not None})
        rep.add(NOTE, f"{plural(len(out_of_tol), 'fixation cross')} "
                      f"{'was' if len(out_of_tol) == 1 else 'were'} held outside tolerance "
                      f"({blocks_phrase(blocks)}).",
                f"worst {max(float(m.get('drift_ms', 0)) for m in out_of_tol):+.0f} ms against a "
                f"{config.FIXATION_CROSS_MS} ms nominal hold",
                "Timer throttling — the window was backgrounded or not fullscreen.")
    if len(shown) != len(held):
        rep.add(NOTE, f"{len(shown)} crosses were shown but {len(held)} were recorded as hidden.")


def check_attention(out: Out, rep: Report, markers: list[Marker]) -> None:
    regained = [m for m in markers if m.event == "window_focus_regained"]
    lost = [m for m in markers if m.event == "window_focus_lost"]
    hidden = [m for m in markers if m.event == "visibility_changed"
              and m.get("visibility") == "hidden"]

    if lost or hidden:
        total_ms = sum(float(m.get("unfocused_ms", 0)) for m in regained)
        blocks = sorted({m.block for m in lost if m.block is not None})
        out.line("Attention", f"focus lost {len(lost)}× "
                              f"({fmt_duration(total_ms / 1000)} total)")
        rep.add(NOTE, f"The window lost focus {plural(len(lost), 'time')}"
                      + (f" ({blocks_phrase(blocks)})" if blocks else "") + ".",
                f"{fmt_duration(total_ms / 1000)} unfocused in total"
                + (f", and hidden {plural(len(hidden), 'time')}" if hidden else "") + ".",
                "Gaze cannot be trusted to correspond to on-screen content across those "
                "intervals — exclude them at analysis.")
    else:
        out.line("Attention", "window held focus throughout")

    for m in [m for m in markers if m.event == "presentation_mode_checked"]:
        if m.get("scale_mismatch"):
            rep.add(FIX, f"The display reported "
                         f"{m.get('screen_w')}×{m.get('screen_h')} but the rig expects "
                         f"{m.get('expected_screen_w')}×{m.get('expected_screen_h')}.",
                    "Display scaling is on, so px_per_degree — and every size derived from it — "
                    "is wrong for this recording. Set the monitor to its native resolution.")
        if m.get("fullscreen") is False:
            rep.add(FIX, "The browser was not fullscreen when the session started.",
                    f"viewport {m.get('viewport_w')}×{m.get('viewport_h')} against a "
                    f"{m.get('screen_w')}×{m.get('screen_h')} screen — browser chrome was "
                    f"on display, and timers throttle outside fullscreen.")
        break

    rejected = Counter(str(m.get("reason", "?")) for m in markers
                       if m.event == "submission_rejected")
    if rejected:
        rep.add(NOTE, f"The server rejected {plural(sum(rejected.values()), 'submission')}.",
                counted(rejected))
    # The client reports a numeric step's reason as the whole validator sentence,
    # which is far too long to tally next to short codes like below_min_words.
    blocked = Counter(_short(m.get("reason", "?")) for m in markers
                      if m.event == "validation_blocked")
    if blocked:
        rep.add(NOTE, f"The browser blocked {plural(sum(blocked.values()), 'input')} inline.",
                counted(blocked))


def check_coverage(out: Out, rep: Report, pyxdf, path: Path, markers: list[Marker],
                   marker_stream_id: int) -> None:
    """Can the other modalities actually be aligned against these markers?

    Three ways they cannot: a stream on a different clock, a stream that stops
    (or starts) inside the task, and a stream that recorded nothing at all.
    """
    streams, _ = pyxdf.load_xdf(str(path), synchronize_clocks=False,
                                dejitter_timestamps=False, handle_clock_resets=False)
    first, last = markers[0].ts, markers[-1].ts
    task = last - first

    wrong_clock: list[str] = []
    short: list[str] = []
    silent: list[str] = []

    for stream in streams:
        if int(stream["info"]["stream_id"]) == marker_stream_id:
            continue
        name = stream["info"]["name"][0]
        stamps = stream["time_stamps"]
        offsets = stream.get("clock_values") or []

        if len(stamps) == 0:
            silent.append(f"{name}: no samples at all" + (
                f", though it exchanged {len(offsets)} clock offsets — the outlet was "
                f"connected the whole time and never pushed anything" if offsets else ""))
            continue

        lo, hi = float(stamps[0]), float(stamps[-1])
        if abs(lo - first) > DIFFERENT_CLOCK_S:
            epoch = (" It is stamping wall-clock time (seconds since 1970) where LSL expects "
                     "lsl_local_clock()." if UNIX_EPOCH_RANGE[0] < lo < UNIX_EPOCH_RANGE[1] else "")
            wrong_clock.append(
                f"{name}: {fmt_duration(hi - lo)} of data, stamped from {lo:.0f} where the "
                f"markers are at {first:.0f}.{epoch}")
            continue

        late, early = lo - first, last - hi
        if late > 0.5 or early > 0.5:
            covered = max(0.0, min(hi, last) - max(lo, first))
            gaps = []
            if late > 0.5:
                gaps.append(f"starts {fmt_duration(late)} into the task")
            if early > 0.5:
                gaps.append(f"stops {fmt_duration(early)} before it ends")
            short.append(f"{name}: {' and '.join(gaps)} — it covers "
                         f"{covered / task * 100:.0f}% of the markers.")

    if wrong_clock:
        rep.add(FIX, f"{plural(len(wrong_clock), 'stream')} "
                     f"{'is' if len(wrong_clock) == 1 else 'are'} on a different clock from "
                     f"the markers.", *wrong_clock,
                "LSL's clock-offset records cannot correct this — they measure the difference "
                "between hosts, not a source stamping in the wrong time base. Nothing in that "
                "stream can be aligned to a marker without an ad-hoc offset. Fix the outlet "
                "before the next participant.")
    if short:
        rep.add(FIX, f"{plural(len(short), 'stream')} "
                     f"{'does' if len(short) == 1 else 'do'} not run for the whole task.", *short,
                "Markers outside those windows have no corresponding data.")
    if silent:
        rep.add(FIX, f"{plural(len(silent), 'stream')} recorded nothing.", *silent)

    ok = len(streams) - 1 - len(wrong_clock) - len(short) - len(silent)
    if wrong_clock or short or silent:
        out.line("Coverage", f"{ok} of {plural(len(streams) - 1, 'other stream')} "
                             f"{'aligns' if ok == 1 else 'align'} to the whole task")
    else:
        out.line("Coverage", f"all {plural(len(streams) - 1, 'other stream')} span "
                             f"the markers on the same clock")


SKIP_IN_TIMELINE = frozenset({"user_text_1", "user_text_2", "user_text_3", "t_lsl", "t_unix",
                              "t_iso", "session_id", "participant_id", "block_index", "event"})


def print_timeline(markers: list[Marker]) -> None:
    """Every marker, one per line, formatted like check_lsl.py --watch."""
    print()
    origin = markers[0].ts
    for m in markers:
        where = f"b{m.block}" if m.block is not None else "-"
        detail = ", ".join(
            f"{k}={_short(v)}" for k, v in m.rec.items()
            if k not in SKIP_IN_TIMELINE and v is not None and not isinstance(v, dict)
        )
        print(f"  {m.ts - origin:9.3f}  {m.event:<26} {where:<5} {detail[:88]}")
    print(f"\n{len(markers):,} markers. Participant free text is not shown.")


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 28 else text[:27] + "…"


# --- entry point ----------------------------------------------------------

def import_pyxdf():
    try:
        import pyxdf
    except Exception as exc:
        print("pyxdf is not installed, so no XDF file can be read.\n", file=sys.stderr)
        print('  uv pip install -e ".[analysis]"\n', file=sys.stderr)
        print(f"import error was: {exc}", file=sys.stderr)
        return None
    return pyxdf


def pick_marker_stream(rep: Report, inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    named = [s for s in inventory if s.get("name") == config.LSL_STREAM_NAME]
    if not named:
        typed = [s for s in inventory if s.get("type") == config.LSL_STREAM_TYPE]
        if not typed:
            return None
        rep.add(NOTE, f"No stream is named {config.LSL_STREAM_NAME}; using the "
                      f"{config.LSL_STREAM_TYPE} stream {typed[0].get('name')!r} instead.")
        named = typed[:1]

    if len(named) > 1:
        rep.add(UNUSABLE, f"{len(named)} separate outlets recorded as "
                          f"{config.LSL_STREAM_NAME}.",
                ", ".join(f"{s.get('hostname', '?')}/{str(s.get('uid', ''))[:8]}" for s in named),
                "Two servers were running at once. Their markers interleave in this recording "
                "and cannot be told apart. Checks below cover one of them only.")
    return named[0]


def keep_longest_session(rep: Report, markers: list[Marker]) -> list[Marker]:
    """One recording per participant is the intended unit. If a file holds more
    than one session, say so and check the longest -- reporting on an average of
    two participants would be worse than reporting on one of them."""
    sessions = Counter(m.session_id for m in markers if m.session_id)
    if len(sessions) <= 1:
        return markers
    keep, kept = sessions.most_common(1)[0]
    rep.add(FIX, f"This file contains {len(sessions)} sessions.",
            f"Everything below covers the longest one ({kept} of {sum(sessions.values())} "
            f"markers) and ignores the rest.",
            "One recording per participant is the intended unit — stop and restart Record "
            "between participants.")
    return [m for m in markers if m.session_id in (keep, None)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xdf", type=Path, help="the .xdf (or .xdfz) recording to check")
    ap.add_argument("--timeline", action="store_true",
                    help="print every marker after the summary")
    ap.add_argument("--no-coverage", action="store_true",
                    help="skip loading the other streams to check they cover the task; "
                         "the rest of the check only reads the marker stream, so this is "
                         "the flag for a very large recording in a hurry")
    args = ap.parse_args()

    if not args.xdf.is_file():
        print(f"No such file: {args.xdf}", file=sys.stderr)
        return 2

    pyxdf = import_pyxdf()
    if pyxdf is None:
        return 2

    rep = Report()
    inventory = pyxdf.resolve_streams(str(args.xdf))
    header = pick_marker_stream(rep, inventory)
    if header is None:
        print(f"{args.xdf.name} contains no {config.LSL_STREAM_NAME} stream.\n", file=sys.stderr)
        if inventory:
            print("It holds:", file=sys.stderr)
            for s in inventory:
                print(f"    {s.get('name')}  (type {s.get('type') or '?'}, "
                      f"{s.get('channel_count')} ch, {s.get('nominal_srate')} Hz)", file=sys.stderr)
        else:
            print("It holds no streams at all.", file=sys.stderr)
        return 2

    stream_id = int(header["stream_id"])
    stream, markers, bad = load_markers(pyxdf, args.xdf, stream_id)
    if not markers:
        print(f"The {config.LSL_STREAM_NAME} stream in {args.xdf.name} holds no readable "
              f"markers.", file=sys.stderr)
        return 2
    markers = keep_longest_session(rep, markers)

    out = Out()
    check_recording(out, rep, args.xdf, inventory, stream_id, stream, markers, bad)
    head_cut, tail_cut = check_session(out, rep, markers)
    started = check_blocks(out, rep, markers, head_cut, tail_cut)
    check_design(out, rep, started)
    check_llm(out, rep, markers)
    check_fixation(out, rep, markers)
    check_attention(out, rep, markers)
    if not args.no_coverage:
        check_coverage(out, rep, pyxdf, args.xdf, markers, stream_id)

    print(f"\n{header.get('name')} · {args.xdf.name}")
    print(f"\n  {rep.verdict()}\n")
    out.flush()
    rep.print_findings()
    if args.timeline:
        print_timeline(markers)
    print()
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
