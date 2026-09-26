#!/usr/bin/env python3
"""Plan (and cost) filling the missing arm of A/B pairs with another reviewer.

We have a production review for most (pr, head_sha, rubric) tasks. To A/B-test a different
agent — say deepseek — against them, we need that agent's review of the SAME tasks. This tool
finds the tasks lacking the target provider's arm and either estimates what running them would
cost, or emits the `epsiloneridani-review --shadow` commands that produce them (which archive back to
this repo and pair up automatically via make_pairs.py).

    python3 scripts/ab_fill.py --target deepseek                  # cost estimate (default)
    python3 scripts/ab_fill.py --target deepseek --commands       # emit shadow-review commands
    python3 scripts/ab_fill.py --target deepseek --commands --limit 20 > fill.sh

The estimate is anchored, when possible, on the target's OWN historical runs: agentic token
usage is harness-specific (deepseek reads less context but answers longer than gpt-5.5), so we
calibrate the recorded token counts by the target-vs-other ratio observed on tasks both have
reviewed, then price at the target's rate. The naive proxy (recorded tokens priced at the
target rate, no calibration) is shown as an upper bound.
"""
import argparse
import collections
import json
import pathlib

import tcdata

# (input, output) USD per 1M tokens — mirrors EpsilonEridaniReview runner/review.py PRICES.
PRICES = {"claude-opus-4-8": (15.0, 75.0), "claude-sonnet-4-6": (3.0, 15.0),
          "gpt-5.5": (1.25, 10.0),
          "deepseek/deepseek-v4-pro": (0.435, 0.87), "minimax/minimax-m3": (0.60, 2.40)}
PROVIDER_MODEL = {"deepseek": "deepseek/deepseek-v4-pro", "minimax": "minimax/minimax-m3",
                  "claude": "claude-opus-4-8", "codex": "gpt-5.5"}


def load_runs():
    for p in sorted((tcdata.ROOT / "records" / "runs").rglob("*.json")):
        yield json.loads(p.read_text())


def toks(r):
    u = r.get("usage") or {}
    return (u.get("input_tokens") or 0), (u.get("output_tokens") or 0)


def latest_production(runs):
    """One representative production run per (pr, head, rubric): the latest round."""
    best = {}
    for r in runs:
        if r.get("arm") not in (None, "production"):
            continue
        key = (r["pr"], r.get("head_sha"), r["rubric"])
        cur = best.get(key)
        if cur is None or (r.get("round") or 0) > (cur.get("round") or 0):
            best[key] = r
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="deepseek", help="provider to fill the missing arm with")
    ap.add_argument("--commands", action="store_true",
                    help="emit shadow-review commands instead of an estimate")
    ap.add_argument("--label", default="", help="shadow label (default: <target>-backfill)")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of PRs in command mode")
    ap.add_argument("--repo", default="EpsilonEridaniProject/EpsilonEridani")
    a = ap.parse_args()
    target_model = PROVIDER_MODEL.get(a.target, a.target)
    p_in, p_out = PRICES.get(target_model, (1.0, 1.0))
    label = a.label or f"{a.target}-backfill"

    runs = list(load_runs())
    tasks = latest_production(runs)

    # Tasks (pr, head, rubric) that already have ANY run by the target provider — skip those.
    have_target = {(r["pr"], r.get("head_sha"), r["rubric"])
                   for r in runs if r.get("provider") == a.target}
    todo = {k: r for k, r in tasks.items() if k not in have_target}

    # Calibration: on tasks reviewed by BOTH the target and someone else, how do the target's
    # tokens compare to the other reviewer's? Aggregate ratios, weighted by token volume.
    by_task = collections.defaultdict(dict)
    for r in runs:
        by_task[(r["pr"], r.get("head_sha"), r["rubric"])][r.get("provider")] = r
    cal_in_t = cal_in_o = cal_out_t = cal_out_o = 0
    cal_n = 0
    for providers in by_task.values():
        tr = providers.get(a.target)
        others = [v for k, v in providers.items() if k != a.target]
        if not tr or not others:
            continue
        o = others[0]
        ti, to = toks(tr); oi, oo = toks(o)
        if oi and oo:
            cal_in_t += ti; cal_in_o += oi; cal_out_t += to; cal_out_o += oo; cal_n += 1
    in_ratio = (cal_in_t / cal_in_o) if cal_in_o else None
    out_ratio = (cal_out_t / cal_out_o) if cal_out_o else None

    if a.commands:
        # One shadow run per PR covers all its todo rubrics at its latest reviewed head; the
        # rubric is exactly the diff `gh pr diff` returns, and --expect-head guards a moved head.
        per_pr = collections.defaultdict(lambda: {"rubrics": set(), "head": None})
        for (pr, head, rubric) in todo:
            per_pr[pr]["rubrics"].add(rubric)
            per_pr[pr]["head"] = head
        items = sorted(per_pr.items())
        if a.limit:
            items = items[:a.limit]
        print("#!/usr/bin/env bash")
        print("# Fill the {} arm. Needs `pi` on PATH and OPENROUTER_API_KEY exported.".format(
            a.target))
        print(f"# {len(items)} PRs, {sum(len(v['rubrics']) for _, v in items)} rubric-arms.")
        print("set -e")
        for pr, v in items:
            rubrics = ",".join(sorted(v["rubrics"]))
            print(f"epsiloneridani-review {pr} --repo {a.repo} --shadow --label {label} "
                  f"--reviewer {a.target} --expect-head {v['head'][:12]} --rubrics {rubrics}")
        return

    # Estimate
    naive_in = sum(toks(r)[0] for r in todo.values())
    naive_out = sum(toks(r)[1] for r in todo.values())
    naive_cost = naive_in * p_in / 1e6 + naive_out * p_out / 1e6

    print(f"Target: {a.target} ({target_model}) @ ${p_in}/1M in, ${p_out}/1M out")
    print(f"Tasks to fill (distinct pr×head×rubric, no {a.target} arm yet): {len(todo)}")
    print(f"  across {len({k[0] for k in todo})} PRs")
    print()
    print(f"Recorded tokens on those tasks (the original reviewer's): "
          f"{naive_in/1e6:.1f}M in / {naive_out/1e6:.1f}M out")
    print(f"  Naive proxy (recorded tokens @ {a.target} price, no calibration): "
          f"${naive_cost:.2f}  [upper bound]")
    print()
    if in_ratio and out_ratio:
        cal_in = naive_in * in_ratio
        cal_out = naive_out * out_ratio
        cal_cost = cal_in * p_in / 1e6 + cal_out * p_out / 1e6
        print(f"Calibration from {cal_n} task(s) both {a.target} and another agent reviewed:")
        print(f"  {a.target} uses {in_ratio:.2f}x the input and {out_ratio:.2f}x the output "
              f"tokens of the other agent")
        print(f"  Calibrated estimate: {cal_in/1e6:.1f}M in / {cal_out/1e6:.1f}M out "
              f"-> ${cal_cost:.2f}")
        if cal_n < 10:
            print(f"  (!) calibration rests on only {cal_n} task(s); run "
                  f"`--commands --limit 25 | bash` for a tighter sample before trusting it")
    else:
        print(f"No existing {a.target} runs to calibrate against; trust the naive proxy only "
              f"loosely (tokenizer + agent-harness differences can move it 2x either way).")
        print(f"Run `ab_fill.py --target {a.target} --commands --limit 25` and execute a sample "
              f"to calibrate.")


if __name__ == "__main__":
    main()
