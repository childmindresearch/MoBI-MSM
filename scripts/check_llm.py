#!/usr/bin/env python3
"""One-shot check that the real model path works before a data-collection day.

Sends a single block-shaped request through the same code the task uses --
same LiteLLM call, same system prompt construction, same Stance, same word
cap and rewrite loop -- and prints the reply plus how long the model took.

Each block says whether its replies end on a question; --statement tests the
blocks that must not. The check reports how many rewrite rounds each reply
needed and whether any had to be delivered still non-compliant, and warns when
the model's latency would overrun the fixed thinking hold.

    .venv/bin/python scripts/check_llm.py
    .venv/bin/python scripts/check_llm.py --stance counterbalancing
    .venv/bin/python scripts/check_llm.py --turn 2 --statement
    .venv/bin/python scripts/check_llm.py --model openai/gpt-5 --repeat 5
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.content import ContentLibrary  # noqa: E402
from app.llm import LLMClient, Turn, build_system_prompt  # noqa: E402


def _missing_credentials(model: str) -> list[str]:
    """Which vendor keys LiteLLM would look for and not find. Empty is fine."""
    if config.LLM_API_BASE:
        return []  # a proxy carries its own credentials
    try:
        import litellm

        env = litellm.validate_environment(model=model)
    except Exception:
        return []  # unknown model or missing dependency -- let the real call report it
    return [] if env.get("keys_in_environment") else list(env.get("missing_keys") or [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stance", default="calibrated", choices=["aligning", "calibrated", "counterbalancing"])
    ap.add_argument("--provider", default="litellm", choices=["litellm", "fake"])
    ap.add_argument("--model", default=None,
                    help=f"LiteLLM model id to test instead of the configured {config.LLM_MODEL!r}")
    ap.add_argument("--repeat", type=int, default=1, help="Send N requests to sample latency spread")
    ap.add_argument("--turn", type=int, default=1, choices=[1, 2],
                    help="Which LLM turn in the block to simulate. Turn 2 is the last thing the "
                         "model says before the block moves on, and is guided differently.")
    ap.add_argument("--statement", action="store_true",
                    help="Simulate a block whose replies must end on a statement, not a question")
    args = ap.parse_args()

    if args.model:
        # Set before the client is built, so every code path below -- including
        # the credential check inside the provider -- sees the override.
        os.environ["MOBI_LLM_MODEL"] = args.model
        config.LLM_MODEL = args.model

    if args.provider == "litellm":
        missing = _missing_credentials(config.LLM_MODEL)
        if missing:
            print(f"No credentials found for {config.LLM_MODEL}. Set {', '.join(missing)} in .env "
                  "or export it, then re-run. (Or set MOBI_LLM_API_BASE for a LiteLLM proxy.)",
                  file=sys.stderr)
            return 2

    lib = ContentLibrary()
    block = lib.practice
    stance = lib.stances[args.stance]
    ends_with_question = not args.statement
    block = type(block)(**{**block.__dict__, "stance": stance, "ends_with_question": ends_with_question})

    print(f"provider={args.provider}  model={config.LLM_MODEL}  effort={config.LLM_EFFORT or '(unset)'}")
    if config.LLM_FALLBACK_MODELS:
        print(f"fallbacks={', '.join(config.LLM_FALLBACK_MODELS)}")
    if config.LLM_API_BASE:
        print(f"api_base={config.LLM_API_BASE}")
    print(f"stance={stance.name}  turn={args.turn}  ends_with_question={ends_with_question}  "
          f"max_rewrites={config.MAX_REWRITES}\n")
    print("--- system prompt ---")
    print(build_system_prompt(block, args.turn))
    print("\n--- replies ---")

    turns = [Turn("user", "I think routines help most people because they remove a lot of small daily decisions.")]
    if args.turn == 2:
        # Turn 2 only ever happens after an exchange, so give it one.
        turns += [
            Turn("assistant", "Removing small decisions does seem to free up attention for bigger "
                              "ones. Though some people find a fixed routine makes them less able to "
                              "adapt when the day goes sideways. Does that trade-off match your "
                              "experience?"),
            Turn("user", "Yes, that is fair, I have definitely had days where sticking to the plan "
                         "was clearly the wrong call for me."),
        ]

    client = LLMClient(args.provider)
    latencies = []
    over_cap = 0
    for i in range(args.repeat):
        result = client.respond(
            block,
            answer_score_1=70,
            confidence_score_1=55,
            turns=list(turns),
            turn_number=args.turn,
        )
        latencies.append(result.latency_ms)
        over_cap += result.over_cap
        print(f"[{i + 1}] {result.latency_ms} ms | {len(result.text.split())} words | "
              f"rewrites={result.rewrites} | over_cap={result.over_cap} | elicited={result.elicited}")
        for n, draft in enumerate(result.drafts, 1):
            if draft != result.text:
                print(f"    draft {n} ({len(draft.split())} words, rejected): {draft}")
        print(f"    {result.text}\n")

    if len(latencies) > 1:
        print(f"latency: min={min(latencies)}ms  max={max(latencies)}ms  "
              f"mean={sum(latencies) // len(latencies)}ms")
    if over_cap:
        print(f"WARNING: {over_cap} of {args.repeat} replies still broke the word cap or the "
              f"question rule after {config.MAX_REWRITES} rewrites and would have been "
              f"delivered as is.")
    overran = [ms for ms in latencies if ms > config.LLM_THINKING_HOLD_MS]
    print(f"\nEvery reply is held behind the thinking indicator for a fixed "
          f"{config.LLM_THINKING_HOLD_MS} ms.")
    if overran:
        print(f"WARNING: {len(overran)} of {len(latencies)} replies took longer than that, so "
              f"those trials would show a longer wait than the others.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
