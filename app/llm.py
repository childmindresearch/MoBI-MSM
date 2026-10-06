"""LLM turn handling (Section 7).

The Stance is a hidden system prompt that never reaches the browser. Each
block's conversation is independent: the model is sent only that block's
vignette, appraisal, ratings and the turns exchanged so far within it. That
isolation is structural, not a request in the prompt -- there is no shared
conversation for an earlier block to leak through.

Two things about every reply are enforced by a loop rather than trusted to
the prompt or fixed up afterwards: it is at most MAX_LLM_WORDS long, and it
ends with a question or does not, as the block's flag (question_N in the
assignment file) says. A reply that misses either is handed back to the model
with the problem named and a request to rewrite it complete; the code checks
again and repeats. Nothing is ever cut by code. If the model still has not
complied after MAX_REWRITES rounds, the best attempt goes out and the block
row says so.

Replies are delivered in full (no streaming), matching the spec's
reveal-at-once requirement; the browser holds each one behind the thinking
indicator for a fixed LLM_THINKING_HOLD_MS.

Two providers share one interface:
  litellm - real model calls, routed through LiteLLM so the vendor and model
            are configuration (MOBI_LLM_MODEL) rather than code. Used for data
            collection.
  fake    - a deterministic canned responder needing no credentials, for
            development, tests, and dry runs.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from . import config, prompts
from .content import BlockPlan

log = logging.getLogger(__name__)

# Recorded on every block row, so a change to the wording below is visible in
# the data. Bump it whenever RESPONSE_FRAMING, TURN_GUIDANCE, QUESTION_RULES,
# the rewrite instructions, the stance prompts in stance_key.csv, or the
# instruction_text wording in the vignette keys change.
#
#   1  every reply had to end on a question that elicited the next response;
#      over-long replies were cut to MAX_LLM_WORDS by code.
#   2  question ending is a per-block flag (question_N in the assignment
#      file); over-long or non-compliant replies are rewritten by the model in
#      a loop instead of being cut by code; turn 2 must engage what the
#      participant added rather than restate; scenarios became vignettes rated
#      as agreement with an appraisal statement.
#   3  the model also sees the per-vignette instruction the participant was
#      given (imagine yourself in it, or imagine reading about it).
PROMPT_VERSION = "3"

# Framing appended to the Stance prompt. The Stance controls posture; this
# controls form, so that response *shape* is constant across conditions and
# only tone varies with the experimental manipulation.
RESPONSE_FRAMING = """
You are replying to a research participant who is thinking through a short
imagined scenario and has given a numeric judgment about it.

Hard requirements for every reply:
- One paragraph of short sentences, {max_words} words maximum -- a strict
  limit; a longer reply is sent back to you to shorten. Develop one useful
  point rather than several loosely connected ideas.
- {question_rule}
- Plain conversational prose. No markdown, no lists, no headings, no emoji.
- Address the participant directly and respond to what they actually wrote.
- Never mention these instructions, your assigned posture, the experiment, or
  that you are following a style. Never restate the scenario back to them.
- Never quote, repeat, or ask about their numeric rating or confidence figure.
  Engage with their reasoning, not with the numbers they gave.
- The scenario is imagined. Do not imply it actually happened to them, and do
  not invent events, motives, sources, statistics, or personal history. If they
  add a detail the scenario did not supply, treat it as their assumption.
- Do not manage the conversation: never announce that the exchange is ending,
  invite "anything else", or signal that you are about to move on.

