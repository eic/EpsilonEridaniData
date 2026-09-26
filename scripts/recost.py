#!/usr/bin/env python3
"""Re-cost ESTIMATED run records cache-aware, from token counts and the canonical prices.

Only codex/gpt-5.5 reviews carry an *estimated* cost (cost_estimated == true): in
subscription mode the CLI reports no billed cost, so review.py estimated it from a price
table — and that estimate (a) used three stale rates (corrected in EpsilonEridaniReview #50) and
(b) charged ALL input at full rate, ignoring that most of it is prompt-cache reads (billed
at ~10%). This recomputes those costs as
    (input - cached_input)*input + cached_input*cache_read + output*output
using schema/prices.json, preserving the original in cost_usd_legacy.

claude costs are the CLI's real, cache-accurate total_cost_usd (cost_estimated absent), and
deepseek/pi costs are provider-reported (cost_estimated == false) — both are left untouched.
Round `cost` is re-summed from member runs. Self-healing + idempotent: a record that should
NOT be recosted but was touched by an earlier run is restored from its legacy value.

    python3 scripts/recost.py
"""
import json
import os
import pathlib
import subprocess

import tcdata

# Single source of truth: EpsilonEridaniProject/EpsilonEridaniReview runner/prices.json. We never keep our own
# copy — fetch the canonical file from GitHub (TAUCETI_PRICES path override, else a local engine
# checkout, can stand in offline).
_PRICES_PATH = "runner/prices.json"


def _load_prices_text():
    override = os.environ.get("TAUCETI_PRICES")
    if override and pathlib.Path(override).is_file():
        return pathlib.Path(override).read_text()
    try:
        return subprocess.run(
            ["gh", "api", "repos/EpsilonEridaniProject/EpsilonEridaniReview/contents/" + _PRICES_PATH,
             "-H", "Accept: application/vnd.github.raw"],
            check=True, capture_output=True, text=True).stdout
    except Exception:
        for p in (os.path.expanduser("~/.cache/epsiloneridani-review/EpsilonEridaniReview/" + _PRICES_PATH),
                  "/tmp/EpsilonEridaniReview/" + _PRICES_PATH):
            if pathlib.Path(p).is_file():
                return pathlib.Path(p).read_text()
        raise


PRICES = {k: v for k, v in json.loads(_load_prices_text()).items() if not k.startswith("_")}


def corrected(d):
    """Cache-aware corrected cost for an estimated run, or None if it must not be recosted."""
    if d.get("cost_estimated") is not True:        # real cost (claude CLI / deepseek pi)
        return None
    p = PRICES.get(d.get("model"))
    if not p:
        return None
    u = d.get("usage") or {}
    inp, out = u.get("input_tokens", 0), u.get("output_tokens", 0)
    cached = u.get("cached_input_tokens", 0)        # codex: cache-read subset of input
    non_cached = max(inp - cached, 0)
    cost = (non_cached * p["input"] + cached * p.get("cache_read", p["input"])
            + out * p["output"]) / 1e6
    return round(cost, 6)


def restore(d, cur, legacy):
    """Undo a prior (wrong) recost: move legacy back to cur, drop markers. Returns changed?"""
    if legacy in d:
        d[cur] = d.pop(legacy)
        d.pop("cost_recosted", None)
        return True
    return False


def main():
    new_cost, runs_changed, runs_reverted = {}, 0, 0
    for p in sorted((tcdata.ROOT / "records" / "runs").rglob("*.json")):
        d = json.loads(p.read_text())
        target = corrected(d)
        if target is not None:
            if "cost_usd_legacy" not in d:
                d["cost_usd_legacy"] = d.get("cost_usd")
            if d.get("cost_usd") != target or not d.get("cost_recosted"):
                d["cost_usd"], d["cost_recosted"] = target, True
                p.write_text(json.dumps(d, indent=2) + "\n"); runs_changed += 1
            new_cost[d["run_id"]] = target
        else:
            if restore(d, "cost_usd", "cost_usd_legacy"):
                p.write_text(json.dumps(d, indent=2) + "\n"); runs_reverted += 1
            new_cost[d["run_id"]] = d.get("cost_usd") or 0

    rounds_changed, rounds_reverted = 0, 0
    for p in sorted((tcdata.ROOT / "records" / "rounds").rglob("*.json")):
        d = json.loads(p.read_text())
        ids = d.get("run_ids") or []
        orig = d.get("cost_legacy") if "cost_legacy" in d else d.get("cost")
        if not ids or not all(i in new_cost for i in ids):
            if restore(d, "cost", "cost_legacy"):
                p.write_text(json.dumps(d, indent=2) + "\n"); rounds_reverted += 1
            continue
        total = round(sum(new_cost[i] for i in ids), 6)
        if abs(total - (orig or 0)) < 1e-9:          # corrected == original: keep clean
            if restore(d, "cost", "cost_legacy"):
                p.write_text(json.dumps(d, indent=2) + "\n"); rounds_reverted += 1
        else:
            if "cost_legacy" not in d:
                d["cost_legacy"] = d.get("cost")
            if d.get("cost") != total or not d.get("cost_recosted"):
                d["cost"], d["cost_recosted"] = total, True
                p.write_text(json.dumps(d, indent=2) + "\n"); rounds_changed += 1

    print(f"recost: runs {runs_changed} recosted / {runs_reverted} reverted; "
          f"rounds {rounds_changed} recosted / {rounds_reverted} reverted")


if __name__ == "__main__":
    main()
