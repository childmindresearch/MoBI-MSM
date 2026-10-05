#!/usr/bin/env python3
"""Generate the counterbalanced pilot rows of assignment.csv.

Sixteen participants, PILOT_001 to PILOT_016. Deterministic: the same seed
always produces the same rows, so the design can be regenerated and audited.

Within each participant (hard constraints, as for any assignment row):
  - Blocks 1-12 cover every Category x Stance cell exactly once.
  - Within each stance, two blocks end on a question and two do not.
  - Each category ends on a question in one or two of its three blocks.
  - No category and no stance appears in two consecutive blocks.
  - Twelve distinct vignettes, each from its block's category.

Across the sixteen (balanced exactly unless noted):
  - Each category appears in each block position 4 times.
  - Each block position has 8 question blocks and 8 statement blocks.
  - Each stance appears in each block position 5 or 6 times (16/3 is not
    whole, so this is as even as it can be).
  - Each Category x Stance cell is a question block for 8 participants.
  - Each of the 96 vignettes is used exactly twice, under two different
    stances, once in a question block and once in a statement block.

    .venv/bin/python scripts/make_pilot_assignment.py            # report only
    .venv/bin/python scripts/make_pilot_assignment.py --write    # also update assignment.csv
"""

from __future__ import annotations

import argparse
import csv
import itertools
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

N = 16
PREFIX = "PILOT_"
CATEGORIES = ["INF", "DEC", "REF", "SUP"]
STANCES = ["aligning", "calibrated", "counterbalancing"]
ROUNDS = 3   # each category appears once per round of four blocks
SEED = 20261005


def latin_rows() -> list[list[int]]:
    """A 4x4 Latin square whose rows chain without repeating a category:
    the last category of row a is never the first of row a+1."""
    for perm in itertools.permutations(range(4)):
        rows = [[(perm[j] + i) % 4 for j in range(4)] for i in range(4)]
        if all(rows[a][3] != rows[(a + 1) % 4][0] for a in range(4)):
            return rows
    raise RuntimeError("no chaining Latin square")


ROWS = latin_rows()


def category_order(p: int) -> list[int]:
    # Round r of participant p uses row (p + r) % 4, so across participants
    # (four per offset) every category lands in every position four times.
    return [c for r in range(ROUNDS) for c in ROWS[(p + r) % 4]]


def stance_options() -> list[tuple[tuple[int, ...], ...]]:
    """Every way to give each category one of each stance across its 3 rounds:
    option[c][r] is the stance of category c in round r."""
    perms = list(itertools.permutations(range(3)))
    return list(itertools.product(perms, repeat=4))


def question_options() -> list[tuple[tuple[int, ...], ...]]:
    """q[c][s] patterns: two categories per stance end on a question, and each
    category ends on one in one or two of its three blocks."""
    pairs = list(itertools.combinations(range(4), 2))
    out = []
    for choice in itertools.product(pairs, repeat=3):
        q = tuple(tuple(int(c in choice[s]) for s in range(3)) for c in range(4))
        if all(1 <= sum(q[c]) <= 2 for c in range(4)):
            out.append(q)
    return out