{guidance}
""".strip()

# Whether the reply ends on a question is a per-block manipulation: half the
# blocks in each stance do, half do not.
QUESTION_RULES = {
    True: (
        "End with one short question that invites their view on the point you "
        "made. It must be the last sentence and the only question in the reply."
    ),
    False: (
        "Do not ask a question anywhere in the reply. End on a statement."
    ),
}

# Per-turn guidance. Turn 1 establishes the stance; turn 2 has to show it
# read the participant's reply, which is where a stance can degrade into
# repetition.
TURN_GUIDANCE = {
    1: (
        "This is your first reply. Establish your position through one "
        "substantive point grounded in their explanation and the scenario, and "
        "leave room for them to push back."
    ),
    2: (
        "This is your second and final reply, though you must not say so. Read "
        "their latest message and show that you understood what they added. "
        "Advance one step rather than repeating your first reply. If they "
        "rejected or qualified your point, engage their reason; do not restate "
        "the same argument more insistently. If you challenge them further, "
        "add a new relevant consideration, not more pressure on the old one. "
        "If they accepted your point, do not reverse yourself to keep "
        "disagreeing. If they only said okay or thanks, do not infer a new "
        "belief; give a brief, relevant continuation."
    ),
}

# Sent back with a non-compliant draft. {problems} is one or more of the
# sentences below, joined.
REWRITE_INSTRUCTION = (
    "{problems} Rewrite your reply so that it complies, as a complete reply "
    "with the same substance -- summarize rather than cut, and keep it to one "
    "paragraph of plain prose. Output only the rewritten reply."
)
PROBLEM_TOO_LONG = "Your reply was {n} words; the limit is {max_words}."
PROBLEM_NO_QUESTION = "Your reply must end with one short question, as the last sentence."
PROBLEM_HAS_QUESTION = "Your reply must not contain a question; it must end on a statement."


@dataclass
class Turn:
    """One exchanged turn inside a block."""

    role: Literal["user", "assistant"]
    text: str


@dataclass
class LLMResult:
    text: str                     # what the participant sees
    raw_text: str                 # the model's first draft, exactly as returned
    drafts: list[str]             # every draft, normalized, first to last
    rewrites: int                 # rewrite rounds it took (0 = first draft complied)
    over_cap: bool                # delivered without complying, after MAX_REWRITES
    ends_with_question: bool      # what the block asked for
    elicited: bool                # what was delivered: does it end on a question
    requested_at: float           # unix seconds, first request sent
    received_at: float            # unix seconds, final reply received
    latency_ms: int
    model: str
    provider: str
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, str]]) -> tuple[str, str | None, dict[str, Any]]:
        """Return (text, stop_reason, usage)."""


def word_count(text: str) -> int:
    return len(text.split())


# Emphasis markers the framing already forbids. Models emit them anyway, and the
# transcript renders as plain text, so *this* would reach the participant with
# the asterisks visible.
_EMPHASIS = re.compile(r"(?<!\w)([*_]{1,2})(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL)


def normalize_reply(text: str) -> str:
    """Flatten a reply to the one shape every trial should present.

    Two things models do despite the framing telling them not to: emphasis
    markers, which the transcript renders as literal asterisks because messages
    are set as plain text; and paragraph breaks, which the stylesheet honours
    (`white-space: pre-wrap`) and which would make some replies one block and
    others two. Reply shape is part of the stimulus and is measured as an eye
    tracking AOI, so it should not vary with the model's formatting whims.
    """
    text = _EMPHASIS.sub(r"\2", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()


def ends_in_question(text: str) -> bool:
    """Whether a reply closes on a question. Recorded per turn, so the team can
    check the delivered shape against the block's flag rather than assume it."""
    return text.rstrip().endswith("?")


def contains_question(text: str) -> bool:
    return "?" in text


def problems_with(text: str, ends_with_question: bool) -> list[str]:
    """What a draft gets wrong, as the sentences the rewrite request uses.
    Empty means it complies."""
    out: list[str] = []
    n = word_count(text)
    if n > config.MAX_LLM_WORDS:
        out.append(PROBLEM_TOO_LONG.format(n=n, max_words=config.MAX_LLM_WORDS))
    if ends_with_question and not ends_in_question(text):
        out.append(PROBLEM_NO_QUESTION)
    if not ends_with_question and contains_question(text):
        out.append(PROBLEM_HAS_QUESTION)
    return out


def build_system_prompt(block: BlockPlan, turn_number: int = 1, ends_with_question: bool | None = None) -> str:
    """Stance posture first, then the invariant form requirements.

    Only the turn guidance and the question rule differ between calls, and the
    question rule is fixed for the whole block."""
    if ends_with_question is None:
        ends_with_question = block.ends_with_question
    guidance = TURN_GUIDANCE.get(turn_number, TURN_GUIDANCE[2])
    framing = RESPONSE_FRAMING.format(
        max_words=config.MAX_LLM_WORDS,
        question_rule=QUESTION_RULES[ends_with_question],
        guidance=guidance,
    )
    return f"{block.stance.system_prompt.strip()}\n\n{framing}"


