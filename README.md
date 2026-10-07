# MoBI-MSM Participant Task

The participant-facing scenario-response task described in
[participant-ux-spec.md](participant-ux-spec.md): a single continuous,
chat-style web application for a fixed 2560×1440 browser window, run by the
participant with the RA in the room on the same screen.

One session is a Start Screen, a practice block, Blocks 1–12, and an End
Screen. Each block runs the identical 15-step sequence in Section 5 of the
spec, tied to one vignette (a short scenario plus an appraisal statement rated 0–100) and one hidden Stance.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set the API key for whichever vendor you are
using — model calls go through [LiteLLM](https://docs.litellm.ai/), so the
vendor is named in `MOBI_LLM_MODEL` (`anthropic/claude-opus-5`, `openai/gpt-5`,
`gemini/gemini-2.5-pro`, `ollama/llama3.3`, …) and the credential is that
vendor's own variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …). For
development without a key, set `MOBI_LLM_PROVIDER=fake` instead — a
deterministic canned responder that exercises the entire flow offline.

Changing model means changing `MOBI_LLM_MODEL` and nothing else; the model each
session ran against is written into its `sessions` row, so a switch mid-study is
visible in the data rather than silent.

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8420
```

Open `http://127.0.0.1:8420` and enter a Participant ID (`TEST001` or
`TEST002` in the bundled fixtures).

Before a collection day, confirm the real model path end to end:

```bash
.venv/bin/python scripts/check_llm.py --repeat 5
```

Add `--model openai/gpt-5` to try a different model without touching `.env` —
useful when comparing latency across candidates.

That prints the actual reply, its word count, how many rewrite rounds it took,
and the model's latency — worth checking against the fixed 7 s thinking hold
(see below); it warns about any reply that would have overrun it.

Check both turns and both question conditions before a collection day:

```bash
.venv/bin/python scripts/check_llm.py --turn 2 --repeat 5
.venv/bin/python scripts/check_llm.py --statement --repeat 5
```

### Reply shape: the question flag and the rewrite loop

User Text Response 2 and 3 have no prompt message of their own; the input box
follows the LLM reply directly. Whether that reply ends on a question is a
per-block manipulation: `question_N` in `assignment.csv` (1 or 0), split so
that within each stance half the blocks end on a question and half on a
statement. `scripts/check_xdf.py` flags a recording whose split is uneven.

Two things about every reply are enforced by a loop rather than trusted to the
prompt: it is at most 75 words, and it follows the block's question rule. A
draft that misses either is handed back to the model with the problem named
(`REWRITE_INSTRUCTION` and the `PROBLEM_*` sentences in `app/llm.py`) and a
request to rewrite it complete. Nothing is ever cut by code. After
`MOBI_LLM_MAX_REWRITES` rounds (default 2) the shortest attempt goes out as is.

- `llm_text_N_rewrites`, `llm_text_N_over_cap` and `llm_text_N_drafts` (every
  draft, as JSON) record what happened per turn, and `llm_text_N_elicited`
  records whether the delivered reply ended on a question, so the delivered
  shape can be checked against `ends_with_question` in the data.
- Each rewrite round is a full model call, so it counts against the thinking
  hold.
- `PROMPT_VERSION` in `app/llm.py` is recorded on every block row; bump it with
  any change to the prompt wording or the stance prompts.
- The input placeholder (`app/prompts.py`, `TEXT_PLACEHOLDER`) is what marks the
  box as the participant's turn in statement blocks.

## How it fits together

