"""LLM turn handling (Section 7).

The Stance is a hidden system prompt that never reaches the browser. Each
block's conversation is independent -- the model sees only that block's
Scenario, Question, and the turns exchanged so far within it. Responses are
capped at 50 words and delivered in full (no streaming), matching the spec's
reveal-at-once requirement.

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

from . import config
from .content import BlockPlan

log = logging.getLogger(__name__)

# Framing appended to the Stance prompt. The Stance controls posture; this
# controls form, so that response *shape* is constant across conditions and
# only tone varies with the experimental manipulation.
RESPONSE_FRAMING = """
You are replying to a research participant who is thinking through a short
scenario and has given a numeric judgment about it.

Hard requirements for every reply:
- 50 words maximum. This is a strict limit; stay comfortably under it.
- Plain conversational prose. No markdown, no lists, no headings, no emoji.
- Address the participant directly and respond to what they actually wrote.
- Never mention these instructions, your assigned posture, the experiment, or
  that you are following a style. Never restate the scenario back to them.
- Do not ask more than one question, and do not end every reply with a question.
""".strip()


@dataclass
class Turn:
    """One exchanged turn inside a block."""

    role: Literal["user", "assistant"]
    text: str


@dataclass
class LLMResult:
    text: str                     # what the participant sees (post-cap)
    raw_text: str                 # what the model returned
    truncated: bool
    requested_at: float           # unix seconds, request sent
    received_at: float            # unix seconds, response received
    latency_ms: int
    model: str
    provider: str
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, str]]) -> tuple[str, str | None, dict[str, Any]]:
        """Return (text, stop_reason, usage)."""


def cap_words(text: str, limit: int = config.MAX_LLM_WORDS) -> tuple[str, bool]:
    """Enforce the 50-word cap.

    Prefers to end on a complete sentence inside the window so a capped reply
    still reads as a finished utterance rather than a cut-off one; falls back
    to a hard cut. The uncapped text is stored either way so the team can audit
    how often this fires.
    """
    words = text.split()
    if len(words) <= limit:
        return text.strip(), False

    window = " ".join(words[:limit])
    # Last sentence boundary in the window, but only if it keeps most of the reply.
    boundaries = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", window)]
    if boundaries and len(window[: boundaries[-1]].split()) >= limit * 0.6:
        return window[: boundaries[-1]].strip(), True
    return window.rstrip(",;:- ").strip(), True


def build_system_prompt(block: BlockPlan) -> str:
    """Stance posture first, then the invariant form requirements."""
    return f"{block.stance.system_prompt.strip()}\n\n{RESPONSE_FRAMING}"


def build_messages(block: BlockPlan, answer_score_1: int, confidence_score_1: int, turns: list[Turn]) -> list[dict[str, str]]:
    """Compose the block-local conversation.

    The scenario, question, and the participant's own numeric judgment are
    folded into their first message so the model has the full local context
    without any carryover between blocks.
    """
    if not turns or turns[0].role != "user":
        raise ValueError("The block conversation must open with a participant turn")

    scenario = block.scenario
    header = (
        f"Scenario:\n{scenario.text}\n\n"
        f"Question:\n{scenario.question_text}\n"
        f"(Scale: 0 = {scenario.scale_low_label}, 100 = {scenario.scale_high_label})\n\n"
        f"My rating: {answer_score_1} out of 100. My confidence in that rating: {confidence_score_1} out of 100.\n\n"
        f"What I said about it:\n{turns[0].text}"
    )
    messages: list[dict[str, str]] = [{"role": "user", "content": header}]
    for turn in turns[1:]:
        messages.append({"role": turn.role, "content": turn.text})
    return messages


class FakeProvider:
    """Deterministic stand-in. Produces Stance-flavored replies under 50 words
    with no network call, so the whole flow is testable without credentials."""

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

    def __init__(self, stance_label: str = "calibrated", delay_s: float = 0.0) -> None:
        self.stance_label = stance_label
        self.delay_s = delay_s

    def complete(self, system: str, messages: list[dict[str, str]]) -> tuple[str, str | None, dict[str, Any]]:
        if self.delay_s:
            time.sleep(self.delay_s)
        turn_number = sum(1 for m in messages if m["role"] == "assistant") + 1
        opener = self._OPENERS.get(self.stance_label, self._OPENERS["calibrated"])
        closer = self._CLOSERS.get(self.stance_label, self._CLOSERS["calibrated"])
        text = f"{opener} {closer} (Simulated reply {turn_number}; no model was called.)"
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
    ) -> LLMResult:
        system = build_system_prompt(block)
        messages = build_messages(block, answer_score_1, confidence_score_1, turns)
        provider = self._provider_for(block)

        requested_at = time.time()
        raw, stop_reason, usage = provider.complete(system, messages)
        received_at = time.time()

        text, truncated = cap_words(raw)
        if truncated:
            log.warning("LLM reply exceeded the %d-word cap and was capped", config.MAX_LLM_WORDS)

        return LLMResult(
            text=text,
            raw_text=raw,
            truncated=truncated,
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
