#!/usr/bin/env python3
"""Human meta-reviewer: judge which of two anonymized reviews leads to the better PR.

Shows the diff + two reviews (model identity hidden, left/right randomized) and asks the SAME
question the AI judges answer (pairwise-judge-v2). Records the labeller's verdict, free-text note,
and `gh` identity to eval/decisions/<id>.json, committed + pushed (durable; one decision per
(pair, labeller)). The AI verdicts are NEVER shown — they're used only to stratify which pairs to
present (a balanced mix of AI-consensus, AI-split, and AI-unstable pairs), so human labels land
where they're most informative for calibration.

    gh auth status            # be logged in
    python3 scripts/label.py              # label until you quit
    python3 scripts/label.py --rubric correctness
    python3 scripts/label.py --models minimax,deepseek   # focus one matchup

Part of the eval pipeline (make_pairs -> judge -> label -> calibration/power). Keep the README's
"Labelling and analysis" section in sync when these scripts' flags or behaviour change.
"""
import argparse
import collections
import datetime
import gzip
import hashlib
import json
import random
import subprocess

import tcdata

G, R, DIM, BOLD, RESET, CYAN = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m", "\033[36m"


def gh_login():
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("not logged in to gh — run `gh auth login` first")
    return r.stdout.strip()


def blob_text(sha):
    return gzip.decompress((tcdata.ROOT / "blobs" / sha[:2] / (sha + ".gz")).read_bytes()).decode("utf-8", "replace")


def load_run(rid, pr):
    return json.loads((tcdata.ROOT / "records" / "runs" / str(pr) / (rid + ".json")).read_text())


def render_diff(text, cap=300):
    out, lines = [], text.splitlines()
    for ln in lines[:cap]:
        c = G if ln.startswith("+") else R if ln.startswith("-") else CYAN if ln.startswith("@@") else DIM
        out.append(c + ln + RESET)
    if len(lines) > cap:
        out.append(f"{DIM}... [{len(lines)-cap} more diff lines truncated] ...{RESET}")
    return "\n".join(out)


def render_review(run):
    out = [f"{BOLD}verdict:{RESET} {run.get('verdict')}", f"{BOLD}summary:{RESET} {run.get('summary') or '(none)'}",
           f"{BOLD}findings:{RESET}"]
    fs = run.get("findings") or []
    if not fs:
        out.append("  (none)")
    for f in fs:
        out.append(f"  • {f.get('file','')}:{f.get('line','')} — {f.get('issue','')}")
        if f.get("fix"):
            out.append(f"      {DIM}fix: {f.get('fix')}{RESET}")
    return "\n".join(out)


def panel_consensus():
    """Per-pair AI stratum from the grok+sonnet panel's order-stable verdicts: consensus|split|
    unstable|none. Uses the LATEST prompt version each (pair, judge) was scored under, so pairs
    judged only with v3 (e.g. a freshly-added topic) stratify just like the v2-judged history."""
    recs = [json.loads(p.read_text()) for p in (tcdata.ROOT / "eval" / "judgments").glob("*.json")]

    def ver(r):
        pf = r["judge"]["prompt_file"]
        return 3 if "v3" in pf else 2 if "v2" in pf else 1

    best = collections.defaultdict(dict)  # pair_id -> judge -> best prompt version seen
    for r in recs:
        if r["judge"]["spec"] in ("grok", "sonnet"):
            j = r["judge"]["spec"]
            best[r["pair_id"]][j] = max(best[r["pair_id"]].get(j, 0), ver(r))

    byjp = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(list)))
    for r in recs:
        j = r["judge"]["spec"]
        if j in ("grok", "sonnet") and ver(r) == best[r["pair_id"]].get(j):
            byjp[r["pair_id"]][j][r["order"]].append(r["winner_arm"])

    def stable(po):
        def modal(ws):
            return collections.Counter(ws).most_common(1)[0][0] if ws else None
        a, b = modal(po.get("ab", [])), modal(po.get("ba", []))
        return a if (a is not None and a == b) else None

    out = {}
    for pid, judges in byjp.items():
        verdicts = {j: stable(po) for j, po in judges.items()}
        present = {j: v for j, v in verdicts.items() if v is not None}
        if len(judges) < 2 or len(present) < 2:
            out[pid] = "unstable"
        elif len(set(present.values())) == 1:
            out[pid] = "consensus"
        else:
            out[pid] = "split"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rubric", default="")
    ap.add_argument("--pr", default="", help="comma-separated PR numbers to restrict to")
    ap.add_argument("--models", default="",
                    help="comma-separated arm tokens; keep only pairs matching this matchup "
                         "(e.g. --models minimax,deepseek)")
    ap.add_argument("--no-push", action="store_true", help="commit locally but don't push each decision")
    a = ap.parse_args()
    me = gh_login()
    print(f"{BOLD}labelling as {me}{RESET}")
    print(f"{DIM}Your correctness bar sets the ceiling: the more carefully you check each finding —")
    print("is it actually true? would acting on it really make the PR better? — the better the")
    print(f"automatic judge we can calibrate against you. Don't just trust the claims.{RESET}\n")

    pairs = {json.loads(p.read_text())["pair_id"]: json.loads(p.read_text())
             for p in (tcdata.ROOT / "eval" / "pairs").glob("*.json")}
    prset = {int(x) for x in a.pr.split(",") if x.strip()} if a.pr else None
    models = [m for m in a.models.split(",") if m.strip()]
    pairs = {k: v for k, v in pairs.items()
             if (not a.rubric or v["rubric"] == a.rubric)
             and (prset is None or v["pr"] in prset)
             and tcdata.pair_matches_models(v, models) and v.get("diff_blob")
             and (tcdata.ROOT / "blobs" / v["diff_blob"][:2] / (v["diff_blob"] + ".gz")).exists()}

    def informative(p):  # drop forced ties and pairs with an errored/non-review arm
        try:
            return tcdata.is_informative(load_run(p["arms"]["a"]["run_id"], p["pr"]),
                                         load_run(p["arms"]["b"]["run_id"], p["pr"]))
        except Exception:
            return False
    pairs = {k: v for k, v in pairs.items() if informative(v)}

    done = {json.loads(p.read_text())["pair_id"]
            for p in (tcdata.ROOT / "eval" / "decisions").glob("*.json")
            if json.loads(p.read_text()).get("labeller") == me}
    strat = panel_consensus()

    # Queue: round-robin ACROSS PRs (so topics interleave — a deck-heavy history can't crowd out a
    # freshly-added area), and WITHIN each PR order by how informative the pair is for calibration
    # (AI-split and AI-unstable pairs first, settled-consensus last), shuffling within a stratum.
    rank = {"split": 0, "unstable": 1, "consensus": 2, "none": 3}
    by_pr = collections.defaultdict(list)
    for pid, pr in pairs.items():
        if pid not in done:
            by_pr[pr["pr"]].append(pid)
    for pids in by_pr.values():
        random.shuffle(pids)
        pids.sort(key=lambda p: rank.get(strat.get(p, "none"), 3))
    prs = list(by_pr)
    random.shuffle(prs)
    queue = []
    while any(by_pr[p] for p in prs):
        for p in prs:
            if by_pr[p]:
                queue.append(by_pr[p].pop(0))
    if not queue:
        print("nothing left to label (for you). thanks!"); return
    print(f"{len(queue)} pairs to label (already done: {len(done)}). [1]/[2]=better PR  [t]=tie  [s]=skip  [q]=quit\n")

    (tcdata.ROOT / "eval" / "decisions").mkdir(parents=True, exist_ok=True)
    n = 0
    for pid in queue:
        pair = pairs[pid]
        ra = load_run(pair["arms"]["a"]["run_id"], pair["pr"])
        rb = load_run(pair["arms"]["b"]["run_id"], pair["pr"])
        first_arm = random.choice(["a", "b"])           # randomize presentation; hidden mapping
        r1, r2 = (ra, rb) if first_arm == "a" else (rb, ra)
        print("=" * 90)
        print(f"{BOLD}PR #{pair['pr']} · rubric: {pair['rubric']}{RESET}   "
              f"({CYAN}https://github.com/EpsilonEridaniProject/EpsilonEridani/pull/{pair['pr']}/files{RESET})\n")
        print(render_diff(blob_text(pair["diff_blob"])))
        print(f"\n{BOLD}{'-'*40} REVIEW 1 {'-'*40}{RESET}\n" + render_review(r1))
        print(f"\n{BOLD}{'-'*40} REVIEW 2 {'-'*40}{RESET}\n" + render_review(r2))
        print(f"\n{BOLD}If the author acted on each, which leads to the better PR?{RESET}")
        t0 = datetime.datetime.now(datetime.timezone.utc)
        choice = input("  [1/2/t/s/q] > ").strip().lower()
        if choice == "q":
            break
        if choice == "s" or choice not in ("1", "2", "t"):
            print("  (skipped)\n"); continue
        note = input("  note (optional): ").strip()
        winner = "tie" if choice == "t" else (first_arm if choice == "1" else ("b" if first_arm == "a" else "a"))
        did = "d-" + hashlib.sha256(f"{pid}|{me}".encode()).hexdigest()[:16]
        rec = {"schema": "epsiloneridani.decision/v1", "decision_id": did, "labeller": me,
               "pair_id": pid, "pr": pair["pr"], "rubric": pair["rubric"],
               "winner_arm": winner, "raw_choice": choice, "presented_first_arm": first_arm,
               "note": note or None,
               "duration_s": round((datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds(), 1),
               "ts": t0.isoformat(), "diff_blob": pair["diff_blob"],
               "arms": {"a": pair["arms"]["a"]["run_id"], "b": pair["arms"]["b"]["run_id"]}}
        path = tcdata.ROOT / "eval" / "decisions" / (did + ".json")
        path.write_text(json.dumps(rec, indent=2) + "\n")
        n += 1
        root = str(tcdata.ROOT)
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", root, "-c", f"user.name={me}", "commit", "-q", "-m",
                        f"label: {me} on {pid[:10]} ({pair['rubric']})"], capture_output=True)
        if not a.no_push:
            subprocess.run(["git", "-C", root, "pull", "-q", "--rebase", "origin", "main"], capture_output=True)
            subprocess.run(["git", "-C", root, "push", "-q", "origin", "main"], capture_output=True)
        print(f"  recorded ({winner}). {n} this session.\n")
    print(f"\nthanks — {n} decisions recorded.")


if __name__ == "__main__":
    main()
