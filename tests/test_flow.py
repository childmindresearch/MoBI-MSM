"""End-to-end exercise of the participant flow."""

import json
import re

import pytest

from app import config

WORDS = "I think this depends a great deal on the specific person and their particular circumstances here."
assert len(WORDS.split()) >= config.MIN_RESPONSE_WORDS


def _run_block(client, sid, view):
    """Drive one block from its opening view to whatever comes after it."""
    while True:
        if view.get("done"):
            return view
        if view.get("awaiting_llm"):
            view = client.post(f"/api/session/{sid}/llm").json()
            continue
        spec = view["input"]
        if spec["type"] == "numeric":
            value = 55
        elif spec["type"] == "text":
            value = WORDS
        else:
            value = True
        r = client.post(f"/api/session/{sid}/submit", json={"step": spec["step"], "value": value})
        assert r.status_code == 200, r.text
        view = r.json()
        if spec["step"] == "gate":
            return view


def test_full_session(client):
    assert client.post("/api/participant/lookup", json={"participant_id": "TEST001"}).status_code == 200

    r = client.post("/api/session", json={"participant_id": "TEST001"})
    assert r.status_code == 200, r.text
    view = r.json()
    sid = view["session_id"]
    assert view["is_practice"] is True
    assert view["input"]["step"] == "answer_1"

    seen_blocks = []
    for _ in range(config.REAL_BLOCK_COUNT + 1):
        seen_blocks.append(view["block_index"])
        view = _run_block(client, sid, view)
        if view.get("done"):
            break

    assert view["done"] is True
    assert seen_blocks == list(range(0, config.REAL_BLOCK_COUNT + 1))

    # Section 4: a completed participant is never re-run.
    r = client.post("/api/participant/lookup", json={"participant_id": "TEST001"})
    assert r.status_code == 409
    assert r.json()["status"] == "already_run"


def test_lookup_branches(client):
    r = client.post("/api/participant/lookup", json={"participant_id": "NOPE"})
    assert r.status_code == 404 and r.json()["status"] == "not_found"

    r = client.post("/api/participant/lookup", json={"participant_id": "TEST002"})
    assert r.status_code == 200


def test_numeric_validation(client):
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    for bad in ["", "abc", "50.5", "-1", "101"]:
        r = client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": bad})
        assert r.status_code == 422, f"{bad!r} should have been rejected"
    assert client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": "0"}).status_code == 200


def test_text_minimum_words(client):
    """The minimum is one word: only an empty response is blocked."""
    assert config.MIN_RESPONSE_WORDS == 1
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 40})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 60})
    r = client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": "   "})
    assert r.status_code == 422 and r.json()["detail"] == "Please type a response."
    assert client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": "Yes"}).status_code == 200


def test_text_minimum_is_still_enforced_when_raised(client, monkeypatch):
    """The word floor is switched down, not removed: raising it brings it back."""
    monkeypatch.setattr(config, "MIN_RESPONSE_WORDS", 15)
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 40})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 60})
    r = client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": "too short"})
    assert r.status_code == 422 and "15 words" in r.json()["detail"]
    assert client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": WORDS}).status_code == 200


def test_word_count_indicator_hidden(client):
    cfg = client.get("/api/config").json()
    assert cfg["show_word_count"] is False
    assert cfg["min_response_words"] == 1


def test_out_of_order_submission_rejected(client):
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    r = client.post(f"/api/session/{sid}/submit", json={"step": "activation", "value": 50})
    assert r.status_code == 409


def test_answer_1_prompt_is_templated(client):
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 73})
    view = client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 60}).json()
    assert "73 out of 100" in view["messages"][0]["text"]


def test_stance_never_reaches_the_client(client):
    """The hidden manipulation must not appear in any client-visible payload."""
    import json

    sid = client.post("/api/session", json={"participant_id": "TEST001"}).json()["session_id"]
    view = client.get("/api/config").json()
    payloads = [json.dumps(view)]

    v = client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 50}).json()
    payloads.append(json.dumps(v))
    v = client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 50}).json()
    payloads.append(json.dumps(v))
    v = client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": WORDS}).json()
    payloads.append(json.dumps(v))
    v = client.post(f"/api/session/{sid}/llm").json()
    payloads.append(json.dumps(v))

    blob = " ".join(payloads).lower()
    # Word-boundary matched: "distance" legitimately contains "stance", and a
    # substring check would flag the rig's viewing_distance_mm as a leak.
    for term in ["aligning", "calibrated", "counterbalancing", "stance", "system_prompt"]:
        assert not re.search(rf"\b{term}\b", blob), f"{term!r} leaked to the client"


