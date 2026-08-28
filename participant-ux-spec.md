# MoBI-MSM Participant-Facing UX Specification

Status: Draft v8 — display rig corrected to 27" QHD (2560×1440) at 600 mm; sizing now specified in degrees of visual angle and derived from the rig
Last updated: 2026-08-26

## 1. Purpose & Scope

This document specifies the participant-facing user interface for the MoBI-MSM protocol's core scenario-response task. It covers everything the participant and the in-room research assistant (RA) see and do during that task, from session start to session end. It does not cover informed consent, demographics, unrelated tasks bundled into the same visit, or the LSL/data-synchronization backend beyond noting what needs to be logged — those are handled elsewhere or are implementation details left to Claude Code.

The interface is a chat-style web application (visual model: Claude/ChatGPT-style transcript), designed for a fixed 2560×1440 browser window on a 27-inch QHD display viewed from approximately 600 mm — no responsive or mobile layout is required. Because the screen is recorded and eye tracking is collected concurrently, the presentation is specified in **degrees of visual angle rather than pixels**: text size, spacing between messages, and the separation between distinct interface elements are all defined as angular quantities and resolved to pixels from the display geometry, so that a fixation can be attributed to a specific line and a specific element at typical tracker accuracy (see Section 10). Changing the monitor or the seating distance therefore changes only the pixel resolution of those quantities, not the design; the angular specification is the invariant. The transcript is scoped to a single block: within a block it is one continuous scrollable conversation, and advancing to the next block clears it, so each block is self-contained and no earlier block remains reachable on screen. The participant operates the interface directly; the RA sits in the same room and may assist with pacing or progression at their discretion. There is no separate RA-only interface — RA and participant share the one screen.

## 2. Terminology

| Term | Definition |
|---|---|
| Category | One of four scenario types: Know/Understand, Decide/Get Advice, Reflect/Make Meaning, Seek Support/Help. |
| Stance | The hidden system-prompt posture assigned to the LLM for a given block: Aligning, Calibrated, or Counterbalancing. |
| Scenario | The standardized written prompt the participant reads at the start of a block. Authored by the team separately from this spec. |
| Question | The standardized numeric-judgment question tied to a Scenario (e.g., "How likely is it that..."), displayed together with the 0/100 scale's anchor labels (e.g., "0 = Very unlikely / 100 = Very likely"). |
| Block | One full pass through the flow in Section 5, tied to one Scenario (and its Question) and one Stance. |
| Answer Score 1 / 2 | Participant's 0–100 numeric response to the Question, collected once before and once after the text exchange. |
| Confidence Score 1 / 2 | Participant's 0–100 numeric confidence in their Answer Score, collected alongside each Answer Score. |
| Activation Score | Participant's 0–100 numeric self-rated physical/emotional activation, collected once at the end of each block. |
| User Text Response (1–3) | The participant's three free-text turns within a block's LLM exchange. |
| LLM Text Response (1–2) | The two LLM-generated replies (≤50 words each) within a block's exchange, styled by that block's Stance. |

## 3. Session Structure

1. **Start Screen**
2. **Practice Block** (full structure, dedicated practice-only scenario, data logged and flagged as practice)
3. **Block 1 → Block 12**, each pairing one of the 4 Categories with one of the 3 Stances (all 12 combinations covered once per participant), separated by an inter-block continue gate. Category/Stance-to-block order is pre-assigned per participant (externally generated) to balance ordinal position across the sample.
4. **End Screen**

This entire sequence — Start Screen through End Screen — runs in a single continuous sitting; there is no multi-day or multi-session split. (If a future protocol variant needs to split a participant's 12 blocks across multiple sessions, the intended approach is to suffix the Participant ID in the assignment CSV, e.g. `1234a` / `1234b`, rather than changing this UI's flow.)

There is no mid-session "long break" screen. The RA manages pacing informally, using the inter-block gate (Section 5, step 15) as the natural pause point, and may encourage the participant to move faster or slower as needed.

