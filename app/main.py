"""FastAPI application: static frontend plus the session API.

The browser never receives the assignment plan, the Stance, or anything about
future blocks -- it asks the server what to render next and posts back what the
participant did. Every state change emits an LSL marker and is persisted before
the response is returned.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, prompts
from .content import ContentError, ContentLibrary
from .llm import LLMError, get_client
from .markers import get_stream, lsl_available
from .session import FlowError, Session, ValidationError, now_iso
from .store import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mobi")

APP_VERSION = "0.1.0"
STATIC_DIR = Path(__file__).parent / "static"

library: ContentLibrary | None = None
sessions: dict[str, Session] = {}

# Client-reported UI events that are allowed to become markers. Anything else is
# rejected so the browser cannot write arbitrary entries into the record.
CLIENT_EVENTS = frozenset({
    "messages_rendered",
    "transcript_scrolled",
    "fixation_cross_shown",
    "fixation_cross_hidden",
    "window_focus_lost",
    "window_focus_regained",
    "visibility_changed",
    "presentation_mode_checked",
    "thinking_shown",
    "thinking_slow_state",
    "input_focused",
    "validation_blocked",
    "start_screen_shown",
    "end_screen_shown",
})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    yield


app = FastAPI(title="MoBI-MSM Participant Task", version=APP_VERSION, lifespan=lifespan)


def _startup() -> None:
    global library
    library = ContentLibrary()
    get_store()
    # Built here rather than on first use: LiteLLM is slow to import, and that
    # cost would otherwise land inside the first participant's measured wait.
    # It also surfaces a bad provider or missing credentials now, not mid-run.
    client = get_client()
    stream = get_stream()
    stream.push(
        "app_started",
        app_version=APP_VERSION,
        llm_provider=client.provider_name,
        llm_model=client.model,
        lsl=lsl_available(),
        participants=len(library.participant_ids()),
    )
    log.info(
        "Loaded %d participants, %d scenarios. LSL outlet: %s. LLM provider: %s (%s)",
        len(library.participant_ids()), len(library.scenarios),
        "yes" if lsl_available() else "NO - JSONL only",
        client.provider_name, client.model,
    )
    log.info(
        "Display rig: %.1f\" %dx%d at %.0f mm -> %.2f px/deg (pitch %.4f mm/px)",
        config.SCREEN_DIAGONAL_IN, config.SCREEN_W_PX, config.SCREEN_H_PX,
        config.VIEWING_DISTANCE_MM, config.PX_PER_DEGREE, config.PIXEL_PITCH_MM,
    )
    if not font_installed():
        where = STATIC_DIR / config.UI_FONT_FILE
        if config.ALLOW_SYSTEM_FONT:
            log.warning(
                "Pinned font %r is MISSING (%s) and MOBI_ALLOW_SYSTEM_FONT is set. "
                "The interface will run on a system-font fallback: layout metrics, and "
                "therefore the eye-tracking geometry, are NOT valid for data collection.",
                config.UI_FONT_FAMILY, where,
            )
        else:
            log.error(
                "Pinned font %r is MISSING (%s). Sessions are blocked until it is installed "
                "-- see 'Font determinism' in the README.", config.UI_FONT_FAMILY, where,
            )


def font_installed() -> bool:
    """Whether the pinned webfont (Section 12) is present in static assets."""
    return (STATIC_DIR / config.UI_FONT_FILE).is_file()


def _library() -> ContentLibrary:
    if library is None:
        raise HTTPException(503, "Content library not loaded")
    return library


def _session(session_id: str) -> Session:
    session = sessions.get(session_id)
    if session is None:
        # No resume after a refresh or crash, by design (Section 11).
        raise HTTPException(404, "Session not found. This session cannot be resumed; start a new one.")
    return session


# Identity fields the marker record owns. A caller may pin block_index (e.g. a
# submission that closes the block it belongs to), but nothing may invent a
# different session or participant.
MARKER_IDENTITY_KEYS = ("session_id", "participant_id", "block_index")


def _mark(session: Session | None, event: str, **payload: Any) -> None:
    block_index = payload.pop("block_index", None)
    if block_index is None and session is not None:
        block_index = session.block.block_index
    payload.pop("session_id", None)
    payload.pop("participant_id", None)
    record = get_stream().push(
        event,
        session_id=session.session_id if session else None,
        participant_id=session.participant_id if session else None,
        block_index=block_index,
        **payload,
    )
    get_store().record_event(record)


# --- request models ---

class ParticipantRequest(BaseModel):
    participant_id: str = Field(min_length=1, max_length=64)


class SubmitRequest(BaseModel):
    step: str
    value: Any = None


class EventRequest(BaseModel):
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)


# --- routes ---

@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Constants the browser needs to mirror the server's input rules."""
    return {
        "app_version": APP_VERSION,
        "min_response_words": config.MIN_RESPONSE_WORDS,
        "score_min": config.SCORE_MIN,
        "score_max": config.SCORE_MAX,
        "thinking_slow_after_ms": config.THINKING_SLOW_AFTER_MS,
        "fixation_cross_ms": config.FIXATION_CROSS_MS,
        "fixation_cross_tolerance_ms": config.FIXATION_CROSS_TOLERANCE_MS,
        # Display rig: the client resolves every size from px_per_degree, and
        # checks the reported viewport against the expected screen dimensions.
        "rig": {
            "px_per_degree": round(config.PX_PER_DEGREE, 3),
            "screen_w_px": config.SCREEN_W_PX,
            "screen_h_px": config.SCREEN_H_PX,
            "screen_diagonal_in": config.SCREEN_DIAGONAL_IN,
            "viewing_distance_mm": config.VIEWING_DISTANCE_MM,
            "pixel_pitch_mm": round(config.PIXEL_PITCH_MM, 4),
        },
        "total_blocks": config.REAL_BLOCK_COUNT,
        "lsl_available": lsl_available(),
        "llm_provider": get_client().provider_name,
        "ui_font_family": config.UI_FONT_FAMILY,
        "ui_font_file": config.UI_FONT_FILE,
        "ui_font_installed": font_installed(),
        "allow_system_font": config.ALLOW_SYSTEM_FONT,
        "copy": {
            "start_title": prompts.START_SCREEN_TITLE,
            "start_body": prompts.START_SCREEN_BODY,
            "end_title": prompts.END_SCREEN_TITLE,
            "end_body": prompts.END_SCREEN_BODY,
        },
    }