def test_data_is_persisted_per_block(client):
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 12})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 34})
    client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": WORDS})
    client.post(f"/api/session/{sid}/llm")

    row = dict(get_store().block_rows(sid)[0])
    assert row["is_practice"] == "1"
    assert row["answer_score_1"] == "12"
    assert row["confidence_score_1"] == "34"
    assert row["user_text_1_words"] == str(len(WORDS.split()))
    assert row["llm_text_1"]
    assert int(row["llm_text_1_latency_ms"]) >= 0
    assert row["llm_text_1_rewrites"] == "0"
    assert row["llm_text_1_over_cap"] == "0"
    assert json.loads(row["llm_text_1_drafts"]) == [row["llm_text_1"]]
    assert row["stance_label"] == "calibrated"  # practice block is always Calibrated
    assert row["vignette_label"] == "PRACTICE"
    assert row["appraisal_text"]
    assert row["ends_with_question"] == "1"
    assert row["prompt_version"]


def test_blocks_are_self_contained(client):
    """Only the view that opens a block resets the transcript, so a participant
    can never scroll back into a previous block."""
    view = client.post("/api/session", json={"participant_id": "TEST002"}).json()
    sid = view["session_id"]

    # The practice block's opening view clears whatever came before it.
    assert view["reset_transcript"] is True

    resets = []
    while True:
        if view.get("done"):
            break
        if view.get("awaiting_llm"):
            view = client.post(f"/api/session/{sid}/llm").json()
            resets.append((view["block_index"], view["reset_transcript"]))
            continue
        spec = view["input"]
        value = 50 if spec["type"] == "numeric" else (WORDS if spec["type"] == "text" else True)
        view = client.post(f"/api/session/{sid}/submit", json={"step": spec["step"], "value": value}).json()
        resets.append((view["block_index"], view["reset_transcript"]))
        if view["block_index"] == 2 and spec["step"] == "gate":
            break

    # Exactly two resets so far: entering block 1 and entering block 2. Every
    # other step within a block leaves the transcript alone.
    reset_blocks = [idx for idx, was_reset in resets if was_reset]
    assert reset_blocks == [1, 2], resets


def test_client_cannot_spoof_marker_identity(client):
    """Client event detail must not override session identity, or crash the
    endpoint by colliding with a reserved field name."""
    from app.store import get_store

    view = client.post("/api/session", json={"participant_id": "TEST002"}).json()
    sid = view["session_id"]

    r = client.post(
        f"/api/session/{sid}/event",
        json={"event": "messages_rendered",
              "detail": {"participant_id": "SPOOFED", "session_id": "SPOOFED",
                         "block_index": 99, "geometry": [{"id": "m1", "x": 1, "y": 2}]}},
    )
    assert r.status_code == 200, r.text

    rows = [dict(x) for x in get_store()._conn.execute(
        "SELECT * FROM events WHERE event = 'messages_rendered' AND session_id = ?", (sid,))]
    assert rows, "marker was not recorded"
    row = rows[-1]
    assert row["participant_id"] == "TEST002"
    assert row["session_id"] == sid
    assert row["block_index"] == 0          # the real block, not the claimed 99
    assert "geometry" in row["payload"]     # legitimate detail is still kept


def test_unknown_client_event_rejected(client):
    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    r = client.post(f"/api/session/{sid}/event", json={"event": "arbitrary_event", "detail": {}})
    assert r.status_code == 400


def test_session_blocked_without_pinned_font(client, monkeypatch):
    """Section 12: a session must not run on a system-font fallback, because the
    layout metrics the eye-tracking geometry depends on would be invalid."""
    from app import config, main

    monkeypatch.setattr(config, "ALLOW_SYSTEM_FONT", False)
    monkeypatch.setattr(main, "font_installed", lambda: False)

    # The Start Screen lookup still resolves -- the block is on starting a run.
    assert client.post("/api/participant/lookup", json={"participant_id": "TEST001"}).status_code == 200

    r = client.post("/api/session", json={"participant_id": "TEST001"})
    assert r.status_code == 503
    assert "font" in r.json()["detail"].lower()

    # With the font present, the same request succeeds.
    monkeypatch.setattr(main, "font_installed", lambda: True)
    assert client.post("/api/session", json={"participant_id": "TEST001"}).status_code == 200


