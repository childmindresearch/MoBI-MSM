"""Loading and resolution of the assignment file and the three key files.

Per Section 4 of the spec, block order and content come from a pre-generated
assignment CSV whose cells are lookup labels, resolved at runtime against a
category key, a vignette key (vignettes_appraisals.csv), and a stance key.

A vignette is a short imagined situation; its appraisal is a first-person
statement about it that the participant rates on the 0-100 agreement scale.
The scale is the same for every appraisal, so its anchor labels live in
prompts.py rather than in the key. Validation of those files is
explicitly the research team's job (Section 11) -- this module does the minimum
lookup-integrity checking needed to fail loudly rather than render blanks.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from . import config


class ContentError(RuntimeError):
    """Raised when the fixture files cannot be loaded or resolved."""


@dataclass(frozen=True)
class Vignette:
    label: str                # VID, e.g. DEC-01
    category_label: str       # the key's Intent column: DEC, INF, REF or SUP
    text: str
    appraisal_text: str


@dataclass(frozen=True)
class Stance:
    label: str
    name: str
    system_prompt: str


@dataclass(frozen=True)
class BlockPlan:
    """One resolved block: what to show, and which hidden Stance drives the LLM."""

    block_index: int          # 0 = practice, 1..12 = real blocks
    is_practice: bool
    category_label: str
    category_name: str
    vignette: Vignette
    stance: Stance
    ends_with_question: bool  # whether each LLM reply in this block must end on a question


@dataclass(frozen=True)
class ParticipantPlan:
    participant_id: str
    blocks: tuple[BlockPlan, ...]   # practice first, then blocks 1..12


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ContentError(f"Required content file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    if not rows:
        raise ContentError(f"Content file is empty: {path}")
    return rows


def _flag(raw: str, where: str) -> bool:
    """A 0/1 cell. Anything else is a broken assignment file, not a default."""
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ContentError(f"{where}: expected 1 or 0, got {raw!r}")


class ContentLibrary:
    """The four content files, loaded once at startup and resolved per participant."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.dir = Path(fixtures_dir or config.FIXTURES_DIR)
        self.categories = self._load_categories()
        self.vignettes = self._load_vignettes()
        self.stances = self._load_stances()
        self.practice = self._load_practice()
        self._assignments = self._load_assignments()

    # --- loaders ---

    def _load_categories(self) -> dict[str, str]:
        return {r["category_label"]: r["category_name"] for r in _read_csv(self.dir / "category_key.csv")}

    def _load_vignettes(self) -> dict[str, Vignette]:
        out: dict[str, Vignette] = {}
        for r in _read_csv(self.dir / "vignettes_appraisals.csv"):
            out[r["VID"]] = Vignette(
                label=r["VID"],
                category_label=r["Intent"],
                text=r["vignette_text"],
                appraisal_text=r["appraisal_text"],
            )
        return out

    def _load_stances(self) -> dict[str, Stance]:
        return {
            r["stance_label"]: Stance(
                label=r["stance_label"], name=r["stance_name"], system_prompt=r["system_prompt"]
            )
            for r in _read_csv(self.dir / "stance_key.csv")
        }

    def _load_practice(self) -> BlockPlan:
        """The practice record is fixed for every participant, so it is referenced
        directly rather than looked up per participant (Sections 4 and 8)."""
        path = self.dir / "practice_vignette.csv"
        row = _read_csv(path)[0]
        stance_label = row.get("stance_label") or "calibrated"
        stance = self.stances.get(stance_label)
        if stance is None:
            raise ContentError(f"Practice vignette references unknown stance {stance_label!r}")
        vignette = Vignette(
            label=row["VID"],
            category_label="practice",
            text=row["vignette_text"],
            appraisal_text=row["appraisal_text"],
        )
        return BlockPlan(
            block_index=config.PRACTICE_BLOCK_INDEX,
            is_practice=True,
            category_label="practice",
            category_name="Practice",
            vignette=vignette,
            stance=stance,
            ends_with_question=_flag(row.get("ends_with_question") or "1", f"{path.name} ends_with_question"),
        )

    def _load_assignments(self) -> dict[str, dict[str, str]]:
        rows = _read_csv(self.dir / "assignment.csv")
        return {r["participant_id"]: r for r in rows if r.get("participant_id")}

    # --- resolution ---

    def has_participant(self, participant_id: str) -> bool:
        return participant_id in self._assignments

    def participant_ids(self) -> list[str]:
        return sorted(self._assignments)

    def resolve(self, participant_id: str) -> ParticipantPlan:
        """Turn one assignment row into the practice block plus 12 resolved blocks."""
        row = self._assignments.get(participant_id)
        if row is None:
            raise ContentError(f"Participant ID not found in assignment file: {participant_id!r}")

        blocks: list[BlockPlan] = [self.practice]
        for n in range(1, config.REAL_BLOCK_COUNT + 1):
            category_label = row.get(f"category_{n}", "")
            vignette_label = row.get(f"vignette_{n}", "")
            stance_label = row.get(f"stance_{n}", "")
            question = row.get(f"question_{n}", "")
            if not (category_label and vignette_label and stance_label and question):
                raise ContentError(f"{participant_id}: assignment row is missing values for block {n}")

            category_name = self.categories.get(category_label)
            if category_name is None:
                raise ContentError(f"{participant_id} block {n}: unknown category label {category_label!r}")
            vignette = self.vignettes.get(vignette_label)
            if vignette is None:
                raise ContentError(f"{participant_id} block {n}: unknown vignette label {vignette_label!r}")
            if vignette.category_label != category_label:
                raise ContentError(
                    f"{participant_id} block {n}: vignette {vignette_label!r} is {vignette.category_label}, "
                    f"but the block is assigned category {category_label!r}"
                )
            stance = self.stances.get(stance_label)
            if stance is None:
                raise ContentError(f"{participant_id} block {n}: unknown stance label {stance_label!r}")

            blocks.append(
                BlockPlan(
                    block_index=n,
                    is_practice=False,
                    category_label=category_label,
                    category_name=category_name,
                    vignette=vignette,
                    stance=stance,
                    ends_with_question=_flag(question, f"{participant_id} block {n} question_{n}"),
                )
            )
        return ParticipantPlan(participant_id=participant_id, blocks=tuple(blocks))