## 4. Participant Assignment & Randomization

Block order and content are driven by a pre-generated **assignment file** (CSV), created by the research team outside this UI, with one row per Participant ID and 12 repeating column triplets — `category_1, scenario_1, stance_1` through `category_12, scenario_12, stance_12` — each cell holding a lookup label, not the actual content. Three separate **key files, also CSVs**, resolve those labels at runtime:

- **Category key**: category label → full Category name (e.g., Know/Understand).
- **Scenario key**: scenario label → full Scenario text, its associated Question text, and the Question's 0/100 scale anchor labels (e.g., "Very unlikely" / "Very likely" — these vary per Question and are not a fixed "0–100" caption).
- **Stance key**: stance label → the hidden system-prompt content for that Stance.

Validation and error-checking of the assignment file and key files (e.g., confirming a row's category label actually matches the category tied to its assigned scenario in the Scenario key) is handled separately by the research team and is out of scope for this UI (Section 11).

The Practice Block's scenario is fixed and identical for every participant, so it has no column in the assignment CSV — it's referenced directly as a single fixed record, not looked up per participant.

**Participant ID entry** happens on the Start Screen (Section 9): the RA types a Participant ID into a free-text field. On submit:

- If the ID is not found in the assignment file, the field shows an inline error and blocks proceeding, consistent with the inline-validation pattern used elsewhere (Section 6).
- If the ID is found but already has completed session data on record, the screen shows an inline notice that this participant has already been run and gives the RA the opportunity to enter a different ID. The system never proceeds with, or overwrites, an already-run participant's data.
- If the ID is found and has not yet been run, that row is loaded, resolved against the three key files, and used to drive Category/Scenario/Stance content and order for that participant's 12 blocks.

## 5. Screen-by-Screen Flow (per Block)

Practice and real blocks follow the identical structure below; only the Scenario/Question content and Stance assignment differ, and the practice block's data is flagged separately (Section 8). Each block begins on a cleared transcript and its steps accumulate as one scrollable conversation until the block ends.

| # | Element | Type | Participant Action | Data Captured |
|---|---|---|---|---|
| 1 | Scenario | Chat message | Read | scenario_id, block_index, timestamp_shown |
| 2 | Question | Chat message (appears together with #1); displays the Question text plus its 0/100 scale anchor labels | Read | question_id, timestamp_shown |
| 3 | Answer Score 1 prompt + input | Numeric input, 0–100 integer | Type score, submit | answer_score_1, timestamp_submitted |
| 4 | Confidence Score 1 prompt + input | Numeric input, 0–100 integer | Type score, submit | confidence_score_1, timestamp_submitted |
| 5 | User Text Response 1 prompt (dynamically templated with Answer Score 1) + input | Free-text input, ≥15 words | Type response, submit | user_text_1, word_count, timestamp_submitted |
| 6 | Thinking indicator | Non-message UI state | Wait | — (see Section 7 for logging of real latency) |
| 7 | LLM Text Response 1 | Chat message, ≤50 words | Read | llm_text_1, stance, timestamp_shown, model latency (from LSL-logged request/response times) |
| 8 | User Text Response 2 prompt + input | Free-text input, ≥15 words | Type response, submit | user_text_2, word_count, timestamp_submitted |
| 9 | Thinking indicator | Non-message UI state | Wait | — |
| 10 | LLM Text Response 2 | Chat message, ≤50 words | Read | llm_text_2, timestamp_shown, model latency |
| 11 | User Text Response 3 prompt + input | Free-text input, ≥15 words | Type response, submit | user_text_3, word_count, timestamp_submitted |
| 12 | Answer Score 2 prompt (re-shown as a new chat message restating the Question) + input | Numeric input, 0–100 integer | Type score, submit | answer_score_2, timestamp_submitted |
| 13 | Confidence Score 2 prompt + input | Numeric input, 0–100 integer | Type score, submit | confidence_score_2, timestamp_submitted |
| 14 | Activation Score prompt + input | Numeric input, 0–100 integer | Type score, submit | activation_score, timestamp_submitted |
| 15 | Inter-block continue gate; advancing clears the transcript so the next block opens on a clean screen (Section 6) | Chat-style inline element with button + Enter-key equivalent | Click or press Enter | timestamp_advanced |

After Block 12's step 14, the flow goes directly to the End Screen — there is no step 15 gate after the final block. A brief inter-block fixation cross additionally appears before every block's Scenario, including the Practice Block's; see Section 12.

## 6. Input Behavior Specifications

**Numeric score fields** (Answer Score 1/2, Confidence Score 1/2, Activation Score): whole numbers 0–100 only, no decimals. Invalid entries (non-numeric, out-of-range, decimal) are blocked inline before submission — the field shows an error state and cannot be submitted until corrected. Pressing Enter submits and advances only if the current value is valid. A submit button is also present as an alternative to Enter throughout.

**Free-text fields** (User Text Response 1–3): Enter submits the response; Shift+Enter inserts a newline. A submit button is available as an alternative to Enter. Minimum length is 15 words, computed as a whitespace-delimited token count, enforced with the same inline-blocking pattern as numeric fields (submission blocked, error state shown, until the minimum is met). No maximum length and no time limit. Copy/paste is disabled in these fields to protect the integrity of the keystroke-level (keydown/keyup) data stream — participants must type their response directly.

**Navigation**: strictly forward-only throughout the entire session. There is no mechanism to revisit or edit a previously submitted score or text response, and no "back" affordance appears anywhere in the UI.

**Transcript scope**: the visible transcript covers only the current block. Within a block the participant can scroll freely over that block's scenario, prompts, own responses, and LLM replies. Advancing through the inter-block gate (Section 5, step 15) clears the transcript before the next block's Scenario is shown, so a previous block's content cannot be re-read, re-scrolled, or compared against the current one. This is a display rule only — clearing the screen never discards data, and every message and value remains in the record described in Section 10.

**Continue/gate elements**: styled as chat-native inline elements (message + button), not full-screen interstitials, consistent with the rest of the interface. Used only between blocks (step 15) and on the Start/End screens (Section 9).

## 7. LLM Turn Handling

**Stance assignment**: each block is assigned one of three Stances as a hidden system prompt, invisible to the participant. Across a participant's 12 real blocks, all 4 Categories × 3 Stances combinations occur exactly once. The Stance shapes tone only (Aligning reinforces the participant's articulated position where plausible/safe; Calibrated acknowledges it while preserving uncertainty/tradeoffs/alternatives; Counterbalancing respectfully introduces competing evidence or interpretations) — participants are never shown which Stance is active.

**Context**: each block's LLM conversation is independent. The LLM has only the current block's Scenario, Question, and text/score turns exchanged so far within that block — there is no carryover of context from one block to the next.

**Response length & delivery**: each LLM Text Response is capped at 50 words and is revealed in full at once — no token-by-token streaming.

**Thinking indicator**: immediately after a User Text Response that triggers an LLM turn (steps 6 and 9 only — there is no LLM turn after User Text Response 3), a "thinking" icon appears. If the real response hasn't arrived within roughly 7 seconds (a tunable constant, not a hard cutoff), the indicator's text changes to a low-key "hang on, still thinking" state. This indicator is UI chrome only: it is never added to the chat transcript, never included in the model's context, and never something the participant can interact with. It is fully replaced by the real LLM Text Response whenever that response actually arrives — the UI does not force a synthetic reveal time or substitute placeholder content into the conversation.

**Ground-truth timing**: actual request-sent and response-received timestamps for every LLM turn are logged as LSL-synchronized events, giving real per-turn latency data independent of the UI's visual smoothing treatment.

**Dynamic templating**: several prompts must interpolate the participant's own just-submitted values at runtime rather than using static copy — most notably User Text Response 1's prompt, which reads back the participant's Answer Score 1 (e.g., "You rated that [Answer Score 1] out of 100. What made you give that score?").

**Failure handling**: true LLM/API failures (errors, timeouts with no eventual response) are explicitly out of scope for this spec and are left to implementation-level engineering judgment, along with retry logic and error logging.

## 8. Practice Block

Runs once, immediately after the Start Screen and before Block 1. Uses a dedicated practice-only Scenario and Question that sit outside the four real Categories, so it does not consume or interfere with the Category × Stance assignment for the 12 real blocks. Structurally identical to a real block (full 15-step sequence in Section 5, including a Stance-driven LLM exchange). Its LLM exchange always uses the Calibrated Stance, fixed for every participant (a neutral default, since the practice block isn't part of the balanced Category × Stance design). Data from the practice block is saved using the same schema as real blocks, tagged with a block-type/is_practice flag so it can be excluded from primary analysis.

## 9. Session Bookends

**Start Screen**: static instructional text, a Participant ID entry field (lookup/validation behavior defined in Section 4), and a "Start" button that becomes available once a valid, not-yet-run Participant ID is loaded. Advances into the Practice Block.

**End Screen**: static thank-you text plus a "Quit" button. Shown immediately after Block 12's Activation Score is submitted (no inter-block gate precedes it).

## 10. Data & Logging Notes

- Every discrete on-screen event (message shown, score submitted, text submitted, LLM response shown, thinking-indicator state change, block transition) should correspond to a marker pushed to LSL, synchronized with the EEG, eye tracking, ECG/EMG/EDA/respiration, motion capture, video, screen recording, and keystroke streams already being collected.
- **AOI geometry**: because gaze is captured in screen coordinates, every marker that changes what is on screen also records the on-screen rectangle of the affected elements, expressed in the same viewport frame as the screen recording, together with the current scroll offset, viewport dimensions, and device pixel ratio. The session record additionally carries the display geometry in force for that run — pixels per degree, viewing distance, pixel pitch, and screen dimensions — so recorded rectangles can be converted to degrees of visual angle using only what is in the data, and a session run on a mis-configured display is identifiable after the fact. This is what allows fixations to be assigned to a specific Scenario, Question, prompt, LLM response, or the participant's own prior answer during analysis. Participant-initiated scrolling within a block is likewise recorded, since it changes the mapping from screen position to content.
- Per-block metadata to persist: participant ID, block index/order, category, stance, is_practice flag, scenario ID, question ID, and every score/text value and timestamp listed in the Section 5 table.
- Any UI behavior that could corrupt the independently-captured keystroke stream (paste, autofill, autocomplete, spellcheck-driven auto-replace) should be disabled in text-entry fields. Keystroke-level (keydown/keyup) data is captured by a separate existing tool and is not duplicated in this UI's own data capture.

## 11. Explicitly Out of Scope for This UI

- Informed consent, demographics, and other unrelated tasks bundled into the same visit — handled elsewhere.
- Retry logic, error handling, and logging for failed or slow LLM calls, beyond the thinking-indicator treatment in Section 7.
- Session resume after a browser crash, refresh, or navigation away.
- A secondary RA-only control interface — the RA operates through the same participant-facing screen.
- Any mandatory or scheduled "long break" screen mid-way through the 12 blocks.
- Validation and error-checking of the assignment file and key files (e.g., category-label consistency checks) — handled separately by the research team, not by this UI.
## 12. Presentation & Recording Integrity Addendum

This section adds four requirements to the presentation layer, on top of Section 10's AOI-geometry logging, aimed at keeping the concurrently-recorded gaze, video, and physiological streams trustworthy over a full ~12-block sitting.

**Inter-block fixation cross.** Immediately before every block's Scenario (Section 5, step 1) is shown — including before the Practice Block's Scenario, and after every inter-block gate (step 15) is advanced — the transcript area is replaced for 500 ms by a single central fixation cross (a small "+", ~1° of visual angle, rendered against the same background as every other screen) with no other content on screen. It is non-interactive and not skippable; the participant simply waits the 500 ms before the block's Scenario renders as normal. There is no fixation cross before the End Screen, since Block 12 goes directly to the End Screen without a next block's Scenario to prepare for.

Its purpose is a per-block drift-correction reference: a known, fixed, identical screen position that recurs 13 times across the session (practice + 12 blocks) gives analysts a zero-point to correct for calibration drift accumulating over the sitting, independent of anything already logged in Section 10. Its shown/hidden transitions are logged the same way as any other on-screen change (Section 10): a marker carrying its on-screen rectangle, timestamp, scroll offset (always 0, since it displaces the transcript), viewport size, and device pixel ratio.

**Font rendering determinism.** The interface embeds exactly one webfont locally (served from the app's own static assets, never a CDN or Google Fonts request, consistent with the no-external-dependency posture already required for session reliability) rather than relying on the OS system-font stack. The page waits for that font to finish loading (via the Font Loading API) before the Start Screen — and therefore any subsequent screen — is shown, rather than allowing a fallback-font flash to reflow text after first paint.

This matters because every size in the interface's stylesheet is computed against a specific font's character metrics — cap height, line height, and the character-based column measure. A system-font stack resolves to a different font, with different metrics, on every OS, and can even change after an OS update on the same machine, silently invalidating the visual-angle math without anything in the UI visibly breaking. Pinning one embedded font makes line-wrap points, line spacing, and AOI boundaries identical across every participant and every session.

**Window/focus integrity.** The browser runs fullscreen/kiosk at the display's native resolution with no toolbar, tabs, or bookmarks bar visible, and OS-level notifications suppressed for the session. Native resolution matters because the angular sizing above is computed from the monitor's physical pixel geometry: if the operating system or browser applies display scaling, the browser lays out in a different pixel count and every visual-angle quantity is silently wrong. The client compares the reported display dimensions against the configured rig at session start, records the comparison, and warns the RA in the status bar on a mismatch. Beyond that, so nothing not authored by this interface can appear over the recorded and gaze-calibrated screen. In addition, the client listens for visibility, blur, and focus transitions and pushes a marker on each one (event name, timestamp, and — for the regained-focus marker — the duration the window was unfocused), so any interval where gaze data cannot be trusted to correspond to on-screen content (participant tabbed away, an OS overlay stole focus, etc.) is explicitly flagged in the record rather than silently present in the gaze stream. This is logging only — it does not pause the flow, block input, or add any participant-facing interruption, consistent with the forward-only, no-resume design (Section 11).

Because there is no separate RA interface (Section 1), a focus-loss event also surfaces briefly in the existing status bar (the same low-contrast, RA-facing chrome used for pacing) rather than introducing a new UI surface, so the RA can react in the room without the participant's screen itself changing in a way that would become a new AOI event.

**Luminance stability.** Background luminance is fixed and identical across every screen and every state — Start Screen, Practice Block, all 12 blocks, the thinking indicator, the inter-block gate, the fixation cross, and the End Screen — with no flashing, strobing, sudden brightness changes, or full-screen color washes at any point in the session. This is a hard constraint on future visual changes, not just the current palette: nothing (a celebratory End Screen flourish, a colored success/error wash, a dark-mode toggle) may alter overall screen luminance mid-session.

The reason is that gaze is collected continuously and pupil diameter is frequently derived alongside it; a luminance change triggers the pupillary light reflex, which would appear in the eye-tracking data as noise indistinguishable from the cognitive or emotional response the protocol is actually trying to measure. Keeping luminance flat removes that confound by construction rather than requiring it to be modeled out during analysis.
