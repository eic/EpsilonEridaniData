#!/usr/bin/env python3
"""Register A/B pairs from run records that already coexist.

Two runs of the same (pr, head_sha, rubric) with matching prompt_policy but a different model,
rubrics version, or arm are an A/B comparison. This scans the run records and writes one
`eval/pairs/<pair_id>.json` per such pair (canonical, order-free id — re-running is a no-op).
It pairs whatever is present: the model-vs-model collisions already in the history today, and
any shadow arm added later (e.g. a deepseek backfill) the next time it runs.

    python3 scripts/make_pairs.py                 # all qualifying pairs
    python3 scripts/make_pairs.py --require-exact # only fidelity:exact runs (strict cohort)
"""
import argparse
import datetime
import hashlib
import itertools
import json
import pathlib

import tcdata


def load_runs():
    for p in sorted((tcdata.ROOT / "records" / "runs").rglob("*.json")):
        yield json.loads(p.read_text())


def latest_per(runs):
    """Keep one run per (pr, head, rubric, model, rubrics_sha, arm): the highest round. Multiple
    rounds of the identical treatment are the same arm observed twice; the latest is canonical."""
    best = {}
    for r in runs:
        key = (r["pr"], r.get("head_sha"), r["rubric"], r.get("model"),
               r.get("rubrics_sha"), r.get("arm"))
        cur = best.get(key)
        if cur is None or (r.get("round") or 0) > (cur.get("round") or 0):
            best[key] = r
    return list(best.values())


def differs(a, b):
    return (a.get("model") != b.get("model")
            or (a.get("rubrics_sha") or "") != (b.get("rubrics_sha") or "")
            or a.get("arm") != b.get("arm"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-exact", action="store_true",
                    help="only pair fidelity:exact runs (exclude backfilled history)")
    ap.add_argument("--by", default="pair-maker-v1")
    a = ap.parse_args()

    runs = [r for r in load_runs() if r.get("verdict") not in (None,)
            and (not a.require_exact or r.get("fidelity") == "exact")]
    counts = {"new": 0, "same": 0, "conflict": 0}

    # Group comparable runs: same task and same prompt policy (a fresh arm and a reactivation
    # arm saw different prompts and must not be compared).
    groups = {}
    for r in latest_per(runs):
        key = (r["pr"], r.get("head_sha"), r["rubric"], r.get("prompt_policy") or "fresh")
        groups.setdefault(key, []).append(r)

    for (pr, head, rubric, policy), members in groups.items():
        for ra, rb in itertools.combinations(members, 2):
            if not differs(ra, rb):
                continue
            a_run, b_run = sorted([ra, rb], key=lambda r: r["run_id"])  # canonical: smaller id = a
            pair_id = hashlib.sha256(
                f"pair-v1|{pr}|{head}|{rubric}|{a_run['run_id']}|{b_run['run_id']}"
                .encode()).hexdigest()[:16]
            rec = {
                "schema": "tauceti.pair/v1", "pair_id": pair_id,
                "pr": pr, "head_sha": head, "rubric": rubric, "prompt_policy": policy,
                "arms": {
                    "a": {k: a_run.get(k) for k in
                          ("run_id", "provider", "model", "rubrics_sha", "arm", "verdict")},
                    "b": {k: b_run.get(k) for k in
                          ("run_id", "provider", "model", "rubrics_sha", "arm", "verdict")},
                },
                "diff_blob": a_run.get("diff_blob") or b_run.get("diff_blob"),
                "created_by": a.by,
            }
            counts[tcdata.write_record(f"eval/pairs/{pair_id}.json",
                                       {k: v for k, v in rec.items() if v is not None})] += 1

    print(f"make_pairs: new={counts['new']} unchanged={counts['same']} "
          f"conflicts={counts['conflict']}")


if __name__ == "__main__":
    main()