def test_config_exposes_presentation_constants(client):
    cfg = client.get("/api/config").json()
    assert cfg["fixation_cross_ms"] == 500
    assert cfg["ui_font_family"]
    assert cfg["ui_font_file"].endswith(".woff2")
    assert "ui_font_installed" in cfg
    assert "allow_system_font" in cfg


def test_presentation_integrity_events_accepted(client):
    """The Section 12 client markers are all on the allowlist and record cleanly."""
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]

    events = {
        "fixation_cross_shown": {"geometry": {"cross": {"x": 942, "y": 522, "w": 36, "h": 36}},
                                 "scroll_top": 0, "hold_ms": 500},
        "fixation_cross_hidden": {"actual_hold_ms": 503},
        "window_focus_lost": {"reason": "blur"},
        "window_focus_regained": {"reason": "focus", "unfocused_ms": 2400},
        "visibility_changed": {"visibility": "hidden"},
        "presentation_mode_checked": {"fullscreen": True, "screen_w": 2560, "screen_h": 1440,
                                     "scale_mismatch": False, "px_per_degree": 44.85},
    }
    for name, detail in events.items():
        r = client.post(f"/api/session/{sid}/event", json={"event": name, "detail": detail})
        assert r.status_code == 200, f"{name}: {r.text}"

    recorded = {row["event"] for row in get_store()._conn.execute(
        "SELECT event FROM events WHERE session_id = ?", (sid,))}
    assert set(events) <= recorded


def test_session_marker_records_font_state(client):
    """Every run records which font it actually used, so a fallback run can never
    be silently analysed as if it had used the pinned face."""
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    row = get_store()._conn.execute(
        "SELECT payload FROM events WHERE session_id = ? AND event = 'session_started'", (sid,)
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert "ui_font" in payload and "font_installed" in payload


def test_marker_dir_removed_midsession_does_not_break_the_run(client, tmp_path):
    """A vanished marker directory must not take down a session in progress."""
    import shutil

    from app import markers
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    assert client.post(f"/api/session/{sid}/submit",
                       json={"step": "answer_1", "value": 40}).status_code == 200

    shutil.rmtree(markers.get_stream()._dir)

    # The run continues, and the event still reaches the database.
    r = client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 55})
    assert r.status_code == 200, r.text
    assert r.json()["input"]["step"] == "text_1"

    row = dict(get_store().block_rows(sid)[0])
    assert row["confidence_score_1"] == "55"

    # The directory is recreated, so the JSONL mirror resumes.
    assert markers.get_stream()._dir.exists()


def test_rig_geometry_math():
    """The visual-angle figures are derived, not written down twice.

    27" QHD at 600 mm: 0.2335 mm/px pitch gives ~44.9 px per degree. This test
    exists because the previous hand-written figure (36 px/deg) silently
    disagreed with the viewing distance it was documented against.
    """
    import math

    from app import config

    assert config.PIXEL_PITCH_MM == pytest.approx(0.2335, abs=0.0005)
    assert config.PX_PER_DEGREE == pytest.approx(44.85, abs=0.1)

    # Independent re-derivation from first principles.
    expected = (2 * config.VIEWING_DISTANCE_MM * math.tan(math.radians(0.5))) / config.PIXEL_PITCH_MM
    assert config.PX_PER_DEGREE == pytest.approx(expected)

    # The fixation cross is specified in degrees, so it tracks the rig.
    assert config.FIXATION_CROSS_DEG * config.PX_PER_DEGREE == pytest.approx(44.85, abs=0.1)


def test_rig_is_served_to_the_client(client):
    rig = client.get("/api/config").json()["rig"]
    assert rig["px_per_degree"] == pytest.approx(44.85, abs=0.1)
    assert (rig["screen_w_px"], rig["screen_h_px"]) == (2560, 1440)
    assert rig["viewing_distance_mm"] == 600.0


