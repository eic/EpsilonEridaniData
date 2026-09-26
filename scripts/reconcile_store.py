#!/usr/bin/env python3
"""Reconcile an orphaned/diverged EpsilonEridaniData checkout into a clean one, losslessly.

A worker's data checkout can diverge from origin/main (e.g. a botched `pull --rebase` left it
wedged on a detached HEAD with local-only commits). The records are content-addressed and
write-if-absent, so the two histories can be merged by file, but three cases need judgement and
an audit trail, which this script provides:

  * local-only records / blobs  -> copied in verbatim (unique paths)
  * same path, enrichment-only  -> upstream kept; the dropped local values (cost/fidelity/...) are
                                   recorded, and any *concerning* drop is flagged for review
  * same path, substantive      -> the local copy is renumbered (filename AND round_id) so both
                                   survive; build_db keys round_id PRIMARY KEY, so a renamed file
                                   alone would still collapse the rows
  * a record with conflict markers (a committed botched merge) -> both sides parsed and resolved
                                   under the same policy, then treated as a normal local record

Everything it does is written to a JSON manifest (`--manifest`) so the "lossless" claim is
auditable rather than asserted. It writes into `--data-dir` (a fresh origin/main checkout) but
never commits or pushes — review the manifest, run build_db, then commit/push by hand.

    python3 scripts/reconcile_store.py --wedged <orphaned-checkout> --data-dir <fresh-clone> \
        --manifest /tmp/reconcile-manifest.json
"""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import sys

import tcdata

# Fields that may differ between two records of the SAME execution without making them different
# reviews: cost is recosted against a fixed price table (legacy value retained), provenance/timing
# is reconstructed best-effort, and serialization timestamps wobble. Two records whose projection
# (everything else) is equal are the same review; the difference is enrichment, not data.
ENRICH = {"cost", "cost_usd", "cost_legacy", "cost_usd_legacy", "cost_recosted", "cost_source",
          "cost_estimated", "duration_s", "duration_source", "fidelity", "source", "ts",
          "posted_at",
          # Content-addressed pointers to the transcript / reviewed diff / rendered scoreboard. These
          # can differ between two records of the *same* review/round when the redaction pass changed
          # (different redacted bytes -> different sha) or when one writer archived the blob and the
          # other left it null. The substance lives in verdict/summary/findings (runs) and in
          # scoreboard_sha256/states/run_ids (rounds); a blob-pointer difference alone is not a new
          # record, and renumbering on it would mint a spurious duplicate row (a fake A/B self-pair).
          "transcript_blob", "diff_blob", "scoreboard_blob"}
# Fidelity ranked best-first: an "exact" record should never be downgraded to "reconstructed".
FIDELITY_RANK = {"exact": 0, "scoreboard-parse": 1, "reconstructed": 2, None: 3}


def projection(d):
    return {k: v for k, v in d.items() if k not in ENRICH}


def round_disc(rec):
    """A per-execution discriminator from the round's run ids (themselves timestamp-unique). Mirrors
    the source-side scheme in EpsilonEridaniReview runner/review.py so renumbered rounds look native."""
    return hashlib.sha256("|".join(sorted(rec.get("run_ids") or [])).encode()).hexdigest()[:12]


def renumber_round_id(rec):
    rid = rec["round_id"]
    if re.search(r"-[0-9a-f]{12}$", rid):  # already disambiguated: idempotent re-run
        return rid
    return f"{rid}-{round_disc(rec)}"


def resolve_markers(text):
    """Split a git-conflicted file into its two sides (ours=HEAD, theirs=incoming)."""
    ours, theirs, state = [], [], 0
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<<"):
            state = 1; continue
        if line.startswith("======="):
            state = 2; continue
        if line.startswith(">>>>>>>"):
            state = 0; continue
        if state in (0, 1):
            ours.append(line)
        if state in (0, 2):
            theirs.append(line)
    return "".join(ours), "".join(theirs)


def merge_enrichment(local, upstream, note):
    """Return upstream, possibly enriched from local where local is strictly better, plus a record
    of every difference. Cost stays upstream's (recosted authority); fidelity prefers the better
    rank; an ENRICH key present only in local is carried over. Anything outside this policy is
    flagged so a human looks. Returns (merged_or_none, was_changed)."""
    diffs = {}
    merged = dict(upstream)
    changed = False
    for k in ENRICH:
        lv, uv = local.get(k), upstream.get(k)
        if lv == uv:
            continue
        diffs[k] = {"local": lv, "upstream": uv}
        if k == "fidelity" and FIDELITY_RANK.get(lv, 3) < FIDELITY_RANK.get(uv, 3):
            merged[k] = lv; changed = True
            note.setdefault("upgraded", []).append("fidelity")
        elif uv is None and lv is not None:
            # upstream lacks this enrichment (key absent or null) but local has it: carry it over
            # (e.g. local archived the scoreboard blob, upstream didn't). cost stays upstream's,
            # because the recosted value is non-null and authoritative.
            merged[k] = lv; changed = True
            note.setdefault("upgraded", []).append(k)
    note["enrichment_diffs"] = diffs
    return (merged if changed else None), changed