def build_messages(block: BlockPlan, answer_score_1: int, confidence_score_1: int, turns: list[Turn]) -> list[dict[str, str]]:
    """Compose the block-local conversation.

    The vignette, its instruction, its appraisal, and the participant's own numeric judgment
    are folded into their first message so the model has the full local
    context without any carryover between blocks. The model-facing labels say
    "Scenario" to match the wording of RESPONSE_FRAMING.
    """
    if not turns or turns[0].role != "user":
        raise ValueError("The block conversation must open with a participant turn")

    vignette = block.vignette
    header = (
        f"Scenario:\n{vignette.text}\n\n"
        f"Instruction I was given:\n{vignette.instruction_text}\n\n"
        f"Statement I rated:\n{prompts.APPRAISAL.format(appraisal_text=vignette.appraisal_text)}\n"
        f"(Scale: 0 = {prompts.APPRAISAL_SCALE_LOW}, 100 = {prompts.APPRAISAL_SCALE_HIGH})\n\n"
        f"My rating: {answer_score_1} out of 100. My confidence in that rating: {confidence_score_1} out of 100.\n\n"
        f"What I said about it:\n{turns[0].text}"
    )
    messages: list[dict[str, str]] = [{"role": "user", "content": header}]
    for turn in turns[1:]:
        messages.append({"role": turn.role, "content": turn.text})
    return messages


class FakeProvider:
    """Deterministic stand-in. Produces Stance-flavored replies under the word
    cap with no network call, so the whole flow is testable without credentials.

    Its replies honour the question rule in the system prompt, so a dry run
    exercises the same shapes the participant actually sees."""

    name = "fake"

    _OPENERS = {
        "aligning": "That reasoning holds up well, and the way you weighed it makes sense.",
        "calibrated": "That is a reasonable read, though a fair amount here is genuinely uncertain.",
        "counterbalancing": "That is one plausible reading, but there is a real case pointing the other way.",
    }
    _CLOSERS = {
        "aligning": "The evidence you are leaning on tends to point in the direction you named.",
        "calibrated": "Both the effect you describe and a much smaller one are consistent with what you observed.",
        "counterbalancing": "People in similar situations often find the opposite explanation fits just as well.",
    }
    _QUESTION = "Does that match how you were thinking about it?"
    _STATEMENT = "Either way, the qualification you added seems worth holding onto."

    def __init__(self, stance_label: str = "calibrated", delay_s: float = 0.0) -> None:
        self.stance_label = stance_label
        self.delay_s = delay_s

    def complete(self, system: str, messages: list[dict[str, str]]) -> tuple[str, str | None, dict[str, Any]]:
        if self.delay_s:
            time.sleep(self.delay_s)
        turn_number = sum(1 for m in messages if m["role"] == "assistant") + 1
        opener = self._OPENERS.get(self.stance_label, self._OPENERS["calibrated"])
        closer = self._CLOSERS.get(self.stance_label, self._CLOSERS["calibrated"])
        ending = self._STATEMENT if QUESTION_RULES[False] in system else self._QUESTION
        text = f"{opener} {closer} (Simulated reply {turn_number}; no model was called.) {ending}"
        return text, "end_turn", {"provider": "fake"}