| File | Role |
|---|---|
| `app/content.py` | Loads `assignment.csv` and the three key files (`category_key.csv`, `vignettes_appraisals.csv`, `stance_key.csv`); resolves one participant into a practice block plus 12 blocks. |
| `app/session.py` | The per-block state machine (Section 5). Owns block index and step position. |
| `app/llm.py` | Stance system prompts, block-local context, the per-turn guidance, the rewrite loop that enforces the 75-word cap and the block's question rule, and the two providers (LiteLLM and a fake). |
| `app/store.py` | SQLite persistence and CSV/JSON export. |
| `app/markers.py` | LSL marker emission with a JSONL mirror. Every marker and field is described in [MARKER_KEY.md](MARKER_KEY.md). |
| `scripts/check_xdf.py` | Spot-checks one finished recording against the protocol. |
| `app/prompts.py` | All participant-facing copy, in one place. |
| `app/static/` | The browser interface (no build step, no dependencies). |

**The server owns the flow.** The browser is told only what to render next and
what single input to collect. It never receives the Stance, the assignment
row, or anything about upcoming blocks, so the hidden manipulation cannot leak
through the client and the forward-only sequence cannot be skipped from it.
`tests/test_flow.py::test_stance_never_reaches_the_client` asserts this.

**Blocks are self-contained.** Opening a block clears the transcript, so a
participant can scroll within the block they are on but never back into a
previous block's vignette or exchange. The boundary is signalled by the server
(`reset_transcript` on the view that opens a block) rather than inferred by the
browser, and is covered by
`tests/test_flow.py::test_blocks_are_self_contained`.

**Nothing is cacheable.** Static assets are served `no-store` and the page
references them through a build token that changes with their contents. A
kiosk holding a stale `app.js` against a newer server is the kind of failure
that only surfaces mid-session.

## Data

Three destinations, written as the session progresses rather than at the end,
so an interrupted run keeps everything collected up to that point:

- **`data/sessions.db`** — the durable record. `sessions` (also the "already
  run" check), `blocks` (one wide row per block with every value and
  timestamp), and `events` (the full marker log).
- **`data/markers/<session_id>.jsonl`** — every marker, mirrored from the LSL
  stream. Written whether or not LSL is available.
- **`data/exports/`** — written automatically when a session completes, and on
  demand:

```bash
.venv/bin/python scripts/export.py --participant TEST001
```

### LSL

Markers go to a `MoBI-MSM-Markers` string outlet (irregular rate, one JSON
object per sample) for synchronization with the EEG, eye tracking,
ECG/EMG/EDA/respiration, motion capture, video, screen recording, and
keystroke streams.

`pylsl` is an optional dependency. Without it the app still runs and still
writes every marker to JSONL, logging a warning at startup — check that
startup line before collecting real data:

```bash
uv pip install -e ".[lsl]"
```

If pylsl installs but the import still fails, the native library is missing
rather than the Python package: `brew install labstreaminglayer/tap/lsl`.

#### In LabRecorder

The outlet is opened by the **server**, at startup — not by the browser, and
not when a participant logs in. The browser is an HTTP client of the server and
never speaks LSL, so every marker it reports (`messages_rendered`, scrolls,
focus loss) is pushed by the server on its behalf. Practically: start the server
once at the beginning of a collection day, hit Update in LabRecorder, and the
stream stays listed across participants. One recording per participant is the
natural unit, matching one `session_id`.

| Field | Value |
|---|---|
| Name | `MoBI-MSM-Markers` |
| Type | `Markers` |
| Channels | 1, `string` |
| Sample rate | 0 (irregular) |
| source_id | `mobi-msm-participant-ui` |

The stable `source_id` is what lets LabRecorder reconnect to the same logical
stream if the server restarts mid-recording. Its flip side is that two servers
running at once are indistinguishable to a recorder — don't.

Because a missing pylsl degrades silently to JSONL-only, confirm the path the
same way you confirm the model path:

```bash
.venv/bin/python scripts/check_lsl.py
```

With the server down that opens a self-test outlet through `app/markers.py`
itself, resolves it back, and round-trips a marker. With the server up it
reports the live outlet exactly as LabRecorder shows it, lists the other streams
on the network, and exits non-zero if two outlets are advertising the same name.
Add `--watch 120` to print markers as they arrive while you click through the
task.

