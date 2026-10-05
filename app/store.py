"""SQLite persistence and CSV/JSON export (Section 10).

Three tables:
  sessions  - one row per run of a participant; also the "already run" check.
  blocks    - one wide row per block, holding every value and timestamp the
              Section 5 table calls for. Wide because the schema is fixed and
              a flat row exports directly to analysis-ready CSV.
  events    - the complete marker log, mirrored from the LSL stream, so timing
              survives independently of the per-block summary.

Rows are written as the session progresses, not at the end, so a run that is
interrupted still leaves everything collected up to that point on disk.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import config

log = logging.getLogger(__name__)

# Per-block value columns, in the order the participant produces them.
BLOCK_VALUE_COLUMNS: tuple[str, ...] = (
    "vignette_shown_at",
    "answer_score_1", "answer_score_1_at",
    "confidence_score_1", "confidence_score_1_at",
    "user_text_1", "user_text_1_words", "user_text_1_at",
    "llm_text_1", "llm_text_1_raw", "llm_text_1_drafts",
    "llm_text_1_rewrites", "llm_text_1_over_cap", "llm_text_1_elicited",
    "llm_text_1_requested_at", "llm_text_1_received_at", "llm_text_1_latency_ms",
    "user_text_2", "user_text_2_words", "user_text_2_at",
    "llm_text_2", "llm_text_2_raw", "llm_text_2_drafts",
    "llm_text_2_rewrites", "llm_text_2_over_cap", "llm_text_2_elicited",
    "llm_text_2_requested_at", "llm_text_2_received_at", "llm_text_2_latency_ms",
    "user_text_3", "user_text_3_words", "user_text_3_at",
    "answer_score_2", "answer_score_2_at",
    "confidence_score_2", "confidence_score_2_at",
    "activation_score", "activation_score_at",
    "advanced_at",
)

BLOCK_META_COLUMNS: tuple[str, ...] = (
    "session_id", "participant_id", "block_index", "is_practice",
    "category_label", "category_name",
    "vignette_label", "vignette_text", "appraisal_text",
    "stance_label", "stance_name",
    "ends_with_question", "prompt_version",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL,
    status         TEXT NOT NULL,          -- in_progress | completed
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    app_version    TEXT,
    llm_model      TEXT,
    llm_provider   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_participant ON sessions(participant_id);

CREATE TABLE IF NOT EXISTS blocks (
    {", ".join(f"{c} TEXT" for c in BLOCK_META_COLUMNS)},
    {", ".join(f"{c} TEXT" for c in BLOCK_VALUE_COLUMNS)},
    PRIMARY KEY (session_id, block_index)
);

CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT,
    participant_id TEXT,
    block_index    INTEGER,
    event          TEXT NOT NULL,
    t_unix         REAL NOT NULL,
    t_iso          TEXT NOT NULL,
    t_lsl          REAL,
    payload        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, t_unix);
"""


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # a lab run is not worth losing to a crash
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._add_missing_block_columns()
            self._conn.commit()

    def _add_missing_block_columns(self) -> None:
        """Bring an existing database up to the current block schema.

        CREATE TABLE IF NOT EXISTS leaves an older `blocks` table alone, so a
        database written before a column was added would fail the first UPDATE
        that named it -- mid-session, on a participant. Columns here are only
        ever added, never dropped or reordered, so a plain ADD COLUMN is the
        whole migration and earlier rows simply hold NULL for the new ones.
        """
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(blocks)")}
        for column in (*BLOCK_META_COLUMNS, *BLOCK_VALUE_COLUMNS):
            if column not in have:
                self._conn.execute(f"ALTER TABLE blocks ADD COLUMN {column} TEXT")
                log.info("Added missing column blocks.%s", column)

    # --- sessions ---

    def has_completed_session(self, participant_id: str) -> bool:
        """Section 4: a participant who has already been run is never re-run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE participant_id = ? AND status = 'completed' LIMIT 1",
                (participant_id,),
            ).fetchone()
        return row is not None

    def existing_sessions(self, participant_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM sessions WHERE participant_id = ? ORDER BY started_at",
                    (participant_id,),
                )
            )

    def create_session(self, session_id: str, participant_id: str, started_at: str, **meta: Any) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions
                   (session_id, participant_id, status, started_at, app_version, llm_model, llm_provider)
                   VALUES (?, ?, 'in_progress', ?, ?, ?, ?)""",
                (
                    session_id, participant_id, started_at,
                    meta.get("app_version"), meta.get("llm_model"), meta.get("llm_provider"),
                ),
            )
            self._conn.commit()

    def complete_session(self, session_id: str, completed_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = 'completed', completed_at = ? WHERE session_id = ?",
                (completed_at, session_id),
            )
            self._conn.commit()

    # --- blocks ---

    def init_block(self, meta: dict[str, Any]) -> None:
        cols = [c for c in BLOCK_META_COLUMNS if c in meta]
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO blocks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [_text(meta[c]) for c in cols],
            )
            self._conn.commit()

    def update_block(self, session_id: str, block_index: int, values: dict[str, Any]) -> None:
        unknown = set(values) - set(BLOCK_VALUE_COLUMNS)
        if unknown:
            raise KeyError(f"Unknown block columns: {sorted(unknown)}")
        if not values:
            return
        assignments = ", ".join(f"{c} = ?" for c in values)
        with self._lock:
            self._conn.execute(
                f"UPDATE blocks SET {assignments} WHERE session_id = ? AND block_index = ?",
                [*(_text(v) for v in values.values()), session_id, str(block_index)],
            )
            self._conn.commit()

    def block_rows(self, session_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM blocks WHERE session_id = ? ORDER BY CAST(block_index AS INTEGER)",
                    (session_id,),
                )
            )

    # --- events ---

    def record_event(self, record: dict[str, Any]) -> None:
        known = {"session_id", "participant_id", "block_index", "event", "t_unix", "t_iso", "t_lsl"}
        payload = {k: v for k, v in record.items() if k not in known}
        with self._lock:
            self._conn.execute(
                """INSERT INTO events
                   (session_id, participant_id, block_index, event, t_unix, t_iso, t_lsl, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.get("session_id"), record.get("participant_id"), record.get("block_index"),
                    record.get("event"), record.get("t_unix"), record.get("t_iso"), record.get("t_lsl"),
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                ),
            )
            self._conn.commit()

    # --- export ---

    def export_blocks_csv(self, out_path: Path, session_ids: Iterable[str] | None = None) -> Path:
        """One row per block, all metadata and values flat -- the analysis format."""
        columns = [*BLOCK_META_COLUMNS, *BLOCK_VALUE_COLUMNS]
        with self._lock:
            if session_ids is None:
                rows = list(self._conn.execute("SELECT * FROM blocks"))
            else:
                ids = list(session_ids)
                rows = list(
                    self._conn.execute(
                        f"SELECT * FROM blocks WHERE session_id IN ({', '.join('?' * len(ids))})", ids
                    )
                )
        rows.sort(key=lambda r: (r["participant_id"] or "", int(r["block_index"] or 0)))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            for r in rows:
                w.writerow({c: r[c] for c in columns})
        return out_path

    def export_session_json(self, out_path: Path, session_id: str) -> Path:
        with self._lock:
            session = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            events = list(
                self._conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY t_unix", (session_id,)
                )
            )
        doc = {
            "session": dict(session) if session else None,
            "blocks": [dict(r) for r in self.block_rows(session_id)],
            "events": [dict(r) for r in events],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        return out_path

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _text(v: Any) -> Any:
    """Every block column is TEXT so exports round-trip without type surprises."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
