# MoBI-MSM Test Fixtures

Representative (non-production) data for exercising the participant-facing UX described in `participant-ux-spec.md`. Content is illustrative, not the real scenario battery — swap these files for the real ones once the team finalizes that content, keeping the same column schemas.

## Files

**category_key.csv** — `category_label, category_name`. The 4 fixed categories.

**scenario_key.csv** — `scenario_label, category_label, scenario_text, question_text, scale_low_label, scale_high_label`. 12 example scenarios (3 per category), taken directly from the examples provided during spec development. `scale_low_label`/`scale_high_label` are the anchor text shown at the 0 and 100 ends of the Answer Score scale (e.g. "Very unlikely" / "Very likely") — this is now reflected in the main spec (Draft v3+) as part of the Question content model.

**stance_key.csv** — `stance_label, stance_name, system_prompt`. The 3 Stances, with their definition text standing in as the system prompt. The real system prompts the team develops may be more elaborate than this definition text; the column is a placeholder for whatever the finalized prompt content turns out to be.

**practice_scenario.csv** — the single fixed practice-block record (scenario, question, scale labels, and stance). Not part of the assignment CSV, since it's identical for every participant per the spec. Confirmed: the practice block always uses the `calibrated` Stance as a neutral default.

**assignment.csv** — `participant_id` plus 12 repeating `category_N, scenario_N, stance_N` triplets. Three test participants (TEST001, TEST002, TEST003), each with all 4 categories × 3 stances covered exactly once, using a distinct scenario for every one of a category's 3 occurrences (no repeated scenario within a participant's session), in a different block order per participant. TEST003 is structurally identical to TEST001; its only purpose is to appear in `completed_sessions.csv` so the Start Screen's "already run" branch has something to test against.

**completed_sessions.csv** — `participant_id, completed_at`. A stand-in for whatever session-output record the real system checks to determine if a Participant ID has already been run. Only TEST003 appears here — TEST001 and TEST002 should behave as "not yet run" at the Start Screen.

## Scaling up

This set has exactly 3 scenarios per category — enough for one participant to see 12 distinct scenarios with no repeats, but every participant assigned so far draws from the same 12. For a real sample, the battery will need more than 3 per category so different participants (or the same participant across counterbalancing positions) aren't all seeing identical scenario sets — that's still the research team's task, not something these fixtures need to solve.
