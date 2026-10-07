# MoBI-MSM Marker Key

What every marker in the `MoBI-MSM-Markers` stream means, when it fires, and
what its fields contain. Written against the code on this branch
(`app/main.py`, `app/session.py`, `app/static/app.js`); if a marker or field
changes there, update this file with it.

## Where markers live

The same record goes to three places:

| Where | Notes |
|---|---|
| LSL stream `MoBI-MSM-Markers` (in the XDF) | One string sample per marker, holding a JSON object. The sample timestamp equals the record's `t_lsl`. |
| `data/markers/<session_id>.jsonl` | One JSON object per line. Written even without LSL. |
| `data/sessions.db`, `events` table | Every marker that belongs to a session. |

## Fields on every marker

| Field | Meaning |
|---|---|
| `event` | The marker name. Look it up in the [reference](#marker-reference) below. |
| `t_lsl` | LSL clock time (seconds). Use this to align with EEG, eye tracking, physiology, and so on. `null` if LSL was not installed. |
| `t_unix` | Wall-clock time, in seconds since 1970 (UTC). |
| `t_iso` | The same wall-clock time as an ISO 8601 string, in UTC with milliseconds. |
| `session_id` | One run of the task. One XDF recording per participant should match one `session_id`. |
| `participant_id` | The ID entered on the Start Screen, e.g. `PILOT_001`. |
| `block_index` | `0` = practice block, `1`–`12` = real blocks. `null` before a session exists. |
| `source` | Present and set to `"client"` only on markers the **browser** reported (see below). Absent on markers the server generated. |

**Server vs. browser markers.** Server markers are stamped when the server
records something, such as a submitted answer or an LLM reply arriving.
Browser markers (`source: "client"`) cover things only the browser can see,
such as what was painted on screen, focus loss, and scrolling. They are
stamped when the server *receives* the browser's report. On the lab machine
that is under a millisecond after the event, but it is not frame-accurate. For
frame-level onsets, use the screen recording.

## Order of markers in a session

```
app_started                      (server start; JSONL only, before Record is pressed)
session_started
block_started                    block_index 0, the practice block
  ... one block, as below ...
block_started                    block_index 1
  ...
block_started                    block_index 12
  ... (no gate in block 12)
session_completed
end_screen_shown
```

Within every block, the **flow markers** always come in this order (from
`STEP_ORDER` in `app/session.py`):

| # | Marker | `step` | What the participant did or saw |
|---|---|---|---|
| 1 | `block_started` | — | Block opens. The fixation cross shows, then the vignette card (scenario, instruction, appraisal statement, and 0–100 anchors). |
| 2 | `submission_recorded` | `answer_1` | Rated agreement with the appraisal (0–100), first time |
| 3 | `submission_recorded` | `confidence_1` | Rated confidence in that rating (0–100) |
| 4 | `submission_recorded` | `text_1` | Typed why they gave that score ("You rated that N out of 100…") |
| 5 | `llm_request_sent` | `llm_1` | Thinking indicator appears |
| 6 | `llm_response_received` | — | First AI reply arrives at the server |
| 7 | `submission_recorded` | `text_2` | Typed a reply to the AI |
| 8 | `llm_request_sent` | `llm_2` | Thinking indicator appears |
| 9 | `llm_response_received` | — | Second AI reply arrives at the server |
| 10 | `submission_recorded` | `text_3` | Typed a final reply (a short one is allowed) |
| 11 | `submission_recorded` | `answer_2` | Rated agreement with the same appraisal again (0–100) |
| 12 | `submission_recorded` | `confidence_2` | Rated confidence again (0–100) |
| 13 | `submission_recorded` | `activation` | Rated current physical and emotional activation (0 = completely calm, 100 = extremely activated) |
| 14 | `submission_recorded` | `gate` | Pressed Continue at the "end of this part" screen. **Not present in block 12**, which goes straight to the End Screen. |

Presentation markers (`messages_rendered`, `input_focused`,
`fixation_cross_*`, `thinking_*`, and so on) appear between these flow
markers, as the screen changes.

## Marker reference

### Session lifecycle

| Marker | Source | When it fires | Fields |
|---|---|---|---|
| `app_started` | server | Server starts, usually once per collection day. It fires before anyone presses Record, so it is in the JSONL but **not in the XDF**. | `app_version`, `llm_provider`, `llm_model`, `lsl` (true if the LSL outlet is live), `participants` (number of rows in the assignment file) |
| `participant_lookup_failed` | server | The Start Screen rejected an ID. There is no session yet, so this marker goes only to `data/markers/no-session.jsonl` and the XDF, not the database. | `reason`: `not_found` (ID is not in the assignment file) or `already_run` (ID already completed a session) |
| `session_started` | server | Valid ID accepted, and the session is created. | `block_count` (13 = practice + 12), `ui_font` (font name, or `SYSTEM-FALLBACK`), `font_installed`, and the display rig needed to convert pixels to degrees: `px_per_degree`, `viewing_distance_mm`, `pixel_pitch_mm`, `screen_w_px`, `screen_h_px`, `screen_diagonal_in` |
| `start_screen_shown` | client | The task screen replaces the Start Screen. (Despite the name, this marks the Start Screen being *left*.) | `font` (font in use, or `SYSTEM-FALLBACK`), `font_ok` |
| `presentation_mode_checked` | client | Once, at session start. Checks fullscreen and display scaling. | `fullscreen`, `screen_w`, `screen_h`, `expected_screen_w`, `expected_screen_h`, `scale_mismatch` (**true means the visual-angle sizing is invalid for this session**), `px_per_degree`, `viewing_distance_mm`, `resolved_deg_px` (pixels per degree as the browser actually applied it), plus the [viewport fields](#viewport-fields) |
| `session_completed` | server | The block 12 activation rating was recorded. The session is done. | — |
| `end_screen_shown` | client | The End Screen is displayed. | — |

### Block flow

| Marker | Source | When it fires | Fields |
|---|---|---|---|
| `block_started` | server | A block opens, at session start for the practice block and after each gate for the rest. | `is_practice`, `category` ([category code](#category-codes)), `vignette` (vignette ID, e.g. `DEC-04`, or `PRACTICE`), `stance` ([stance code](#stance-codes)), `ends_with_question` (true if this block's AI replies were required to end on a question) |
| `submission_recorded` | server | The server accepted an answer. `block_index` is the block the answer belongs to, so a gate submission is stamped with the block it closes. | `step` ([step names](#step-names)), plus the value submitted. Numeric steps: `answer_score_1`, `confidence_score_1`, `answer_score_2`, `confidence_score_2`, or `activation_score`. Text steps: `user_text_N` (**the participant's free text**) and `user_text_N_words`. Gate: no value. |
| `submission_rejected` | server | The server refused an answer that got past the browser's checks. This should be rare. | `step`, `reason` (the message shown to the participant) |

### LLM turns

| Marker | Source | When it fires | Fields |
|---|---|---|---|
| `llm_request_sent` | server | The server asks the model for a reply. | `step`: `llm_1` or `llm_2` |
| `llm_response_received` | server | The reply is back and accepted. The participant sees it later, when the 7 s thinking hold ends (see `thinking_hidden`). | `latency_ms` (total model time, including any rewrite rounds), `rewrites` (rewrite rounds needed; 0 = the first draft complied), `over_cap` (true if it was still non-compliant after the maximum number of rewrites and went out anyway), `ends_with_question` (what the block *required*), `elicited` (whether the delivered reply *actually* ended on a question), `word_count`, `model`, `provider`, `requested_at`, `received_at`. The reply text is not in the marker; it is in the `blocks` table as `llm_text_N`. |
| `llm_request_failed` | server | The model call errored. The participant sees an error notice with a retry, and the retry produces a new `llm_request_sent`. | `error` |
| `thinking_shown` | client | The "Thinking" indicator appears. | `hold_ms` (nominal hold, 7000) |
| `thinking_hidden` | client | The indicator disappears and the reply is about to render. | `hold_ms`, `actual_hold_ms` (how long it was really on screen), `reply_after_ms` (when the reply arrived, relative to the indicator), `overran` (true if the reply took longer than the hold, so this trial's wait was longer than others'), `failed` (true if the request errored) |

### What is on screen (AOI geometry)

These markers let you map gaze to content. All rectangles are
**viewport-relative CSS pixels** (`x`, `y`, `w`, `h`), in the same frame as the
screen recording. Convert to degrees with `px_per_degree` from
`session_started`.

| Marker | Source | When it fires | Fields |
|---|---|---|---|
| `fixation_cross_shown` | client | A 1° cross is painted at screen center, before every block's vignette (13 times per session). | `geometry.cross` (rectangle), `hold_ms` (nominal, 500), `scroll_top` (always 0), plus the [viewport fields](#viewport-fields) |
| `fixation_cross_hidden` | client | The cross is removed and the vignette is painted. | `nominal_hold_ms`, `actual_hold_ms`, `drift_ms` (actual minus nominal), `within_tolerance` (true if within ±50 ms) |
| `messages_rendered` | client | One or more messages were painted. Fires after the browser actually draws them. | `message_ids`, `kinds` ([message kinds](#message-kinds)), `geometry` (one rectangle per message: `id`, `kind`, `x`, `y`, `w`, `h`), plus the [viewport fields](#viewport-fields) |
| `input_focused` | client | An input appears and takes focus (number box, text box, or Continue button). | `step`, `type` (`numeric`, `text`, or `gate`), `geometry.entry` (the whole entry area), `geometry.field` (the box or button itself), plus the [viewport fields](#viewport-fields) |
| `transcript_scrolled` | client | The participant scrolled within the block. Throttled to one marker per settled scroll. | The [viewport fields](#viewport-fields) |

### Recording integrity

| Marker | Source | When it fires | Fields |
|---|---|---|---|
| `window_focus_lost` | client | The task window lost focus or was hidden. **Gaze is not trustworthy from here** until `window_focus_regained`. | `reason`: `blur` or `hidden`, plus the [viewport fields](#viewport-fields) |
| `window_focus_regained` | client | Focus came back. | `reason`: `focus` or `visible`, `unfocused_ms` (how long focus was gone), plus the [viewport fields](#viewport-fields) |
| `visibility_changed` | client | The browser tab became hidden or visible. | `visibility`: `hidden` or `visible` |
| `validation_blocked` | client | The browser stopped an input or showed an error. The session continues. | `reason`, plus extras depending on the reason (see below) |

`validation_blocked` reasons:

| `reason` | Meaning | Extra fields |
|---|---|---|
| `paste_blocked` | Tried to paste, drop, cut, or copy in an input box | `kind` (e.g. `paste`, `drop`, `insertFromPaste`) |
| `below_min_words` | Submitted an empty text box | `step`, `words` |
| *an error message*, e.g. `Enter a number.` | Submitted an empty, non-whole, or out-of-range number | `step` |
| `request_failed` | A request to the server failed, and the participant saw "Something went wrong" | `detail` (the error text) |

## Value lookups

### Viewport fields

Carried on the markers noted above, so that rectangles can be placed after
any scrolling.

| Field | Meaning |
|---|---|
| `scroll_top` | How far the transcript is scrolled, in px. Add it to a rectangle's `y` to get its position within the content. |
| `scroll_height` | Total height of the transcript content, in px |
| `viewport_w`, `viewport_h` | Browser viewport size, in CSS px |
| `device_pixel_ratio` | Should be 1 on the rig. Any other value means display scaling is on. |

### Step names

| `step` | What it is | Block column written |
|---|---|---|
| `answer_1` | Agreement with the appraisal, before the AI exchange (0–100) | `answer_score_1` |
| `confidence_1` | Confidence in `answer_1` (0–100) | `confidence_score_1` |
| `text_1` | Free text: why they gave that score | `user_text_1` |
| `llm_1` | First AI reply | `llm_text_1` |
| `text_2` | Free text: reply to the AI | `user_text_2` |
| `llm_2` | Second AI reply | `llm_text_2` |
| `text_3` | Free text: final reply to the AI | `user_text_3` |
| `answer_2` | Agreement with the same appraisal, after the AI exchange (0–100) | `answer_score_2` |
| `confidence_2` | Confidence in `answer_2` (0–100) | `confidence_score_2` |
| `activation` | Current physical and emotional activation (0–100) | `activation_score` |
| `gate` | Pressed Continue between blocks | `advanced_at` |

### Message kinds

| `kind` | What it is |
|---|---|
| `vignette` | The vignette card: scenario text, instruction, appraisal statement in quotes, and the 0–100 anchors |
| `interstitial` | The practice intro before block 0, and "The session starts now" before block 1 |
| `prompt` | A standard prompt from the task, such as "Enter a whole number…" or the confidence and activation questions |
| `llm` | An AI reply |
| `user-numeric` | The participant's own number, shown back in the transcript |
| `user-text` | The participant's own text, shown back in the transcript |
| `gate` | The "That's the end of this part…" message before Continue |

### Category codes

From `category_key.csv`. The letters are also the prefix of each vignette ID.

| `category` | Name |
|---|---|
| `INF` | Know/Understand |
| `DEC` | Decide/Get Advice |
| `REF` | Reflect/Make Meaning |
| `SUP` | Seek Support/Help |

### Stance codes

From `stance_key.csv`. This is the hidden manipulation: the stance the AI was
told to take. The practice block always uses `calibrated`.

| `stance` | What the AI was told to do |
|---|---|
| `aligning` | Develop and support the participant's own position, substantively and without flattery |
| `calibrated` | Weigh the participant's view in proportion to the evidence |
| `counterbalancing` | Raise one defensible consideration that challenges the participant's conclusion |

## Handle with care

- **Unblinding.** `block_started` carries the `stance`. Anyone watching the
  stream live (for example `scripts/check_lsl.py --watch`) can see the
  condition.
- **Participant text.** `submission_recorded` for `text_1`, `text_2`, and
  `text_3` contains the participant's free text verbatim. Treat the JSONL, the
  XDF, and the database as identifiable. `scripts/check_xdf.py --timeline`
  leaves free text out.
- **Rejected IDs.** Every ID typed at the Start Screen that failed the lookup,
  typos included, is in `participant_lookup_failed`.
