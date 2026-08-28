#!/usr/bin/env python3
"""One-shot check that the real Claude API path works before a data-collection day.

Sends a single block-shaped request through the same code the task uses --
same system prompt construction, same Stance, same 50-word cap -- and prints
the reply plus the latency the participant would have waited.

    .venv/bin/python scripts/check_llm.py
    .venv/bin/python scripts/check_llm.py --stance counterbalancing
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stance", default="calibrated", choices=["aligning", "calibrated", "counterbalancing"])
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "fake"])
    ap.add_argument("--repeat", type=int, default=1, help="Send N requests to sample latency spread")
    args = ap.parse_args()

    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Put it in .env or export it, then re-run.", file=sys.stderr)
        return 2

    lib = ContentLibrary()
    block = lib.practice
    stance = lib.stances[args.stance]
    block = type(block)(**{**block.__dict__, "stance": stance})

    print(f"provider={args.provider}  model={config.LLM_MODEL}  effort={config.LLM_EFFORT}")
    print(f"stance={stance.name}\n")
    print("--- system prompt ---")
    print(build_system_prompt(block))
    print("\n--- replies ---")

    client = LLMClient(args.provider)
    latencies = []
    for i in range(args.repeat):
        result = client.respond(
            block,
            answer_score_1=70,
            confidence_score_1=55,
            turns=[Turn("user", "I think routines help most people because they remove a lot of small daily decisions.")],
        )
        latencies.append(result.latency_ms)
        print(f"[{i + 1}] {result.latency_ms} ms | {len(result.text.split())} words | truncated={result.truncated}")
        print(f"    {result.text}\n")

    if len(latencies) > 1:
        print(f"latency: min={min(latencies)}ms  max={max(latencies)}ms  "
              f"mean={sum(latencies) // len(latencies)}ms")
    print(f"\nThinking indicator switches to its 'still thinking' state after "
          f"{config.THINKING_SLOW_AFTER_MS} ms.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