@app.post("/api/participant/lookup")
def lookup_participant(req: ParticipantRequest) -> JSONResponse:
    """Start Screen validation (Section 4): unknown IDs and already-run IDs both
    block progress with an inline message rather than proceeding."""
    pid = req.participant_id.strip()
    lib = _library()
    store = get_store()

    if not lib.has_participant(pid):
        get_stream().push("participant_lookup_failed", participant_id=pid, reason="not_found")
        return JSONResponse(
            {"status": "not_found",
             "message": f"Participant ID “{pid}” was not found in the assignment file."},
            status_code=404,
        )

    if store.has_completed_session(pid):
        get_stream().push("participant_lookup_failed", participant_id=pid, reason="already_run")
        return JSONResponse(
            {"status": "already_run",
             "message": f"Participant “{pid}” has already completed a session. "
                        "Their data will not be overwritten. Enter a different Participant ID."},
            status_code=409,
        )

    try:
        lib.resolve(pid)   # surfaces a broken assignment row now, not mid-session
    except ContentError as exc:
        return JSONResponse({"status": "invalid_assignment", "message": str(exc)}, status_code=422)

    return JSONResponse({"status": "ok", "participant_id": pid})


@app.post("/api/session")
def start_session(req: ParticipantRequest) -> dict[str, Any]:
    """Create the session and open the Practice Block."""
    pid = req.participant_id.strip()
    lib = _library()
    store = get_store()

    if not lib.has_participant(pid):
        raise HTTPException(404, "Participant ID not found in the assignment file.")
    if store.has_completed_session(pid):
        raise HTTPException(409, "Participant has already completed a session.")

    if not font_installed() and not config.ALLOW_SYSTEM_FONT:
        raise HTTPException(
            503,
            f"The interface font ({config.UI_FONT_FAMILY}) is not installed, so the layout "
            "metrics the eye-tracking geometry depends on would be invalid. "
            "Install it before running a session.",
        )

    try:
        plan = lib.resolve(pid)
    except ContentError as exc:
        raise HTTPException(422, str(exc)) from exc

    client = get_client()
    session = Session(plan, client)
    sessions[session.session_id] = session

    store.create_session(
        session.session_id, pid, session.started_at,
        app_version=APP_VERSION, llm_model=client.model, llm_provider=client.provider_name,
    )
    _mark(session, "session_started", block_count=len(plan.blocks),
          ui_font=config.UI_FONT_FAMILY if font_installed() else "SYSTEM-FALLBACK",
          font_installed=font_installed(),
          # Recorded so AOI rectangles convert to degrees of visual angle
          # without needing any knowledge outside the data.
          px_per_degree=round(config.PX_PER_DEGREE, 3),
          viewing_distance_mm=config.VIEWING_DISTANCE_MM,
          pixel_pitch_mm=round(config.PIXEL_PITCH_MM, 4),
          screen_w_px=config.SCREEN_W_PX, screen_h_px=config.SCREEN_H_PX,
          screen_diagonal_in=config.SCREEN_DIAGONAL_IN)

    view = session.open_block()
    store.init_block(session.block_meta())
    store.update_block(session.session_id, session.block.block_index, {"scenario_shown_at": now_iso()})
    _mark(session, "block_started",
          is_practice=session.block.is_practice,
          category=session.block.category_label,
          scenario=session.block.scenario.label,
          stance=session.block.stance.label)

    return {"session_id": session.session_id, **view.to_dict()}