#### After a session

`scripts/check_xdf.py` reads one finished recording and reports whether it
followed the protocol — while the participant is still in the room, and in
time to fix the rig before the next one:

```bash
.venv/bin/python scripts/check_xdf.py sub-TEST001_ses-01.xdf
```

It needs the XDF and nothing else, so it runs on a copied-off recording
anywhere: `uv pip install -e ".[analysis]"`. Findings are sorted by what an RA
can do about them — problems that make the recording unusable, things to fix
before the next participant (system-font fallback, display scaling, Record
started late, a recorder dropping markers), and things to note and move on
from (focus loss, fixation-cross drift, a slow LLM turn). It exits 0 clean, 1
for notes, 2 for anything to fix.

The protocol it checks against is imported from `app/session.py` and
`app/config.py` rather than restated, so editing the flow updates the check
with it. That matters for one inference in particular: because the flow is
server-owned and forward-only, the task *cannot* skip a step, so a marker
missing from the middle of an intact block is a recorder dropout rather than a
protocol violation — and is reported as one. Markers that are out of order or
duplicated cannot come from a dropout at all, and are reported more seriously.

It also checks the *other* streams, which is where a full-modality rig tends
to fail quietly: a stream that stops inside the task, one that recorded
nothing at all, and — the one that is invisible without looking — a source
stamping in a different time base. LSL's clock-offset records only correct for
the difference between hosts, so an outlet that stamps `time.time()` where
`lsl_local_clock()` was expected lands about 1.8e9 seconds from every other
stream and cannot be aligned to a marker at all. That check loads the whole
file; `--no-coverage` skips it on a very large recording in a hurry.

`--timeline` prints every marker, participant free text excluded.

The file is read with pyxdf's clock synchronisation **off**. Markers are
stamped with the same `t_lsl` carried in their payload, and that equality is
one of the things the check reports on; `synchronize_clocks=True` — pyxdf's
default — rewrites the timestamps and breaks it.

Marker timestamps are pinned to the same `t_lsl` written into the JSONL, so the
mirror and the XDF carry identical numbers rather than values captured
microseconds apart. That is what makes the JSONL alignable against the other
streams if a recording was started late or missed entirely.

Two caveats on precision. Browser-owned events are stamped when the server
receives the POST, not at browser paint — sub-millisecond on loopback, but not
frame-accurate; the screen recording is the better reference for that. And
`app_started` fires before anyone has hit Record, so it appears in the JSONL and
not in the XDF.

`llm_text_N_requested_at` / `_received_at` are recorded as ground-truth
request/response times, independent of anything the UI does visually.

## Sizing for eye tracking

The screen is recorded and gaze collected concurrently, so **sizes are
specified in degrees of visual angle, not pixels** — what matters is how large
something is to the eye.

The rig is defined once, at the top of `app/config.py`:

| | |
|---|---|
| Monitor | 27" QHD, 2560×1440 native |
| Pixel pitch | 0.2335 mm/px (derived) |
| Viewing distance | 600 mm |
| **1° of visual angle** | **44.85 CSS px** (derived) |

`PX_PER_DEGREE` is computed from those four numbers, served to the browser, and
applied as the CSS variable `--deg`. Every size in `app/static/styles.css` is a
multiple of it — `calc(var(--deg) * 1.222)` reads directly as 1.222°. **Moving
to a different monitor or seating distance is a change in `app/config.py` and
nowhere else**; override via `MOBI_SCREEN_DIAGONAL_IN`, `MOBI_SCREEN_W_PX`,
`MOBI_SCREEN_H_PX`, and `MOBI_VIEWING_DISTANCE_MM`.

Typical tracker accuracy is 0.5–1°, so the layout targets:

