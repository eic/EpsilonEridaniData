#!/usr/bin/env python3
"""Backfill run/round records from a EpsilonEridaniReview store (ledger.json + reviews/<pr>/<round>/).

The store is the richest historical source: per-rubric artifacts carry the full reviewer text,
usage, and cost, and the rendered scoreboard of every round is on disk — none of which is
recoverable from GitHub. Two stores exist; ingest both:

    python3 scripts/ingest_ledger.py --store ~/.cache/epsiloneridani-review/store/EpsilonEridaniProject__EpsilonEridani
    python3 scripts/ingest_ledger.py --reviews-branch       # CI's store (the EpsilonEridaniReview branch)

Backfilled records are flagged `fidelity: reconstructed`: the runner did not record base SHAs,
per-run timing, or prompt hashes historically, so `base_ref_oid` is recovered best-effort from a
bulk `gh pr list` (the base tip as GitHub reports it now) and `started_at` is the round
timestamp. Strict A/B cohorts should require `fidelity: exact`.

Idempotent: run ids are deterministic functions of the source data, records are write-if-absent,
and a re-run reports `new=0`. Records are public — fields are allowlisted (no session ids, no
raw stderr) and transcripts pass the redaction pass.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

import tcdata

REPO = "EpsilonEridaniProject/EpsilonEridani"
REVIEW_REPO = "EpsilonEridaniProject/EpsilonEridaniReview"


def base_oids(repo):
    """Bulk best-effort base_ref_oid per PR number. Empty on any failure."""
    r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "all",
                        "--limit", "500", "--json", "number,baseRefOid"],
                       text=True, capture_output=True)
    if r.returncode != 0:
        print(f"note: gh pr list failed ({r.stderr.strip()[:200]}); base_ref_oid stays null",
              file=sys.stderr)
        return {}
    return {p["number"]: p.get("baseRefOid") for p in json.loads(r.stdout)}


def iso_compact(ts):
    return (ts[:19].replace("-", "").replace(":", "") + "Z") if ts else ""


def ingest_store(store, source, repo, bases, counts):
    ledger = json.loads((store / "ledger.json").read_text())
    for pr, pr_state in sorted(ledger.get("prs", {}).items(), key=lambda kv: int(kv[0])):
        seen_rubrics = set()  # rubrics run in an earlier round => reactivation prompt
        for rnd in pr_state.get("rounds", []):
            num, head = rnd.get("round"), rnd.get("head_sha") or ""
            ts = rnd.get("ts") or ""
            started_at = ts[:19] + "Z" if ts else None
            rdir = store / "reviews" / str(pr) / str(num)
            run_ids = []
            for rubric in rnd.get("ran", []):
                art_path = rdir / f"{rubric}.json"
                art = json.loads(art_path.read_text()) if art_path.exists() else {}
                model = art.get("model") or ""
                vo = art.get("verdict_obj") or {}
                rid = hashlib.sha256("|".join(
                    [repo, str(pr), head, rubric, model, rnd.get("rubrics_version") or "",
                     started_at or ""]).encode()).hexdigest()[:6]
                run_id = f"r-{iso_compact(ts)}-{pr}-{rubric}-{rid}"
                run_ids.append(run_id)
                rec = {
                    "schema": "epsiloneridani.run/v1", "run_id": run_id,
                    "dedupe_key": "|".join([repo, str(pr), head, rubric, model,
                                            rnd.get("rubrics_version") or "", "production",
                                            str(num)]),
                    "source": source, "arm": "production",
                    "prompt_policy": "reactivation" if rubric in seen_rubrics else "fresh",
                    "repo": repo, "pr": int(pr), "round": num, "head_sha": head,
                    "base_ref_oid": bases.get(int(pr)),
                    "rubric": rubric, "rubrics_repo": REVIEW_REPO,
                    "rubrics_version": rnd.get("rubrics_version"),
                    "provider": art.get("provider"), "model": model or None,
                    "mode": rnd.get("mode"), "started_at": started_at,
                    "attempts": [{k: art[k] for k in ("returncode", "cost_usd",
                                                      "cost_estimated", "usage")
                                  if art.get(k) is not None}] if art else None,
                    "usage": art.get("usage"), "cost_usd": art.get("cost_usd"),
                    "cost_estimated": art.get("cost_estimated"),
                    "verdict": vo.get("verdict") or "error",
                    "confidence": vo.get("confidence"), "summary": vo.get("summary"),
                    "findings": vo.get("findings") or [],
                    "degraded": (not art) or None,  # rubric ran but its artifact is gone
                    "fidelity": "reconstructed",
                }
                if art.get("text"):
                    rec["transcript_blob"] = tcdata.write_blob(art["text"])
                counts[tcdata.write_record(
                    f"records/runs/{pr}/{run_id}.json",
                    {k: v for k, v in rec.items() if v is not None}, id_field="run_id")] += 1
            rrec = {
                "schema": "epsiloneridani.round/v1", "round_id": f"{pr}-{num}",
                "repo": repo, "pr": int(pr), "round": num, "ts": ts or None,
                "mode": rnd.get("mode"), "arm": "production", "source": source,
                "head_sha": head or None, "base_ref_oid": bases.get(int(pr)),
                "rubrics_version": rnd.get("rubrics_version"),
                "ran": rnd.get("ran"), "run_ids": run_ids or None,
                "states": rnd.get("states"), "cost": rnd.get("cost"),
                "halted_at": rnd.get("halted_at"), "fidelity": "reconstructed",
            }
            sb = rdir / "scoreboard.md"
            if sb.exists():
                body = sb.read_text()
                rrec["scoreboard_sha256"] = hashlib.sha256(body.encode()).hexdigest()
                rrec["scoreboard_blob"] = tcdata.write_blob(body)
            # Round numbering is per-store: CI and the local worker each count their own rounds,
            # and early PRs were reviewed from both. The execution history (run records) is keyed
            # by timestamp so it never collides; disambiguate the CI round envelope with a readable
            # `-ci` id rather than the generic hash sibling. Check first so we land directly on the
            # `-ci` path; write_record still backstops any residual collision losslessly.
            rid = f"{pr}-{num}"
            base = tcdata.ROOT / f"records/rounds/{pr}/{rid}.json"
            if (source == "backfill-reviews-branch" and base.exists()
                    and base.read_text() != json.dumps(
                        {k: v for k, v in rrec.items() if v is not None},
                        indent=1, sort_keys=True) + "\n"):
                rid = f"{pr}-{num}-ci"
            rrec["round_id"] = rid
            status = tcdata.write_record(
                f"records/rounds/{pr}/{rid}.json",
                {k: v for k, v in rrec.items() if v is not None}, id_field="round_id")
            counts[status] += 1
            seen_rubrics.update(rnd.get("ran", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="",
                    help="path to a EpsilonEridaniReview store (ledger.json + reviews/)")
    ap.add_argument("--reviews-branch", action="store_true",
                    help="ingest CI's store: a fresh clone of the EpsilonEridaniReview reviews branch")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args()
    if not a.store and not a.reviews_branch:
        ap.error("give --store PATH and/or --reviews-branch")

    bases = base_oids(a.repo)
    print(f"base_ref_oid recovered for {len(bases)} PRs", file=sys.stderr)
    counts = {"new": 0, "same": 0, "preserved": 0}

    if a.store:
        ingest_store(pathlib.Path(a.store).expanduser(), "backfill-ledger", a.repo, bases, counts)
    if a.reviews_branch:
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "reviews",
                                f"https://github.com/{REVIEW_REPO}", td + "/store"],
                               text=True, capture_output=True)
            if r.returncode != 0:
                sys.exit(f"clone of {REVIEW_REPO}@reviews failed: {r.stderr[-300:]}")
            ingest_store(pathlib.Path(td) / "store", "backfill-reviews-branch", a.repo,
                         bases, counts)

    print(f"ingest: new={counts['new']} unchanged={counts['same']} "
          f"preserved={counts['preserved']}")


if __name__ == "__main__":
    main()