@app.post("/api/session/{session_id}/submit")
def submit(session_id: str, req: SubmitRequest) -> dict[str, Any]:
    session = _session(session_id)
    store = get_store()
    block_index_before = session.block.block_index

    try:
        value, updates, view = session.submit(req.step, req.value)
    except ValidationError as exc:
        _mark(session, "submission_rejected", step=req.step, reason=str(exc))
        raise HTTPException(422, str(exc)) from exc
    except FlowError as exc:
        raise HTTPException(409, str(exc)) from exc

    if updates:
        store.update_block(session_id, block_index_before, updates)

    _mark(session, "submission_recorded", step=req.step, block_index=block_index_before,
          **{k: v for k, v in updates.items() if not k.endswith("_at")})

    # A gate advanced us into a new block: persist its metadata and opening timestamp.
    if req.step == "gate" and not view.done:
        store.init_block(session.block_meta())
        store.update_block(session_id, session.block.block_index, {"scenario_shown_at": now_iso()})
        _mark(session, "block_started",
              is_practice=session.block.is_practice,
              category=session.block.category_label,
              scenario=session.block.scenario.label,
              stance=session.block.stance.label)

    if view.done:
        _finish(session)

    return view.to_dict()


@app.post("/api/session/{session_id}/llm")
def llm_turn(session_id: str) -> dict[str, Any]:
    session = _session(session_id)
    store = get_store()
    block_index = session.block.block_index

    _mark(session, "llm_request_sent", step=session.step)
    try:
        result, updates, view = session.run_llm_turn()
    except FlowError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LLMError as exc:
        _mark(session, "llm_request_failed", error=str(exc))
        raise HTTPException(502, f"The model did not return a response: {exc}") from exc
    except Exception as exc:  # provider/network failures -- Section 11 leaves policy to us
        log.exception("LLM turn failed")
        _mark(session, "llm_request_failed", error=repr(exc))
        raise HTTPException(502, "The model did not return a response.") from exc

    store.update_block(session_id, block_index, updates)
    _mark(session, "llm_response_received",
          latency_ms=result.latency_ms, truncated=result.truncated,
          model=result.model, provider=result.provider,
          requested_at=result.requested_at, received_at=result.received_at,
          word_count=len(result.text.split()))
    return view.to_dict()


@app.post("/api/session/{session_id}/event")
def client_event(session_id: str, req: EventRequest) -> dict[str, str]:
    """Markers for UI state the browser owns (render times, thinking-indicator
    transitions). Restricted to a known set of event names."""
    session = _session(session_id)
    if req.event not in CLIENT_EVENTS:
        raise HTTPException(400, f"Unknown client event: {req.event}")
    # Identity comes from the session the request is authenticated against, never
    # from the browser -- a client must not be able to attribute a marker to a
    # different participant, or crash the endpoint by colliding with a field name.
    detail = {k: v for k, v in req.detail.items() if k not in MARKER_IDENTITY_KEYS}
    _mark(session, req.event, source="client", **detail)
    return {"status": "ok"}


def _finish(session: Session) -> None:
    store = get_store()
    completed = session.completed_at or now_iso()
    store.complete_session(session.session_id, completed)
    _mark(session, "session_completed")
    try:
        store.export_blocks_csv(config.DATA_DIR / "exports" / f"{session.participant_id}_blocks.csv",
                                session_ids=[session.session_id])
        store.export_session_json(config.DATA_DIR / "exports" / f"{session.participant_id}_session.json",
                                  session.session_id)
    except Exception:
        log.exception("Export failed (data is still in the database)")


# --- static frontend ---

# The lab machine must never run a stale build. A cached app.js that disagrees
# with the server's flow is the kind of failure that only shows up mid-session,
# so nothing here is cacheable.
NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


class UncachedStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_STORE)
        return response

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False


def _asset_token() -> str:
    """A token that changes whenever a static asset changes, so the browser can
    never hold a stale app.js against a newer server."""
    stamp = max(
        (p.stat().st_mtime_ns for p in STATIC_DIR.iterdir() if p.is_file()),
        default=0,
    )
    return f"{APP_VERSION}-{stamp:x}"


@app.get("/")
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    token = _asset_token()
    html = html.replace("/static/app.js", f"/static/app.js?v={token}")
    html = html.replace("/static/styles.css", f"/static/styles.css?v={token}")
    return HTMLResponse(html, headers=NO_STORE)


app.mount("/static", UncachedStaticFiles(directory=STATIC_DIR), name="static")