| Property | Degrees | At this rig | Why |
|---|---|---|---|
| Body line spacing | 1.24° | 55.8 px (34.9 px type) | A fixation resolves to one line, not a band of three. |
| Vignette line spacing | 1.35° | 60.5 px (37.4 px type) | The primary reading stimulus. |
| Gap between messages | 1.22° | 55 px | Adjacent message AOIs don't bleed together. |
| Fixation cross | 1.00° | 44.9 px | Section 12 specifies exactly one degree. |
| Column width | 35.6° | 1595 px | 483 px side margins keep content out of the periphery. |
| Line length | ~61 chars | — | Capped in `ch`; the wide column separates AOIs rather than stretching text. |
| Participant vs. system messages | right vs. left | ~1000 px apart | Makes the two AOI classes unambiguous. |

The status bar is deliberately small and low-contrast: it carries pacing
information for the RA and should not attract participant fixations.

### Display scaling is the thing that breaks this

`PX_PER_DEGREE` is computed from the monitor's **native** resolution, and the
browser lays out in **CSS** pixels. If the OS or browser applies display
scaling the two diverge and every figure above is silently wrong.

At session start the client compares `window.screen` against the configured rig
and records `presentation_mode_checked` with `scale_mismatch`. On a mismatch the
RA gets a status-bar warning naming both resolutions. It warns rather than
blocks — but **a mismatch means the sizing is invalid, so fix it before
collecting.** Either set the display to native resolution, or update the rig
config to match what the browser actually reports.

### AOI geometry markers

Gaze arrives in screen coordinates, so AOIs are only recoverable if the
on-screen position of each element is known for the moment it was visible.
Three markers carry that geometry, all viewport-relative (the same frame as the
screen recording) and all stamped with scroll offset, viewport size, and device
pixel ratio:

- `messages_rendered` — every message's rectangle as it appears, keyed by
  message id and kind (`vignette`, `prompt`, `llm`, `user-numeric`,
  `user-text`, `interstitial`, `gate`).
- `input_focused` — the entry area and its field, which move as the composer grows.
- `transcript_scrolled` — trailing-throttled, emitted when the participant
  scrolls within a block, so gaze still maps to content after the view moves.

`session_started` additionally records `px_per_degree`, `viewing_distance_mm`,
`pixel_pitch_mm`, and the screen dimensions, so pixel rectangles convert to
degrees of visual angle using only what is in the data.

Because `messages_rendered` and `input_focused` each carry the scroll offset,
position is captured at every content change even if no scroll marker fires.

## Presentation & recording integrity (Section 12)

Four requirements protect the concurrently-recorded gaze, video, and
physiological streams over a full sitting.

### Inter-block fixation cross

A 1° cross (44.9 px at this rig) is held centre-screen for 500 ms before every block's
vignette — including the practice block, and after every inter-block gate, so
13 times per session. It is a full-viewport overlay on the same background, so
nothing else is on screen and luminance does not change. There is no cross
before the End Screen, since Block 12 goes straight there.

It gives analysts a known, identical screen position recurring through the
session to correct accumulated calibration drift against. Both transitions are
logged with the cross's rectangle, viewport, device pixel ratio, and a scroll
offset of 0 (it displaces the transcript).

The hold is timed from the frame the cross is **painted** and ended on a frame
boundary, and `fixation_cross_hidden` records `nominal_hold_ms`,
`actual_hold_ms`, `drift_ms`, and `within_tolerance`. Browsers throttle both
`requestAnimationFrame` and timers in a backgrounded or non-fullscreen window,
which stretches the hold — so a block whose cross ran long is identifiable at
analysis time instead of being assumed correct. Tolerance is
`FIXATION_CROSS_TOLERANCE_MS` (50 ms).

### Font determinism

The interface pins its metrics to a single webfont embedded in
`app/static/fonts/ui.woff2` — **Inter v4.1 variable**, SIL Open Font License,
committed to the repo and served locally, never a CDN or Google Fonts request.
Provenance and licence are in `app/static/fonts/README.md`. The page waits for that face via the Font Loading API before the Start
Screen appears, and `font-display: block` keeps a fallback from ever being
painted.

