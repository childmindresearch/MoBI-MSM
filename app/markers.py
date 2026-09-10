"""LSL marker emission (Section 10).

Every discrete on-screen event pushes a marker so it can be synchronized with
the EEG, eye tracking, ECG/EMG/EDA/respiration, motion capture, video, screen
recording, and keystroke streams collected alongside this UI.

The outlet is optional at import time: when pylsl/liblsl are available the
markers go to a real LSL StreamOutlet, and they are *always* mirrored to a
per-session JSONL file so a run is never lost to a missing LSL install. The
JSONL is the local record; LSL is the synchronization channel.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on whether liblsl is installed on the host
    from pylsl import StreamInfo, StreamOutlet, local_clock

    _HAVE_LSL = True
except Exception:  # ImportError, or liblsl shared library missing
    StreamInfo = StreamOutlet = None  # type: ignore[assignment]
    local_clock = None  # type: ignore[assignment]
    _HAVE_LSL = False


def lsl_available() -> bool:
    return _HAVE_LSL


class MarkerStream:
    """One marker channel for the whole application.

    Markers are pushed as single-channel strings holding a JSON object, which
    keeps the stream self-describing in the recording without needing a
    side-channel schema.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._dir = Path(log_dir or config.MARKER_LOG_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._outlet = self._make_outlet()

    def _make_outlet(self):
        if not _HAVE_LSL:
            log.warning(
                "pylsl not available - markers will be written to JSONL only. "
                "Install the 'lsl' extra before collecting real data."
            )
            return None
        try:
            info = StreamInfo(
                name=config.LSL_STREAM_NAME,
                type=config.LSL_STREAM_TYPE,
                channel_count=1,
                nominal_srate=0,  # irregular
                channel_format="string",
                source_id=config.LSL_SOURCE_ID,
            )
            desc = info.desc()
            desc.append_child_value("payload_format", "json")
            desc.append_child_value("app", "mobi-msm-participant-ui")
            outlet = StreamOutlet(info)
            log.info("LSL marker outlet open: %s", config.LSL_STREAM_NAME)
            return outlet
        except Exception:  # pragma: no cover - hardware/driver dependent
            log.exception("Failed to open LSL outlet - falling back to JSONL only")
            return None

    def push(
        self,
        event: str,
        *,
        session_id: str | None = None,
        participant_id: str | None = None,
        block_index: int | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Emit one marker. Returns the record, including its timestamps."""
        wall = time.time()
        record: dict[str, Any] = {
            "event": event,
            "t_unix": wall,
            "t_iso": _iso(wall),
            "t_lsl": local_clock() if _HAVE_LSL else None,
            "session_id": session_id,
            "participant_id": participant_id,
            "block_index": block_index,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self._outlet is not None:
                try:
                    # Stamped with the same t_lsl carried in the JSONL rather than
                    # letting push_sample stamp "now". The two are captured a few
                    # microseconds apart otherwise, which makes aligning the JSONL
                    # mirror against the XDF approximate; pinning it makes the
                    # recorded timestamp and the mirrored one the same number.
                    self._outlet.push_sample([line], timestamp=record["t_lsl"])
                except Exception:  # pragma: no cover
                    log.exception("LSL push failed for event %s", event)
            self._write_line(session_id, line, event)
        return record

    def _write_line(self, session_id: str | None, line: str, event: str) -> None:
        """Append one marker to the session's JSONL.

        A marker never takes down a session. If the log directory has gone away
        mid-run (removed, unmounted, permissions changed) it is recreated and the
        write retried once; if it still fails the failure is logged loudly and
        the participant's run continues, with the event still reaching SQLite via
        the caller. Losing one marker is bad; losing the whole session to it is
        worse.
        """
        for attempt in (1, 2):
            try:
                with self._path_for(session_id).open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return
            except OSError:
                if attempt == 1:
                    try:
                        self._dir.mkdir(parents=True, exist_ok=True)
                        continue
                    except OSError:
                        pass
                log.exception(
                    "Could not write marker %r to %s - the run continues and the event is "
                    "still recorded in the database, but the JSONL mirror is incomplete.",
                    event, self._dir,
                )
                return

    def _path_for(self, session_id: str | None) -> Path:
        return self._dir / f"{session_id or 'no-session'}.jsonl"


def _iso(wall: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat(timespec="milliseconds")


_stream: MarkerStream | None = None


def get_stream() -> MarkerStream:
    global _stream
    if _stream is None:
        _stream = MarkerStream()
    return _stream