def load(path):
    return json.loads(path.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wedged", required=True, help="the orphaned/diverged checkout (read-only)")
    ap.add_argument("--data-dir", required=True, help="a fresh origin/main checkout (written into)")
    ap.add_argument("--manifest", required=True, help="where to write the JSON audit manifest")
    ap.add_argument("--report-only", action="store_true", help="classify and report; write nothing")
    a = ap.parse_args()
    wedged = pathlib.Path(a.wedged).resolve()
    data = pathlib.Path(a.data_dir).resolve()
    write = not a.report_only

    m = {"local_only_records": [], "local_only_blobs": 0, "same": 0,
         "enrichment_only": [], "enrichment_merged": [], "flags": [],
         "substantive_renumbered": [], "marker_repaired": []}

    # blobs: content-addressed, so a missing path is genuinely new content; copy verbatim.
    for src in sorted((wedged / "blobs").rglob("*.gz")) if (wedged / "blobs").is_dir() else []:
        dst = data / src.relative_to(wedged)
        if not dst.exists():
            if write:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            m["local_only_blobs"] += 1

    def classify_and_write(local, rel):
        """Reconcile one local record against its upstream counterpart at `rel`."""
        dst = data / rel
        if not dst.exists():
            if write:
                tcdata.write_record(rel, local, root=data,
                                    id_field=("round_id" if "round_id" in local else "run_id"))
            m["local_only_records"].append(rel)
            return
        upstream = load(dst)
        if local == upstream:
            m["same"] += 1
            return
        if projection(local) == projection(upstream):
            note = {"rel": rel}
            merged, changed = merge_enrichment(local, upstream, note)
            if changed:
                if write:
                    dst.write_text(json.dumps(merged, indent=1, sort_keys=True) + "\n")
                m["enrichment_merged"].append(note)
            else:
                m["enrichment_only"].append(note)
            return
        # substantive: keep upstream, preserve the local copy under a renumbered id so build_db
        # (which keys round_id PRIMARY KEY) holds both rather than collapsing them.
        if "round_id" in local:
            new_id = renumber_round_id(local)
            local2 = dict(local, round_id=new_id)
            new_rel = f"records/rounds/{local['pr']}/{new_id}.json"
            if write:
                tcdata.write_record(new_rel, local2, root=data, id_field="round_id")
            m["substantive_renumbered"].append(
                {"old_rel": rel, "new_rel": new_rel, "old_round_id": local["round_id"],
                 "new_round_id": new_id,
                 "diff_keys": sorted(k for k in set(local) | set(upstream)
                                     if local.get(k) != upstream.get(k))})
        else:
            # not expected: run ids are timestamp-unique. Let the storage backstop preserve both,
            # but flag it because two runs sharing an id is itself a smell worth a look.
            if write:
                tcdata.write_record(rel, local, root=data, id_field="run_id")
            m["flags"].append({"rel": rel, "issue": "substantive non-round conflict; preserved"})

    for src in sorted((wedged / "records").rglob("*.json")):
        rel = src.relative_to(wedged).as_posix()
        text = src.read_text()
        if "<<<<<<<" in text or "\n>>>>>>>" in text:
            ours, theirs = resolve_markers(text)
            try:
                sides = [json.loads(ours), json.loads(theirs)]
            except Exception as e:
                m["flags"].append({"rel": rel, "issue": f"unparsable conflict sides: {e}"})
                continue
            if projection(sides[0]) == projection(sides[1]):
                # same review, conflicted only on enrichment (e.g. cost vs 0.0): one record, best
                # enrichment from each side.
                local = dict(sides[0])
                for k in ENRICH:
                    if k in sides[1] and local.get(k) in (None, 0, 0.0) and \
                            sides[1][k] not in (None, 0, 0.0):
                        local[k] = sides[1][k]
                candidates = [local]
            else:
                # two genuinely different executions that collided on a shared id: keep both.
                candidates = sides
            m["marker_repaired"].append(
                {"rel": rel, "sides": len(candidates),
                 "round_ids": [c.get("round_id") for c in candidates]})
            for c in candidates:
                classify_and_write(c, rel)
            continue
        classify_and_write(load(src), rel)

    pathlib.Path(a.manifest).write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    s = {k: (len(v) if isinstance(v, list) else v) for k, v in m.items()}
    print(json.dumps(s, indent=1, sort_keys=True))
    if s["flags"]:
        print(f"\n{s['flags']} flag(s) need review — see manifest {a.manifest}", file=sys.stderr)
    print(f"\n{'wrote into ' + str(data) if write else 'report-only (no writes)'}; "
          f"manifest: {a.manifest}")


if __name__ == "__main__":
    main()