def test_session_records_rig_for_analysis(client):
    """AOI rectangles are in pixels, so the px-to-degree conversion must travel
    with the data rather than living only in a config file."""
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    row = get_store()._conn.execute(
        "SELECT payload FROM events WHERE session_id = ? AND event = 'session_started'", (sid,)
    ).fetchone()
    payload = json.loads(row["payload"])
    for key in ("px_per_degree", "viewing_distance_mm", "pixel_pitch_mm",
                "screen_w_px", "screen_h_px", "screen_diagonal_in"):
        assert key in payload, f"{key} missing from session_started"
    assert payload["px_per_degree"] == pytest.approx(44.85, abs=0.1)


def _to_block(client, sid, view, block_index):
    """Advance through whole blocks until `block_index` has just opened."""
    while view["block_index"] != block_index:
        view = _run_block(client, sid, view)
    return view


@pytest.mark.parametrize("block_index, ends_with_question", [(1, True), (2, False)])
def test_llm_reply_follows_the_blocks_question_flag(client, block_index, ends_with_question):
    """The reply is the only message shown before User Text Response 2 and 3,
    and whether it ends on a question is the block's question_N flag.

    TEST001 block 1 is a question block and block 2 a statement block.
    """
    from app.store import get_store

    view = client.post("/api/session", json={"participant_id": "TEST001"}).json()
    sid = view["session_id"]
    view = _to_block(client, sid, view, block_index)
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 40})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 60})

    for step, next_step in (("text_1", "text_2"), ("text_2", "text_3")):
        view = client.post(f"/api/session/{sid}/submit", json={"step": step, "value": WORDS}).json()
        assert view["awaiting_llm"]

        view = client.post(f"/api/session/{sid}/llm").json()
        kinds = [m["kind"] for m in view["messages"]]
        assert kinds == ["llm"], f"expected the reply alone, got {kinds}"
        text = view["messages"][0]["text"]
        if ends_with_question:
            assert text.rstrip().endswith("?")
        else:
            assert "?" not in text

        # The box says what it wants either way.
        assert view["input"]["step"] == next_step
        assert view["input"]["placeholder"]

    row = [dict(r) for r in get_store().block_rows(sid) if r["block_index"] == str(block_index)][0]
    assert row["ends_with_question"] == ("1" if ends_with_question else "0")
    assert row["llm_text_1_elicited"] == row["ends_with_question"]


def test_elicitation_is_recorded_per_turn(client):
    """Whether each reply actually closed on a question is auditable after the
    fact, rather than assumed from the prompt."""
    from app.store import get_store

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 12})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 34})
    client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": WORDS})
    client.post(f"/api/session/{sid}/llm")

    row = dict(get_store().block_rows(sid)[0])
    assert row["llm_text_1_elicited"] == "1"


def test_reply_word_cap_matches_config(client):
    """Replies now carry the follow-up too, so the cap is the only thing keeping
    the on-screen stimulus a bounded size."""
    sid = client.post("/api/session", json={"participant_id": "TEST001"}).json()["session_id"]
    client.post(f"/api/session/{sid}/submit", json={"step": "answer_1", "value": 50})
    client.post(f"/api/session/{sid}/submit", json={"step": "confidence_1", "value": 50})
    client.post(f"/api/session/{sid}/submit", json={"step": "text_1", "value": WORDS})
    view = client.post(f"/api/session/{sid}/llm").json()

    assert len(view["messages"][0]["text"].split()) <= config.MAX_LLM_WORDS


def test_vignette_card_shows_the_appraisal_on_the_agreement_scale(client):
    from app import prompts

    view = client.post("/api/session", json={"participant_id": "TEST001"}).json()
    card = [m for m in view["messages"] if m["kind"] == "vignette"][0]
    assert "consistent morning routine" in card["text"]
    assert card["question"] == "\u201cA consistent morning routine makes people more productive for the rest of the day.\u201d"
    assert view["messages"][-1]["text"] == prompts.ANSWER_1
    assert (card["scale_low"], card["scale_high"]) == (prompts.APPRAISAL_SCALE_LOW, prompts.APPRAISAL_SCALE_HIGH)
    assert (view["input"]["scale_low"], view["input"]["scale_high"]) == ("Do not agree at all", "Completely agree")


