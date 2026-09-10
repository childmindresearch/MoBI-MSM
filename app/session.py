"""The per-block flow state machine (Section 5).

The server owns block index and step position. The browser is told only what to
render next and what input to collect -- never the Stance, never the upcoming
blocks -- so the hidden manipulation cannot leak through the client and the
forward-only flow cannot be skipped or replayed from it.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from . import config, prompts
from .content import BlockPlan, ParticipantPlan
from .llm import LLMClient, LLMResult, Turn

StepName = Literal[
    "scenario", "answer_1", "confidence_1", "text_1", "llm_1",
    "text_2", "llm_2", "text_3", "answer_2", "confidence_2",
    "activation", "gate", "done",
]

# The order the participant moves through within every block, practice included.
STEP_ORDER: tuple[StepName, ...] = (
    "scenario", "answer_1", "confidence_1", "text_1", "llm_1",
    "text_2", "llm_2", "text_3", "answer_2", "confidence_2",
    "activation", "gate",
)

# Which block column each input step writes to.
STEP_FIELD: dict[str, str] = {
    "answer_1": "answer_score_1",
    "confidence_1": "confidence_score_1",
    "text_1": "user_text_1",
    "text_2": "user_text_2",
    "text_3": "user_text_3",
    "answer_2": "answer_score_2",
    "confidence_2": "confidence_score_2",
    "activation": "activation_score",
}

NUMERIC_STEPS = frozenset({"answer_1", "confidence_1", "answer_2", "confidence_2", "activation"})
TEXT_STEPS = frozenset({"text_1", "text_2", "text_3"})
LLM_STEPS = frozenset({"llm_1", "llm_2"})


class FlowError(RuntimeError):
    """A submission that does not match the step the session is actually on."""


class ValidationError(ValueError):
    """A submitted value that fails the Section 6 input rules."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def word_count(text: str) -> int:
    """Whitespace-delimited token count, as specified in Section 6."""
    return len(text.split())