class LiteLLMProvider:
    """Real model calls, routed through LiteLLM.

    LiteLLM presents every supported vendor behind one OpenAI-shaped call, so
    which model answers the participant is a configuration value rather than a
    code change: `anthropic/claude-opus-5`, `openai/gpt-5`,
    `gemini/gemini-2.5-pro`, `ollama/llama3.3` for a locally hosted model, and
    so on. Credentials come from each vendor's own environment variable, or
    from MOBI_LLM_API_BASE / MOBI_LLM_API_KEY when the lab routes everything
    through a LiteLLM proxy.

    Non-streaming by design: the spec reveals each reply in full at once, and
    the replies are short. Configured fallback models are tried in order when
    the primary one fails, so a vendor outage degrades to another model rather
    than stalling a trial.
    """

    name = "litellm"

    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import litellm  # imported lazily so the fake provider needs no dependency

        # Vendors disagree about which optional parameters they accept, and a
        # rejected parameter would fail the turn. Dropping the unsupported ones
        # is what lets a single configuration move between models.
        litellm.drop_params = True
        # Nothing here should reach a third party beyond the model vendor.
        litellm.telemetry = False
        # LiteLLM logs two INFO lines per call, which would bury this app's own
        # markers in the server log across ~39 turns a session.
        litellm.suppress_debug_info = True
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)

        self._litellm = litellm
        self.model = model or config.LLM_MODEL
        self.effort = config.LLM_EFFORT if effort is None else effort
        self.fallbacks = list(config.LLM_FALLBACK_MODELS)
        self._warn_if_credentials_missing()
        self._warn_if_proxy_misaddressed()

    def _warn_if_credentials_missing(self) -> None:
        """Say so at startup rather than on the participant's first turn."""
        if config.LLM_API_BASE:
            return  # a proxy carries its own credentials
        try:
            env = self._litellm.validate_environment(model=self.model)
        except Exception:  # unknown model name, or an API change -- not fatal here
            return
        missing = env.get("missing_keys") or []
        if missing and not env.get("keys_in_environment"):
            log.warning(
                "No credentials found for %r. Set %s in .env, or point "
                "MOBI_LLM_API_BASE at a LiteLLM proxy.",
                self.model, ", ".join(missing),
            )

    # Model prefixes that actually route to a LiteLLM proxy server. Anything
    # else combined with an api_base goes to that vendor's own handler instead,
    # pointed at the proxy's URL -- which is not a route the proxy serves.
    _PROXY_PREFIXES = ("litellm_proxy/", "openai/")

    def _warn_if_proxy_misaddressed(self) -> None:
        """Catch the api_base/model mismatch here, not on a collection day."""
        if not config.LLM_API_BASE:
            return
        if self.model.startswith(self._PROXY_PREFIXES):
            return
        vendor, _, rest = self.model.partition("/")
        log.warning(
            "MOBI_LLM_API_BASE is set but MOBI_LLM_MODEL is %r, which routes to the "
            "%s handler aimed at that URL rather than to the proxy. For a LiteLLM "
            "proxy use 'litellm_proxy/%s' -- the model name as configured on the proxy.",
            self.model, vendor or "default", rest or self.model,
        )

    def _call_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
        if config.LLM_API_BASE:
            kwargs["api_base"] = config.LLM_API_BASE
        if config.LLM_API_KEY:
            kwargs["api_key"] = config.LLM_API_KEY
        if self.fallbacks:
            kwargs["fallbacks"] = self.fallbacks
        return kwargs

    def complete(self, system: str, messages: list[dict[str, str]]) -> tuple[str, str | None, dict[str, Any]]:
        # LiteLLM takes the system prompt as the first message rather than as a
        # separate argument, and translates it per vendor from there.
        payload = [{"role": "system", "content": system}, *messages]
        try:
            response = self._litellm.completion(
                model=self.model,
                messages=payload,
                max_tokens=config.LLM_MAX_TOKENS,
                timeout=config.LLM_TIMEOUT_S,
                num_retries=2,
                **self._call_kwargs(),
            )
        except Exception as exc:  # every vendor's errors arrive as LiteLLM exceptions
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        hidden = getattr(response, "_hidden_params", None) or {}
        counts = getattr(response, "usage", None)
        usage = {
            "input_tokens": getattr(counts, "prompt_tokens", None),
            "output_tokens": getattr(counts, "completion_tokens", None),
            # The model that actually answered, which differs from the one asked
            # for whenever a fallback fired.
            "model": getattr(response, "model", None) or self.model,
            "requested_model": self.model,
            "vendor": hidden.get("custom_llm_provider"),
        }

        # LiteLLM normalises a safety refusal onto OpenAI's finish reason; some
        # vendors still surface their own word for it.
        if finish_reason in {"content_filter", "refusal"}:
            log.error("Model refused the turn (finish_reason=%s, model=%s)", finish_reason, usage["model"])
            raise LLMRefusal(f"Model declined to respond (finish_reason={finish_reason})")

        text = (getattr(choice.message, "content", None) or "").strip()
        if not text:
            raise LLMError(f"Empty response from model (finish_reason={finish_reason})")
        return text, finish_reason, usage


