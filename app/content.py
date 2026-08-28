"""Loading and resolution of the assignment file and the three key files.

Per Section 4 of the spec, block order and content come from a pre-generated
assignment CSV whose cells are lookup labels, resolved at runtime against a
category key, a scenario key, and a stance key. Validation of those files is
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
class Scenario:
    label: str
    category_label: str
    text: str
    question_text: str
    scale_low_label: str
    scale_high_label: str


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
    scenario: Scenario
    stance: Stance


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


class ContentLibrary:
    """The four content files, loaded once at startup and resolved per participant."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.dir = Path(fixtures_dir or config.FIXTURES_DIR)
        self.categories = self._load_categories()
        self.scenarios = self._load_scenarios()
        self.stances = self._load_stances()
        self.practice = self._load_practice()
        self._assignments = self._load_assignments()

    # --- loaders ---

    def _load_categories(self) -> dict[str, str]:
        return {r["category_label"]: r["category_name"] for r in _read_csv(self.dir / "category_key.csv")}

    def _load_scenarios(self) -> dict[str, Scenario]:
        out: dict[str, Scenario] = {}
        for r in _read_csv(self.dir / "scenario_key.csv"):
            out[r["scenario_label"]] = Scenario(
                label=r["scenario_label"],
                category_label=r["category_label"],
                text=r["scenario_text"],
                question_text=r["question_text"],
                scale_low_label=r["scale_low_label"],
                scale_high_label=r["scale_high_label"],
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
        row = _read_csv(self.dir / "practice_scenario.csv")[0]
        stance_label = row.get("stance_label") or "calibrated"
        stance = self.stances.get(stance_label)
        if stance is None:
            raise ContentError(f"Practice scenario references unknown stance {stance_label!r}")
        scenario = Scenario(
            label=row["scenario_label"],
            category_label="practice",
            text=row["scenario_text"],
            question_text=row["question_text"],
            scale_low_label=row["scale_low_label"],
            scale_high_label=row["scale_high_label"],
        )
        return BlockPlan(
            block_index=config.PRACTICE_BLOCK_INDEX,
            is_practice=True,
            category_label="practice",
            category_name="Practice",
            scenario=scenario,
            stance=stance,
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
            scenario_label = row.get(f"scenario_{n}", "")
            stance_label = row.get(f"stance_{n}", "")
            if not (category_label and scenario_label and stance_label):
                raise ContentError(f"{participant_id}: assignment row is missing values for block {n}")

            category_name = self.categories.get(category_label)
            if category_name is None:
                raise ContentError(f"{participant_id} block {n}: unknown category label {category_label!r}")
            scenario = self.scenarios.get(scenario_label)
            if scenario is None:
                raise ContentError(f"{participant_id} block {n}: unknown scenario label {scenario_label!r}")
            stance = self.stances.get(stance_label)
            if stance is None:
                raise ContentError(f"{participant_id} block {n}: unknown stance label {stance_label!r}")

            blocks.append(
                BlockPlan(
                    block_index=n,
                    is_practice=False,
                    category_label=category_label,
                    category_name=category_name,
                    scenario=scenario,
                    stance=stance,
                )
            )
        return ParticipantPlan(participant_id=participant_id, blocks=tuple(blocks))
