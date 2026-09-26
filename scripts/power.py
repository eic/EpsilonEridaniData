#!/usr/bin/env python3
"""How many human labels to DECIDE one review agent (or rubric version) is better than another,
and how much an AI-judge pre-pass reduces that.

The comparison is a PAIRED PREFERENCE test. On each shared task two arms (agent X vs Y, or
rubric v_new vs v_old) are reviewed; a labeller picks the better review or calls a tie. The
claim "X is better" is a SIGN TEST on the non-tie comparisons against p = 0.5. The required
number of labels is set by:

  * effect size p  -- the true fraction of decisive comparisons X wins (the dominant unknown),
  * tie rate       -- ties carry no directional signal, so they inflate the raw label count,
  * clustering     -- pairs sharing a PR/rubric are correlated (design effect > 1),
  * (with AI)      -- prediction-powered inference shrinks variance by ~(1 - rho^2), where rho
                      is AI-human agreement; AI screening of ties removes the tie penalty.

Everything below is estimated from the current eval data, so the numbers tighten as labels
accumulate. With a few dozen labels the effect-size estimate is very wide -- treat the agent
matchup tallies as "how sparse is the per-matchup data", not as decided win rates.

    python3 scripts/power.py

Part of the eval pipeline (make_pairs -> judge -> label -> calibration/power). Keep the README's
"Labelling and analysis" section in sync when these scripts' flags or behaviour change.
"""
import collections
import glob
import json
import math
from statistics import NormalDist

import tcdata

Z = NormalDist().inv_cdf
EFFECTS = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]


def load(glob_rel):
    return [json.loads(open(f).read()) for f in glob.glob(str(tcdata.ROOT / "eval" / glob_rel))]


def n_sign(p, alpha=0.05, power=0.80):
    """Decisive (non-tie) comparisons to reject p=0.5 at two-sided alpha, given true win rate p."""
    za, zb = Z(1 - alpha / 2), Z(power)
    return (za * 0.5 + zb * math.sqrt(p * (1 - p))) ** 2 / (p - 0.5) ** 2


def ver(r):
    pf = r["judge"]["prompt_file"]
    return 3 if "v3" in pf else 2 if "v2" in pf else 1


def modal(ws):
    return collections.Counter(ws).most_common(1)[0][0] if ws else None


def consensus(judgments, spec):
    """Per-pair order-stable verdict for a judge, using its latest prompt version on that pair."""
    rs = [r for r in judgments if r["judge"]["spec"] == spec]
    best = collections.defaultdict(int)
    for r in rs:
        best[r["pair_id"]] = max(best[r["pair_id"]], ver(r))
    rs = [r for r in rs if ver(r) == best[r["pair_id"]]]
    byp = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rs:
        byp[r["pair_id"]][r["order"]].append(r["winner_arm"])
    out = {}
    for pid, po in byp.items():
        a, b = modal(po.get("ab", [])), modal(po.get("ba", []))
        if a is not None and a == b:
            out[pid] = a
    return out


def main():
    decs = load("decisions/*.json")
    pairs = {p["pair_id"]: p for p in load("pairs/*.json")}
    judgments = load("judgments/*.json")
    hd = {d["pair_id"]: d["winner_arm"] for d in decs}

    wd = collections.Counter(hd.values())
    n = len(hd)
    tie_rate = wd["tie"] / n if n else 0.0
    print(f"human labels: {n}   winners={dict(wd)}   tie rate={tie_rate:.0%}")

    # --- per-matchup tallies: how sparse is the per-agent data? ---
    print("\nper-matchup decisive tallies (winner by model; shows data sparsity, NOT decided rates):")
    mt = collections.defaultdict(collections.Counter)
    for d in decs:
        p = pairs.get(d["pair_id"])
        if not p:
            continue
        ma = p["arms"]["a"].get("model") or p["arms"]["a"].get("provider")
        mb = p["arms"]["b"].get("model") or p["arms"]["b"].get("provider")
        key = tuple(sorted([str(ma), str(mb)]))
        if d["winner_arm"] == "tie":
            mt[key]["tie"] += 1
        else:
            mt[key][ma if d["winner_arm"] == "a" else mb] += 1
    for key, c in sorted(mt.items(), key=lambda kv: -sum(kv[1].values())):
        dec = sum(v for k, v in c.items() if k != "tie")
        print(f"  {key[0]} vs {key[1]}: {dict(c)}  ({dec} decisive)")

    # --- AI-human agreement -> rho (for the prediction-powered boost) ---
    print("\nAI-human sign agreement on decisive-decisive pairs  (rho ~ 2*agree-1):")
    rhos = {}
    for spec in ["sonnet", "grok", "opus", "gpt-5.5", "deepseek"]:
        C = consensus(judgments, spec)
        both = [pid for pid in hd if pid in C and hd[pid] in ("a", "b") and C[pid] in ("a", "b")]
        if not both:
            continue
        agree = sum(1 for pid in both if hd[pid] == C[pid]) / len(both)
        rhos[spec] = (2 * agree - 1, len(both), agree)
        print(f"  {spec:<9}: {agree:.0%} agree on n={len(both):<2}  -> rho~{2*agree-1:+.2f}")

    # --- power tables ---
    deff = 1.0  # design effect placeholder; >1 once intra-PR correlation is estimated
    print(f"\nHUMAN LABELS to decide X>Y at 80% power, alpha=0.05 (tie rate {tie_rate:.0%}, DEFF={deff}):")
    print(f"  {'true win rate p':>16} | {'decisive needed':>15} | {'total labels':>12}")
    for p in EFFECTS:
        nd = n_sign(p) * deff
        tot = nd / (1 - tie_rate) if tie_rate < 1 else float("inf")
        print(f"  {p:>16.0%} | {nd:>15.0f} | {tot:>12.0f}")

    # --- AI boost: PPI variance reduction (1 - rho^2), at a representative rho ---
    print("\nAI-BOOSTED labels (prediction-powered inference, reduction ~ (1 - rho^2)):")
    print("  AI labels the full pool cheaply; humans label a random subset to debias.")
    print(f"  {'AI-human rho':>16} | {'factor (1-rho^2)':>16} | {'p=0.60':>8} | {'p=0.70':>8} | {'p=0.75':>8}")
    base = {p: n_sign(p) / (1 - tie_rate) for p in (0.60, 0.70, 0.75)}
    for rho in [0.3, 0.5, 0.6, 0.7, 0.8, 0.9]:
        f = 1 - rho ** 2
        row = " | ".join(f"{base[p]*f:>8.0f}" for p in (0.60, 0.70, 0.75))
        print(f"  {rho:>16.2f} | {f:>16.2f} | {row}")

    if rhos:
        best = max(rhos.values(), key=lambda v: v[1])  # most-data judge
        r = max(0.0, best[0])
        print(f"\n  At our current best-sampled judge (rho~{best[0]:+.2f}, n={best[1]}): "
              f"PPI factor {1-r**2:.2f}  (~{(1-(1-r**2))*100:.0f}% fewer human labels), "
              f"PLUS skipping AI-predicted ties.")
        print("  Caveat: rho itself is estimated on a handful of pairs; it will move with more labels.")


if __name__ == "__main__":
    main()