def sequence(p: int, st, q) -> list[tuple[int, int, int]]:
    """(category, stance, question) for blocks 1-12."""
    cats = category_order(p)
    return [(c, st[c][i // 4], q[c][st[c][i // 4]]) for i, c in enumerate(cats)]


def valid(seq) -> bool:
    return all(a[0] != b[0] and a[1] != b[1] for a, b in zip(seq, seq[1:]))


def stance_cost(stance_seqs) -> int:
    """Distance from 5-6 of each stance in each position."""
    total = 0
    for pos in range(12):
        n = Counter(seq[pos] for seq in stance_seqs)
        total += sum(max(0, 5 - n[s]) + max(0, n[s] - 6) for s in range(3))
    return total


def question_cost(seqs) -> int:
    """Distance from 8 question blocks per position and 8 per cell."""
    total = sum(abs(sum(seq[pos][2] for seq in seqs) - N // 2) for pos in range(12))
    cells = Counter((c, s) for seq in seqs for c, s, q in seq if q)
    return total + sum(abs(cells[(c, s)] - N // 2) for c in range(4) for s in range(3))


def descend(n_options, current, evaluate, rng, sweeps=200):
    """Coordinate descent: give each participant in turn the option that
    minimises the total, until a full sweep changes nothing or it hits 0."""
    best = evaluate(current)
    for _ in range(sweeps):
        changed = False
        for p in rng.sample(range(N), N):
            options = list(range(n_options[p]))
            rng.shuffle(options)
            for o in options:
                old = current[p]
                current[p] = o
                c = evaluate(current)
                if c < best:
                    best, changed = c, True
                else:
                    current[p] = old
            if best == 0:
                return best
        if not changed:
            return best
    return best


def search(rng: random.Random):
    # Phase 1: stances. Only options with no stance repeated in consecutive
    # blocks are eligible (the category order already never repeats).
    st_valid = []
    for p in range(N):
        cats = category_order(p)
        ok = []
        for st in stance_options():
            stances = [st[c][i // 4] for i, c in enumerate(cats)]
            if all(a != b for a, b in zip(stances, stances[1:])):
                ok.append((st, stances))
        st_valid.append(ok)

    for restart in range(200):
        st_pick = [rng.randrange(len(st_valid[p])) for p in range(N)]
        residual = descend([len(v) for v in st_valid], st_pick,
                           lambda pick: stance_cost([st_valid[p][pick[p]][1] for p in range(N)]), rng)
        if residual == 0:
            break
    else:
        raise RuntimeError("could not balance stances across positions")
    st = [st_valid[p][st_pick[p]][0] for p in range(N)]

    # Phase 2: question flags, with the stances fixed.
    q_all = question_options()
    for restart in range(200):
        q_pick = [rng.randrange(len(q_all)) for _ in range(N)]
        residual = descend([len(q_all)] * N, q_pick,
                           lambda pick: question_cost([sequence(p, st[p], q_all[pick[p]]) for p in range(N)]), rng)
        if residual == 0:
            break
    else:
        raise RuntimeError("could not balance question flags")
    return [sequence(p, st[p], q_all[q_pick[p]]) for p in range(N)], 0


def assign_vignettes(seqs, vids_by_cat, rng: random.Random):
    """Each vignette twice: different participants, different stances, one
    question block and one statement block."""
    out = [[None] * 12 for _ in range(N)]
    for c, cat in enumerate(CATEGORIES):
        slots = [(p, i, seqs[p][i][1], seqs[p][i][2]) for p in range(N) for i in range(12) if seqs[p][i][0] == c]
        q_slots = [s for s in slots if s[3] == 1]
        n_slots = [s for s in slots if s[3] == 0]
        assert len(q_slots) == len(n_slots) == len(vids_by_cat[cat]), (cat, len(q_slots), len(n_slots))
        for _ in range(10_000):
            rng.shuffle(q_slots)
            rng.shuffle(n_slots)
            # Pair each question slot with a statement slot from another
            # participant and a different stance; a pair shares one vignette.
            remaining = list(n_slots)
            pairs = []
            for qs in q_slots:
                match = next((ns for ns in remaining if ns[0] != qs[0] and ns[2] != qs[2]), None)
                if match is None:
                    break
                remaining.remove(match)
                pairs.append((qs, match))
            if len(pairs) == len(q_slots):
                break
        else:
            raise RuntimeError(f"could not pair vignette slots for {cat}")
        vids = list(vids_by_cat[cat])
        rng.shuffle(vids)
        for vid, (a, b) in zip(vids, pairs):
            out[a[0]][a[1]] = vid
            out[b[0]][b[1]] = vid
    return out


def report(seqs, vignettes) -> bool:
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= cond
        print(f"  [{'ok' if cond else 'NO'}] {label}{(' -- ' + detail) if detail else ''}")

    print("Within each participant")
    check("every Category x Stance cell once",
          all(len({(c, s) for c, s, _ in seq}) == 12 for seq in seqs))
    check("2 question / 2 statement blocks per stance",
          all(Counter(s for _, s, q in seq if q) == Counter({0: 2, 1: 2, 2: 2}) for seq in seqs))
    check("no category or stance in consecutive blocks", all(valid(seq) for seq in seqs))
    check("12 distinct vignettes, each from its block's category",
          all(len(set(v)) == 12 and all(x.startswith(CATEGORIES[seq[i][0]]) for i, x in enumerate(v))
              for seq, v in zip(seqs, vignettes)))

    print("Across participants")
    cat_pos = [Counter(seq[i][0] for seq in seqs) for i in range(12)]
    check("each category in each position 4 times", all(set(c.values()) == {4} for c in cat_pos))
    q_pos = [sum(seq[i][2] for seq in seqs) for i in range(12)]
    check("8 question blocks in each position", set(q_pos) == {8}, f"per position: {q_pos}")
    st_pos = [Counter(seq[i][1] for seq in seqs) for i in range(12)]
    spread = sorted({n for c in st_pos for n in c.values()})
    check("each stance in each position 5 or 6 times", set(spread) <= {5, 6}, f"counts seen: {spread}")
    cells = Counter((c, s) for seq in seqs for c, s, q in seq if q)
    check("each cell is a question block for 8 participants", set(cells.values()) == {8},
          f"counts seen: {sorted(set(cells.values()))}")
    uses: dict[str, list] = {}
    for seq, vs in zip(seqs, vignettes):
        for (c, s, q), v in zip(seq, vs):
            uses.setdefault(v, []).append((s, q))
    check("all 96 vignettes used exactly twice", len(uses) == 96 and all(len(u) == 2 for u in uses.values()))
    check("each vignette under two stances, once question and once statement",
          all(u[0][0] != u[1][0] and u[0][1] != u[1][1] for u in uses.values()))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="Replace the PILOT_ rows in assignment.csv")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    with (config.FIXTURES_DIR / "vignettes_appraisals.csv").open(newline="", encoding="utf-8-sig") as f:
        vids_by_cat: dict[str, list[str]] = {}
        for r in csv.DictReader(f):
            vids_by_cat.setdefault(r["Intent"], []).append(r["VID"])

    rng = random.Random(args.seed)
    seqs, residual = search(rng)
    vignettes = assign_vignettes(seqs, vids_by_cat, rng)
    print(f"seed {args.seed}, residual imbalance {residual}\n")
    ok = report(seqs, vignettes)

    rows = []
    for p, (seq, vs) in enumerate(zip(seqs, vignettes)):
        row = {"participant_id": f"{PREFIX}{p + 1:03d}"}
        for n, ((c, s, q), v) in enumerate(zip(seq, vs), 1):
            row |= {f"category_{n}": CATEGORIES[c], f"vignette_{n}": v,
                    f"stance_{n}": STANCES[s], f"question_{n}": str(q)}
        rows.append(row)

    if args.write:
        if not ok:
            print("\nNot writing: the design failed a check above.", file=sys.stderr)
            return 1
        path = config.FIXTURES_DIR / "assignment.csv"
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            kept = [r for r in reader if not r["participant_id"].startswith(PREFIX)]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            w.writeheader()
            w.writerows([*kept, *rows])
        print(f"\nWrote {len(rows)} pilot rows to {path} ({len(kept)} other rows kept).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