def test_assignment_rejects_a_vignette_from_the_wrong_category(tmp_path):
    import shutil

    from app.content import ContentError, ContentLibrary

    for f in (config.FIXTURES_DIR).glob("*.csv"):
        shutil.copy(f, tmp_path / f.name)
    path = tmp_path / "assignment.csv"
    path.write_text(path.read_text().replace("TEST001,INF,INF-01", "TEST001,INF,DEC-09", 1))
    with pytest.raises(ContentError, match="DEC-09"):
        ContentLibrary(tmp_path).resolve("TEST001")


def test_fixture_question_flags_are_balanced_within_each_stance():
    """Half of each stance's blocks end on a question, half do not."""
    from collections import Counter

    from app.content import ContentLibrary

    lib = ContentLibrary()
    for pid in lib.participant_ids():
        counts = Counter((b.stance.label, b.ends_with_question) for b in lib.resolve(pid).blocks[1:])
        for stance in lib.stances:
            assert counts[(stance, True)] == counts[(stance, False)] == 2, (pid, counts)


class _ScriptedProvider:
    """Returns the given drafts in order, and records what it was sent."""

    name = "scripted"

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append(messages)
        return self.drafts.pop(0), "end_turn", {"input_tokens": 10, "output_tokens": 5}


def _respond(drafts, ends_with_question=True):
    from app.content import ContentLibrary
    from app.llm import LLMClient, Turn

    client = LLMClient("fake")
    provider = _ScriptedProvider(drafts)
    client._provider_for = lambda block: provider
    result = client.respond(ContentLibrary().practice, 50, 50, [Turn("user", "Because.")],
                            ends_with_question=ends_with_question)
    return result, provider


def test_over_long_reply_is_rewritten_by_the_model_not_cut():
    from app.llm import PROBLEM_TOO_LONG

    long = " ".join(["word"] * (config.MAX_LLM_WORDS + 10)) + " right?"
    short = "A shorter reply that keeps the point. Does that fit?"
    result, provider = _respond([long, short])

    assert result.text == short
    assert (result.rewrites, result.over_cap) == (1, False)
    assert result.drafts == [long, short]
    assert result.usage["input_tokens"] == 20  # both rounds counted
    # The rewrite request carries the draft back and names the problem.
    retry = provider.calls[1]
    assert retry[-2] == {"role": "assistant", "content": long}
    assert PROBLEM_TOO_LONG.format(n=config.MAX_LLM_WORDS + 11, max_words=config.MAX_LLM_WORDS) in retry[-1]["content"]


def test_question_rule_is_enforced_both_ways():
    from app.llm import PROBLEM_HAS_QUESTION, PROBLEM_NO_QUESTION

    result, provider = _respond(["No question here.", "Now there is one?"], ends_with_question=True)
    assert result.text == "Now there is one?" and result.rewrites == 1
    assert PROBLEM_NO_QUESTION in provider.calls[1][-1]["content"]

    result, provider = _respond(["Is this a question? It ends flat.", "It ends flat."], ends_with_question=False)
    assert result.text == "It ends flat." and result.rewrites == 1
    assert PROBLEM_HAS_QUESTION in provider.calls[1][-1]["content"]


def test_still_non_compliant_after_max_rewrites_is_delivered_and_flagged():
    drafts = [" ".join(["word"] * (config.MAX_LLM_WORDS + n)) + "?" for n in (30, 10, 20)]
    result, provider = _respond(drafts)

    assert len(provider.calls) == config.MAX_REWRITES + 1
    assert result.over_cap is True
    assert result.text == drafts[1]  # the shortest attempt, never cut by code
    assert result.raw_text == drafts[0]


def test_thinking_hold_is_a_fixed_seven_seconds(client):
    cfg = client.get("/api/config").json()
    assert cfg["llm_thinking_hold_ms"] == 7000
    assert "thinking_slow_after_ms" not in cfg

    sid = client.post("/api/session", json={"participant_id": "TEST002"}).json()["session_id"]
    r = client.post(f"/api/session/{sid}/event", json={
        "event": "thinking_hidden",
        "detail": {"hold_ms": 7000, "actual_hold_ms": 7003, "reply_after_ms": 2400, "overran": False},
    })
    assert r.status_code == 200
    r = client.post(f"/api/session/{sid}/event", json={"event": "thinking_slow_state", "detail": {}})
    assert r.status_code == 400