@dataclass
class Message:
    """One rendered chat message."""

    id: str
    kind: Literal["scenario", "prompt", "llm", "gate", "notice", "answer", "interstitial"]
    text: str
    scale_low: str | None = None
    scale_high: str | None = None
    question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class InputSpec:
    """What the browser should collect next."""

    step: str
    field: str
    type: Literal["numeric", "text", "gate"]
    min: int | None = None
    max: int | None = None
    min_words: int | None = None
    scale_low: str | None = None
    scale_high: str | None = None
    placeholder: str | None = None
    button: str = "Submit"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class StepView:
    """The server's answer to 'what happens next'."""

    messages: list[Message] = field(default_factory=list)
    input: InputSpec | None = None
    awaiting_llm: bool = False
    done: bool = False
    block_index: int = 0
    block_label: str = ""
    is_practice: bool = False
    progress: dict[str, int] = field(default_factory=dict)
    reset_transcript: bool = False
    """Set only on the view that opens a block. Blocks are self-contained: the
    browser clears the transcript here, so a participant can never scroll back
    into a previous block's scenario or exchange."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "input": self.input.to_dict() if self.input else None,
            "awaiting_llm": self.awaiting_llm,
            "done": self.done,
            "block_index": self.block_index,
            "block_label": self.block_label,
            "is_practice": self.is_practice,
            "progress": self.progress,
            "reset_transcript": self.reset_transcript,
        }


class Session:
    """One participant's run, from Start Screen through End Screen."""

    def __init__(self, plan: ParticipantPlan, llm: LLMClient, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.plan = plan
        self.llm = llm
        self.started_at = now_iso()
        self.completed_at: str | None = None
        self._block_pos = 0                      # index into plan.blocks
        self._step: StepName = "scenario"
        self._values: dict[str, Any] = {}        # values for the current block
        self._turns: list[Turn] = []             # current block's LLM conversation
        self._msg_ids = itertools.count(1)

    # --- current position ---

    @property
    def participant_id(self) -> str:
        return self.plan.participant_id

    @property
    def block(self) -> BlockPlan:
        return self.plan.blocks[self._block_pos]

    @property
    def step(self) -> StepName:
        return self._step

    @property
    def is_final_block(self) -> bool:
        return self._block_pos == len(self.plan.blocks) - 1

    def _next_id(self) -> str:
        return f"m{next(self._msg_ids)}"

    def _block_label(self) -> str:
        block = self.block
        if block.is_practice:
            return "Practice"
        return f"{block.block_index} of {config.REAL_BLOCK_COUNT}"

    def _progress(self) -> dict[str, int]:
        return {
            "block_index": self.block.block_index,
            "total_blocks": config.REAL_BLOCK_COUNT,
            "step_position": STEP_ORDER.index(self._step) + 1 if self._step in STEP_ORDER else len(STEP_ORDER),
            "step_count": len(STEP_ORDER),
        }

    def _view(
        self,
        messages: list[Message],
        input_spec: InputSpec | None,
        *,
        awaiting_llm: bool = False,
        done: bool = False,
        reset_transcript: bool = False,
    ) -> StepView:
        return StepView(
            messages=messages,
            input=input_spec,
            awaiting_llm=awaiting_llm,
            done=done,
            reset_transcript=reset_transcript,
            block_index=self.block.block_index,
            block_label=self._block_label(),
            is_practice=self.block.is_practice,
            progress=self._progress(),
        )

    # --- rendering ---

    def open_block(self) -> StepView:
        """Render the Scenario and Question together, then ask for Answer Score 1
        (Section 5, steps 1-3)."""
        if self._step != "scenario":
            raise FlowError(f"open_block called while on step {self._step!r}")
        block = self.block
        messages: list[Message] = []

        if block.is_practice:
            messages.append(Message(self._next_id(), "interstitial", prompts.PRACTICE_INTRO))
        elif block.block_index == 1:
            messages.append(Message(self._next_id(), "interstitial", prompts.FIRST_REAL_BLOCK_INTRO))

        messages.append(
            Message(
                self._next_id(),
                "scenario",
                block.scenario.text,
                question=block.scenario.question_text,
                scale_low=block.scenario.scale_low_label,
                scale_high=block.scenario.scale_high_label,
            )
        )
        self._step = "answer_1"
        messages.append(Message(self._next_id(), "prompt", prompts.ANSWER_1))
        return self._view(messages, self._input_spec(), reset_transcript=True)

    def _input_spec(self) -> InputSpec | None:
        step = self._step
        scenario = self.block.scenario
        if step in NUMERIC_STEPS:
            if step in ("answer_1", "answer_2"):
                low, high = scenario.scale_low_label, scenario.scale_high_label
            elif step in ("confidence_1", "confidence_2"):
                low, high = prompts.CONFIDENCE_SCALE_LOW, prompts.CONFIDENCE_SCALE_HIGH
            else:
                low, high = prompts.ACTIVATION_SCALE_LOW, prompts.ACTIVATION_SCALE_HIGH
            return InputSpec(
                step=step, field=STEP_FIELD[step], type="numeric",
                min=config.SCORE_MIN, max=config.SCORE_MAX,
                scale_low=low, scale_high=high,
            )
        if step in TEXT_STEPS:
            return InputSpec(
                step=step, field=STEP_FIELD[step], type="text",
                min_words=config.MIN_RESPONSE_WORDS,
                placeholder=prompts.TEXT_PLACEHOLDER.get(step),
            )
        if step == "gate":
            return InputSpec(step="gate", field="advanced_at", type="gate", button="Continue")
        return None

    def _prompt_text(self, step: StepName) -> str:
        if step == "answer_1":
            return prompts.ANSWER_1
        if step == "confidence_1":
            return prompts.CONFIDENCE_1
        if step == "text_1":
            return prompts.TEXT_1.format(answer_score_1=self._values["answer_score_1"])
        # text_2 and text_3 deliberately have no prompt copy: the LLM reply
        # that precedes them asks for the response itself, so the participant
        # sees one message rather than a reply followed by a fixed question.
        if step == "answer_2":
            return prompts.ANSWER_2.format(question_text=self.block.scenario.question_text)
        if step == "confidence_2":
            return prompts.CONFIDENCE_2
        if step == "activation":
            return prompts.ACTIVATION
        if step == "gate":
            return prompts.GATE
        raise FlowError(f"No prompt copy for step {step!r}")

    # --- validation ---

    def validate(self, step: str, raw: Any) -> Any:
        """Server-side mirror of the Section 6 input rules. The browser blocks
        bad input inline; this is the authority."""
        if step != self._step:
            raise FlowError(f"Expected a submission for step {self._step!r}, got {step!r}")

        if step in NUMERIC_STEPS:
            text = str(raw).strip()
            if not text:
                raise ValidationError("Enter a number.")
            if not text.lstrip("+").isdigit():
                raise ValidationError("Enter a whole number with no decimal point.")
            value = int(text)
            if not (config.SCORE_MIN <= value <= config.SCORE_MAX):
                raise ValidationError(f"Enter a number between {config.SCORE_MIN} and {config.SCORE_MAX}.")
            return value

        if step in TEXT_STEPS:
            text = str(raw).strip()
            words = word_count(text)
            if words < config.MIN_RESPONSE_WORDS:
                raise ValidationError(
                    f"Please write at least {config.MIN_RESPONSE_WORDS} words "
                    f"({words} so far)."
                )
            return text

        if step == "gate":
            return True

        raise FlowError(f"Step {step!r} does not accept a submission")

    # --- progression ---

    def submit(self, step: str, raw: Any) -> tuple[Any, dict[str, Any], StepView]:
        """Record a validated submission and produce the next view.

        Returns (value, block_column_updates, next_view).
        """
        value = self.validate(step, raw)
        stamp = now_iso()
        updates: dict[str, Any] = {}

        if step in NUMERIC_STEPS:
            column = STEP_FIELD[step]
            self._values[column] = value
            updates[column] = value
            updates[f"{column}_at"] = stamp
        elif step in TEXT_STEPS:
            column = STEP_FIELD[step]
            self._values[column] = value
            self._turns.append(Turn("user", value))
            updates[column] = value
            updates[f"{column}_words"] = word_count(value)
            updates[f"{column}_at"] = stamp
        elif step == "gate":
            updates["advanced_at"] = stamp

        return value, updates, self._advance_from(step)

    def _advance_from(self, step: str) -> StepView:
        """Move to the next step and render whatever the participant sees next."""
        if step == "gate":
            return self._start_next_block()

        next_step = STEP_ORDER[STEP_ORDER.index(step) + 1]  # type: ignore[arg-type]

        # Section 5: Block 12 ends at the Activation Score -- no gate, straight to the End Screen.
        if step == "activation" and self.is_final_block:
            self._step = "done"
            self.completed_at = now_iso()
            return self._view([], None, done=True)

        self._step = next_step

        if next_step in LLM_STEPS:
            # The browser shows the thinking indicator and then asks for the turn.
            return self._view([], None, awaiting_llm=True)

        kind = "gate" if next_step == "gate" else "prompt"
        message = Message(self._next_id(), kind, self._prompt_text(next_step))
        return self._view([message], self._input_spec())

    def _start_next_block(self) -> StepView:
        self._block_pos += 1
        self._values = {}
        self._turns = []
        self._step = "scenario"
        return self.open_block()

    # --- LLM turns ---

    def run_llm_turn(self) -> tuple[LLMResult, dict[str, Any], StepView]:
        """Fetch the LLM reply for the step the session is waiting on."""
        if self._step not in LLM_STEPS:
            raise FlowError(f"No LLM turn is pending (current step: {self._step!r})")
        which = 1 if self._step == "llm_1" else 2

        result = self.llm.respond(
            self.block,
            answer_score_1=int(self._values["answer_score_1"]),
            confidence_score_1=int(self._values["confidence_score_1"]),
            turns=list(self._turns),
            turn_number=which,
        )
        self._turns.append(Turn("assistant", result.text))

        updates = {
            f"llm_text_{which}": result.text,
            f"llm_text_{which}_raw": result.raw_text,
            f"llm_text_{which}_truncated": result.truncated,
            f"llm_text_{which}_elicited": result.elicited,
            f"llm_text_{which}_requested_at": result.requested_at,
            f"llm_text_{which}_received_at": result.received_at,
            f"llm_text_{which}_latency_ms": result.latency_ms,
        }

        # The reply is the only message shown: it responds to the participant
        # and asks for their next response in the same breath, so no separate
        # standardized prompt follows it.
        message = Message(self._next_id(), "llm", result.text)
        next_step: StepName = "text_2" if which == 1 else "text_3"
        self._step = next_step
        view = self._view([message], self._input_spec())
        return result, updates, view

    # --- block metadata for persistence ---

    def block_meta(self) -> dict[str, Any]:
        block = self.block
        return {
            "session_id": self.session_id,
            "participant_id": self.participant_id,
            "block_index": block.block_index,
            "is_practice": block.is_practice,
            "category_label": block.category_label,
            "category_name": block.category_name,
            "scenario_label": block.scenario.label,
            "scenario_text": block.scenario.text,
            "question_text": block.scenario.question_text,
            "scale_low_label": block.scenario.scale_low_label,
            "scale_high_label": block.scenario.scale_high_label,
            "stance_label": block.stance.label,
            "stance_name": block.stance.name,
        }