class LLMError(RuntimeError):
    """Any failure to obtain a usable turn from the model."""


class LLMRefusal(LLMError):
    """The model declined to respond."""


# Accepted for configurations written before LiteLLM was introduced.
_PROVIDER_ALIASES = {"anthropic": "litellm", "real": "litellm"}


class LLMClient:
    """Provider-agnostic entry point used by the session flow."""

    def __init__(self, provider: str | None = None) -> None:
        name = (provider or config.LLM_PROVIDER).strip().lower()
        name = _PROVIDER_ALIASES.get(name, name)
        if name not in {"litellm", "fake"}:
            raise ValueError(f"Unknown LLM provider {name!r}; expected 'litellm' or 'fake'")
        self.provider_name = name
        self.model = "fake" if name == "fake" else config.LLM_MODEL
        # Built once, up front: importing LiteLLM takes seconds, and that cost
        # must not land inside a participant's first measured wait.
        self._live = LiteLLMProvider() if name == "litellm" else None

    def _provider_for(self, block: BlockPlan) -> Provider:
        if self._live is not None:
            return self._live
        return FakeProvider(stance_label=block.stance.label, delay_s=config.FAKE_LLM_DELAY_S)

    def respond(
        self,
        block: BlockPlan,
        answer_score_1: int,
        confidence_score_1: int,
        turns: list[Turn],
        turn_number: int = 1,
        ends_with_question: bool | None = None,
    ) -> LLMResult:
        if ends_with_question is None:
            ends_with_question = block.ends_with_question
        system = build_system_prompt(block, turn_number, ends_with_question)
        messages = build_messages(block, answer_score_1, confidence_score_1, turns)
        provider = self._provider_for(block)

        requested_at = time.time()
        raw, stop_reason, usage = provider.complete(system, messages)
        drafts = [normalize_reply(raw)]

        # The compliance loop: name what is wrong, ask for a complete rewrite,
        # check again. The model does the shortening, never code.
        while True:
            problems = problems_with(drafts[-1], ends_with_question)
            if not problems or len(drafts) > config.MAX_REWRITES:
                break
            log.info("LLM turn %d draft %d non-compliant: %s", turn_number, len(drafts), " ".join(problems))
            ask = REWRITE_INSTRUCTION.format(problems=" ".join(problems))
            retry = [*messages, {"role": "assistant", "content": drafts[-1]}, {"role": "user", "content": ask}]
            again, stop_reason, more = provider.complete(system, retry)
            drafts.append(normalize_reply(again))
            for key in ("input_tokens", "output_tokens"):
                if usage.get(key) is not None and more.get(key) is not None:
                    usage[key] += more[key]
        received_at = time.time()

        compliant = [d for d in drafts if not problems_with(d, ends_with_question)]
        if compliant:
            text, over_cap = compliant[-1], False
        else:
            # Best of a bad lot: the shortest attempt, delivered as is and
            # flagged. The row records every round, so this is auditable.
            text, over_cap = min(drafts, key=word_count), True
            log.warning("LLM turn %d still non-compliant after %d rewrites (%d words, question=%s); delivering shortest",
                        turn_number, len(drafts) - 1, word_count(text), ends_with_question)

        return LLMResult(
            text=text,
            raw_text=raw,
            drafts=drafts,
            rewrites=len(drafts) - 1,
            over_cap=over_cap,
            ends_with_question=ends_with_question,
            elicited=ends_in_question(text),
            requested_at=requested_at,
            received_at=received_at,
            latency_ms=int(round((received_at - requested_at) * 1000)),
            model=usage.get("model") or self.model,
            provider=provider.name,
            stop_reason=stop_reason,
            usage=usage,
        )


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
