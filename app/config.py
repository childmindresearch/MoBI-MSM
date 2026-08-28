"""Runtime configuration and protocol constants.

Values that the spec calls out as tunable live here so the research team can
change them in one place without touching flow logic.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support so the lab machine needs no shell setup.

    Real environment variables always win, so an operator can override any
    value for a single run without editing the file.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(PROJECT_ROOT / ".env")


def _path_env(name: str, default: str) -> Path:
    raw = os.environ.get(name, default)
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


# --- Protocol constants (Sections 5-8 of the UX spec) ---

REAL_BLOCK_COUNT = 12
"""Blocks 1-12, one per Category x Stance combination."""

MIN_RESPONSE_WORDS = 15
"""Minimum whitespace-delimited tokens for User Text Response 1-3 (Section 6)."""

MAX_LLM_WORDS = 50
"""Hard cap on each LLM Text Response (Section 7)."""

SCORE_MIN = 0
SCORE_MAX = 100
"""Numeric score fields are whole numbers in [0, 100] (Section 6)."""

THINKING_SLOW_AFTER_MS = 7000
"""Tunable: when the thinking indicator switches to its 'still thinking' text.
Section 7 describes this as a soft threshold, not a cutoff."""

FIXATION_CROSS_MS = 500
"""How long the inter-block fixation cross is held before a block's Scenario
renders (Section 12). Shown once per block including the practice block -- 13
times per session -- as a recurring drift-correction reference for gaze."""

FIXATION_CROSS_TOLERANCE_MS = 50
"""How far the realised fixation-cross hold may deviate from nominal before the
record flags it. The hold is a timing reference, so a block whose cross was held
materially too long should be identifiable at analysis time rather than assumed
correct. Deviation happens when the browser throttles timers -- which is what a
backgrounded or non-fullscreen window does."""

# --- Display rig geometry -------------------------------------------------
#
# THE SINGLE SOURCE OF TRUTH FOR ALL SIZING.
#
# Every size in the interface is specified in degrees of visual angle and
# resolved to pixels from these four numbers, so moving to a different monitor
# or seating distance is a change here and nowhere else. Previously the pixel
# figure was duplicated between this file and the stylesheet, and the two
# drifted apart -- hence this being derived rather than written down twice.


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


SCREEN_DIAGONAL_IN = _f("MOBI_SCREEN_DIAGONAL_IN", 27.0)
SCREEN_W_PX = int(_f("MOBI_SCREEN_W_PX", 2560))
SCREEN_H_PX = int(_f("MOBI_SCREEN_H_PX", 1440))
VIEWING_DISTANCE_MM = _f("MOBI_VIEWING_DISTANCE_MM", 600.0)
"""Participant eye-to-screen distance. Measured on the rig, not assumed."""

SCREEN_W_MM = SCREEN_DIAGONAL_IN * 25.4 * SCREEN_W_PX / math.hypot(SCREEN_W_PX, SCREEN_H_PX)
SCREEN_H_MM = SCREEN_DIAGONAL_IN * 25.4 * SCREEN_H_PX / math.hypot(SCREEN_W_PX, SCREEN_H_PX)
PIXEL_PITCH_MM = SCREEN_W_MM / SCREEN_W_PX

PX_PER_DEGREE = (2 * VIEWING_DISTANCE_MM * math.tan(math.radians(0.5))) / PIXEL_PITCH_MM
"""Pixels subtending one degree of visual angle on this rig.

27" QHD at 600 mm gives ~44.9 px/deg. Note this is *CSS* pixels: if the OS or
browser applies display scaling, the browser's reported viewport will not match
SCREEN_W_PX x SCREEN_H_PX and this figure is wrong. The client checks for that
at session start and warns (Section 12)."""

FIXATION_CROSS_DEG = 1.0
"""Drawn extent of the fixation cross, in degrees of visual angle (Section 12)."""

UI_FONT_FAMILY = "MoBI UI"
"""The single embedded webfont the interface pins its metrics to (Section 12).
Every size in the stylesheet is computed against this font's character metrics,
so a system-font fallback would silently invalidate the visual-angle math."""

UI_FONT_FILE = "fonts/ui.woff2"
"""Path under app/static/ of the embedded font. Served locally; never a CDN."""

ALLOW_SYSTEM_FONT = os.environ.get("MOBI_ALLOW_SYSTEM_FONT", "").strip().lower() in {"1", "true", "yes"}
"""Development-only escape hatch. Section 12 forbids running on a system-font
fallback, because the stylesheet's visual-angle math is computed against the
embedded font's metrics. With no font installed the app refuses to start a
session; setting this lets it run anyway for development, logs a loud warning,
and records the substitution in the session record so no run can be
mis-analysed as if it had used the pinned font."""

PRACTICE_BLOCK_INDEX = 0
"""Practice block is block 0; real blocks are 1-12."""


# --- Environment-driven configuration ---

FIXTURES_DIR = _path_env("MOBI_FIXTURES_DIR", "test-fixtures")
DATA_DIR = _path_env("MOBI_DATA_DIR", "data")

DB_PATH = DATA_DIR / "sessions.db"
MARKER_LOG_DIR = DATA_DIR / "markers"

LLM_PROVIDER = os.environ.get("MOBI_LLM_PROVIDER", "anthropic").strip().lower()
LLM_MODEL = os.environ.get("MOBI_LLM_MODEL", "claude-opus-5").strip()
LLM_EFFORT = os.environ.get("MOBI_LLM_EFFORT", "low").strip().lower()
LLM_MAX_TOKENS = int(os.environ.get("MOBI_LLM_MAX_TOKENS", "4000"))
LLM_TIMEOUT_S = float(os.environ.get("MOBI_LLM_TIMEOUT_S", "60"))
FAKE_LLM_DELAY_S = float(os.environ.get("MOBI_FAKE_LLM_DELAY_S", "0"))
"""Artificial delay for the fake provider, so the thinking indicator and its
slow state can be rehearsed without calling the real API."""

LSL_STREAM_NAME = os.environ.get("MOBI_LSL_STREAM_NAME", "MoBI-MSM-Markers")
LSL_STREAM_TYPE = os.environ.get("MOBI_LSL_STREAM_TYPE", "Markers")
LSL_SOURCE_ID = os.environ.get("MOBI_LSL_SOURCE_ID", "mobi-msm-participant-ui")