This matters because every size in the stylesheet is computed against one
font's character metrics — cap height, line height, and the `ch`-based column
measure. A system-font stack resolves differently per OS and can change after
an OS update, silently invalidating the visual-angle math with nothing visibly
broken.

The font is committed because a session cannot legitimately run without it.
With it missing,
the server logs an error, `POST /api/session` returns 503, and the browser
shows a "Setup incomplete" screen instead of the Start Screen. For development
only, `MOBI_ALLOW_SYSTEM_FONT=1` runs anyway on a system fallback; every
session records which face it actually used (`ui_font` on `session_started`),
so a fallback run can never be silently analysed as if it had used the pinned
face.

### Window and focus integrity

Run the browser fullscreen with no toolbar, tabs, or bookmarks bar, and
suppress OS notifications for the session:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --kiosk --disable-notifications --disable-session-crashed-bubble --no-first-run --user-data-dir=/tmp/mobi-kiosk http://localhost:8420
```

On macOS also turn on Do Not Disturb. The client logs `blur`, `focus`, and
`visibilitychange` transitions, with the unfocused duration on the regain
marker, so any interval where gaze cannot be trusted to correspond to on-screen
content is flagged rather than silently present. This is **logging only** — it
never pauses the flow or blocks input.

At session start the client also records `presentation_mode_checked`
(comparing window to screen dimensions). Toolbars can't be detected directly,
but a window smaller than the screen means chrome is showing; when that
happens, and on every focus loss, a notice appears briefly in the existing
status bar — the same low-contrast RA-facing chrome used for pacing, so the
participant's screen gains no new AOI event.

### Luminance stability

Background luminance is identical across every screen and state. `--surface`
is only a hair lighter than `--bg` — cards are delineated by borders, not fill
brightness — so average screen luminance barely moves as card count changes,
and `color-scheme: light` stops the UA rendering dark form controls on a
dark-mode host.

Gaze is collected continuously and pupil diameter often derived alongside it, so
a luminance change triggers the pupillary light reflex and appears as noise
indistinguishable from the response being measured. **This is a hard constraint
on future changes**: no flashing, strobing, sudden brightness changes,
full-screen colour washes, success/error tints, or dark-mode toggle.

## Keystroke-stream integrity

Keydown/keyup is captured by a separate tool, so this UI must not introduce
text the participant did not type. In every text field: paste, drop, cut,
copy, autocomplete, autocorrect, autocapitalize, and spellcheck are disabled,
along with Grammarly/1Password/LastPass overlay attributes, and page text is
not selectable. Paste is blocked on both the `paste` event and `beforeinput`,
which covers menu and IME routes.

## Configuration

Everything is environment-driven; `.env` is read at startup and real
environment variables take precedence over it.

| Variable | Default | Notes |
|---|---|---|
| `MOBI_LLM_PROVIDER` | `litellm` | `fake` for development with no key. `anthropic` is accepted as an old name for `litellm`. |
| `MOBI_LLM_MODEL` | `anthropic/claude-opus-5` | Any LiteLLM model id, `vendor/model`. |
| `MOBI_LLM_EFFORT` | unset | Reasoning effort, sent only to vendors that accept it. Unset by default — LiteLLM maps it to extended thinking, which *raises* latency; see below. |
| `MOBI_LLM_FALLBACK_MODELS` | unset | Comma-separated models tried in order if the primary fails. |
| `MOBI_LLM_API_BASE` / `_API_KEY` | unset | Route everything through a LiteLLM proxy instead of calling vendors directly. Requires a `litellm_proxy/…` model id — see below. |
| `MOBI_FIXTURES_DIR` | `test-fixtures` | Point at the real battery when it exists. |
| `MOBI_DATA_DIR` | `data` | |
| `MOBI_FAKE_LLM_DELAY_S` | `0` | Artificial delay for rehearsing the thinking indicator. |
| `MOBI_ALLOW_SYSTEM_FONT` | unset | Development only — run without the pinned webfont. Never set for collection. |
| `MOBI_SCREEN_DIAGONAL_IN` | `27.0` | Display rig — drives all visual-angle sizing. |
| `MOBI_SCREEN_W_PX` / `_H_PX` | `2560` / `1440` | Monitor **native** resolution. |
| `MOBI_VIEWING_DISTANCE_MM` | `600` | Participant eye-to-screen distance. Measure it. |

### The fixed thinking hold and reasoning effort

Every LLM reply appears exactly `LLM_THINKING_HOLD_MS` (7 s, `app/config.py`)
after the thinking indicator does, timed in the browser. A reply that comes
back sooner is held; one that comes back later appears on arrival. The
`thinking_hidden` marker records the actual hold, when the reply arrived, and
whether it overran, and `scripts/check_xdf.py` lists any trial that did — its
wait was longer than the others'.

`MOBI_LLM_EFFORT` is unset by default, deliberately. LiteLLM maps it onto
extended thinking — a heavier mechanism than the `output_config` effort it
replaced — so setting it makes replies *slower*, not faster. Measured against
one gateway:

| `MOBI_LLM_EFFORT` | min | max | mean |
|---|---|---|---|
| `low` | 5.1 s | 8.2 s | 6.7 s |
| unset | 2.4 s | 5.9 s | 3.7 s |

That matters because the hold is 7 s. With effort set, the hold sits inside
the latency spread, so some trials overrun it and show a longer wait — a
difference across blocks the experiment did not ask for. Unset, a first draft
clears it with room to spare, but a rewrite round adds a full call on top.

Re-measure with `scripts/check_llm.py --repeat 5` when changing model or
gateway; the margin is not large.

### Going through a LiteLLM proxy

If the lab runs a LiteLLM gateway, the vendor keys stay on the proxy and the
machine holds only a virtual key. That takes three settings together, not two:

```
MOBI_LLM_API_BASE=https://litellm.your-org.example
MOBI_LLM_API_KEY=sk-...
MOBI_LLM_MODEL=litellm_proxy/claude-opus-5
```

The `litellm_proxy/` prefix is what routes the call to the gateway, and the name
after it is the model **as configured on the proxy** — not the vendor's own id.
Set `MOBI_LLM_API_BASE` with, say, `anthropic/claude-opus-5` instead and LiteLLM
sends Anthropic-shaped requests to the proxy's URL, which is not a route it
serves; the app logs a warning at startup when it sees that combination.

Protocol constants — the 1-word minimum (word counter hidden via `SHOW_WORD_COUNT`), the 75-word cap, the 0–100 range,
the fixed 7 s thinking hold, and the 500 ms fixation-cross hold — are named at
the top of `app/config.py`.
Participant-facing wording lives in `app/prompts.py`.

## Swapping in the real content

Replace the four CSVs in `test-fixtures/` (or point `MOBI_FIXTURES_DIR`
elsewhere), keeping the column schemas documented in
[test-fixtures/README.md](test-fixtures/README.md). Validating those files is
the research team's job and deliberately out of scope here (spec Section 11);
the app checks only that every label resolves, and refuses to start a session
whose assignment row is broken rather than rendering blanks mid-run.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers a full 13-block session, both Start Screen rejection branches, the
Section 6 input rules, out-of-order submissions, dynamic templating, per-block
persistence, the stance-leak assertion, self-contained blocks, marker-identity
spoofing, the Section 12 presentation markers, the font gate, the rig geometry
math, and a marker directory vanishing mid-session.

## Deliberately not built

Per Section 11 of the spec: consent and demographics, assignment-file
validation, a separate RA interface, a scheduled long break, and session
resume after a crash or refresh — a reloaded page cannot rejoin a session and
will say so.
