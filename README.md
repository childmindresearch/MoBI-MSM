# MoBI-MSM Participant Task

The participant-facing scenario-response task described in
[participant-ux-spec.md](participant-ux-spec.md): a single continuous,
chat-style web application for a fixed 2560×1440 browser window, run by the
participant with the RA in the room on the same screen.

One session is a Start Screen, a practice block, Blocks 1–12, and an End
Screen. Each block runs the identical 15-step sequence in Section 5 of the
spec, tied to one Scenario and one hidden Stance.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. For development
without a key, set `MOBI_LLM_PROVIDER=fake` instead — a deterministic canned
responder that exercises the entire flow offline.

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8420
```

Open `http://127.0.0.1:8420` and enter a Participant ID (`TEST001` or
`TEST002` in the bundled fixtures).

Before a collection day, confirm the real API path end to end:

```bash
.venv/bin/python scripts/check_llm.py --repeat 5
```

That prints the actual reply, its word count, and the latency spread the
participant would wait — worth checking against the 7 s threshold at which the
thinking indicator changes text.

## How it fits together

| File | Role |
|---|---|
| `app/content.py` | Loads `assignment.csv` and the three key files; resolves one participant into a practice block plus 12 blocks. |
| `app/session.py` | The per-block state machine (Section 5). Owns block index and step position. |
| `app/llm.py` | Stance system prompts, block-local context, the 50-word cap, and the two providers. |
| `app/store.py` | SQLite persistence and CSV/JSON export. |
| `app/markers.py` | LSL marker emission with a JSONL mirror. |
| `app/prompts.py` | All participant-facing copy, in one place. |
| `app/static/` | The browser interface (no build step, no dependencies). |

**The server owns the flow.** The browser is told only what to render next and
what single input to collect. It never receives the Stance, the assignment
row, or anything about upcoming blocks, so the hidden manipulation cannot leak
through the client and the forward-only sequence cannot be skipped from it.
`tests/test_flow.py::test_stance_never_reaches_the_client` asserts this.

**Blocks are self-contained.** Opening a block clears the transcript, so a
participant can scroll within the block they are on but never back into a
previous block's scenario or exchange. The boundary is signalled by the server
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
| Scenario line spacing | 1.35° | 60.5 px (37.4 px type) | The primary reading stimulus. |
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
  message id and kind (`scenario`, `prompt`, `llm`, `user-numeric`,
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
Scenario — including the practice block, and after every inter-block gate, so
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
| `MOBI_LLM_PROVIDER` | `anthropic` | `fake` for development with no key. |
| `MOBI_LLM_MODEL` | `claude-opus-5` | |
| `MOBI_LLM_EFFORT` | `low` | Lower effort means lower and steadier latency. |
| `MOBI_FIXTURES_DIR` | `test-fixtures` | Point at the real battery when it exists. |
| `MOBI_DATA_DIR` | `data` | |
| `MOBI_FAKE_LLM_DELAY_S` | `0` | Artificial delay for rehearsing the thinking indicator. |
| `MOBI_ALLOW_SYSTEM_FONT` | unset | Development only — run without the pinned webfont. Never set for collection. |
| `MOBI_SCREEN_DIAGONAL_IN` | `27.0` | Display rig — drives all visual-angle sizing. |
| `MOBI_SCREEN_W_PX` / `_H_PX` | `2560` / `1440` | Monitor **native** resolution. |
| `MOBI_VIEWING_DISTANCE_MM` | `600` | Participant eye-to-screen distance. Measure it. |

Protocol constants — the 15-word minimum, the 50-word cap, the 0–100 range,
the 7 s thinking threshold, and the 500 ms fixation-cross hold — are named at
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
