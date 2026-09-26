#!/usr/bin/env python3
"""Rebuild the derived SQLite database from the record files.

The record files in records/ and eval/ are the source of truth; db/tauceti.db is a throwaway
materialization rebuilt from scratch on every invocation (and gitignored). Tables: runs, rounds,
findings (one row per finding), posts, pairs, judgments, resolutions, human_decisions. Views:

  ab_pairs     candidate A/B comparisons — two runs of the same (pr, head_sha, rubric) with
               matching prompt_policy but a different model, rubrics version, or arm
  pr_outcomes  the latest round per PR
  outcomes     one row per registered pair with AI consensus, human decision, and final label
"""
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "db" / "tauceti.db"


def records(subdir):
    for p in sorted((ROOT / subdir).rglob("*.json")):
        text = p.read_text()
        if "<<<<<<<" in text or "\n>>>>>>>" in text:
            # A record with conflict markers is corruption from a botched merge, not data. Fail
            # loudly: silently skipping it (the old behaviour) hid the loss for days.
            raise SystemExit(f"error: {p} contains git conflict markers; repair it before building")
        try:
            yield p, json.loads(text)
        except Exception as e:
            print(f"warning: unreadable record {p}: {e}", file=sys.stderr)


def j(v):
    return json.dumps(v) if v is not None else None


def main():
    DB.parent.mkdir(exist_ok=True)
    DB.unlink(missing_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY, dedupe_key TEXT, source TEXT, arm TEXT, prompt_policy TEXT,
      repo TEXT, pr INTEGER, round INTEGER, head_sha TEXT, base_ref_oid TEXT,
      merge_base_sha TEXT, rubric TEXT, rubrics_repo TEXT, rubrics_sha TEXT,
      rubrics_version TEXT, provider TEXT, model TEXT, mode TEXT, auth TEXT, ci INTEGER,
      prompt_sha256 TEXT, diff_sha256 TEXT, diff_prompt_truncated INTEGER,
      started_at TEXT, duration_s REAL, attempts TEXT, usage TEXT,
      cost_usd REAL, cost_estimated INTEGER, verdict TEXT, confidence TEXT, summary TEXT,
      transcript_blob TEXT, diff_blob TEXT, fidelity TEXT, n_findings INTEGER,
      cost_usd_legacy REAL, cost_recosted INTEGER, prices_sha TEXT
    );
    CREATE TABLE findings (
      run_id TEXT, idx INTEGER, file TEXT, line TEXT, issue TEXT, fix TEXT, evidence TEXT
    );
    CREATE TABLE rounds (
      round_id TEXT PRIMARY KEY, repo TEXT, pr INTEGER, round INTEGER, ts TEXT, mode TEXT,
      arm TEXT, source TEXT, head_sha TEXT, base_ref_oid TEXT, merge_base_sha TEXT,
      rubrics_sha TEXT, rubrics_version TEXT, diff_sha256 TEXT, ran TEXT, run_ids TEXT,
      states TEXT, overall TEXT, cost REAL, halted_at TEXT, fidelity TEXT,
      cost_legacy REAL, cost_recosted INTEGER
    );
    CREATE TABLE posts (
      repo TEXT, pr INTEGER, round INTEGER, head_sha TEXT, posted_at TEXT,
      scoreboard_comment_id INTEGER, threads TEXT, failures TEXT
    );
    CREATE TABLE pairs (
      pair_id TEXT PRIMARY KEY, pr INTEGER, head_sha TEXT, rubric TEXT, prompt_policy TEXT,
      run_a TEXT, run_b TEXT, model_a TEXT, model_b TEXT, rubrics_sha_a TEXT,
      rubrics_sha_b TEXT, arm_a TEXT, arm_b TEXT, verdict_a TEXT, verdict_b TEXT,
      diff_blob TEXT, created_ts TEXT
    );
    CREATE TABLE judgments (
      judgment_id TEXT PRIMARY KEY, pair_id TEXT, sample INTEGER, judge_spec TEXT,
      judge_model TEXT, prompt_sha TEXT, winner_arm TEXT, confidence TEXT,
      "order" TEXT, rationale TEXT, rubric TEXT, pr INTEGER
    );
    CREATE TABLE resolutions (
      pair_id TEXT, policy TEXT, status TEXT, consensus TEXT, audit INTEGER,
      judgment_ids TEXT, reason TEXT, ts TEXT, PRIMARY KEY (pair_id, policy)
    );
    CREATE TABLE human_decisions (
      decision_id TEXT PRIMARY KEY, pair_id TEXT, winner_arm TEXT, raw_choice TEXT,
      presented_first_arm TEXT, labeller TEXT, note TEXT, revised INTEGER,
      pr INTEGER, rubric TEXT, duration_s REAL, ts TEXT, diff_blob TEXT, arms TEXT
    );
    """)

    for _, r in records("records/runs"):
        fnd = r.get("findings") or []
        db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("run_id"), r.get("dedupe_key"), r.get("source"), r.get("arm"),
             r.get("prompt_policy"), r.get("repo"), r.get("pr"), r.get("round"),
             r.get("head_sha"), r.get("base_ref_oid"), r.get("merge_base_sha"),
             r.get("rubric"), r.get("rubrics_repo"), r.get("rubrics_sha"),
             r.get("rubrics_version"), r.get("provider"), r.get("model"), r.get("mode"),
             r.get("auth"), r.get("ci"), r.get("prompt_sha256"), r.get("diff_sha256"),
             r.get("diff_prompt_truncated"), r.get("started_at"), r.get("duration_s"),
             j(r.get("attempts")), j(r.get("usage")), r.get("cost_usd"),
             r.get("cost_estimated"), r.get("verdict"), r.get("confidence"),
             r.get("summary"), r.get("transcript_blob"), r.get("diff_blob"),
             r.get("fidelity"), len(fnd),
             r.get("cost_usd_legacy"), r.get("cost_recosted"), r.get("prices_sha")))
        for i, f in enumerate(fnd):
            db.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?)",
                       (r.get("run_id"), i, f.get("file"), str(f.get("line") or ""),
                        f.get("issue"), f.get("fix"), f.get("evidence")))

    for _, r in records("records/rounds"):
        db.execute("INSERT OR REPLACE INTO rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (r.get("round_id"), r.get("repo"), r.get("pr"), r.get("round"), r.get("ts"),
                    r.get("mode"), r.get("arm"), r.get("source"), r.get("head_sha"),
                    r.get("base_ref_oid"), r.get("merge_base_sha"), r.get("rubrics_sha"),
                    r.get("rubrics_version"), r.get("diff_sha256"), j(r.get("ran")),
                    j(r.get("run_ids")), j(r.get("states")), r.get("overall"),
                    r.get("cost"), r.get("halted_at"), r.get("fidelity"),
                    r.get("cost_legacy"), r.get("cost_recosted")))

    for _, r in records("records/posts"):
        db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?)",
                   (r.get("repo"), r.get("pr"), r.get("round"), r.get("head_sha"),
                    r.get("posted_at"), r.get("scoreboard_comment_id"),
                    j(r.get("threads")), j(r.get("failures"))))

    for sub, table in (("eval/pairs", "pairs"), ("eval/judgments", "judgments"),
                       ("eval/resolutions", "resolutions"), ("eval/decisions", "human_decisions")):
        for _, r in records(sub):
            if table == "pairs":
                a, b = r["arms"]["a"], r["arms"]["b"]
                db.execute("INSERT OR REPLACE INTO pairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (r["pair_id"], r.get("pr"), r.get("head_sha"), r.get("rubric"),
                            r.get("prompt_policy"), a.get("run_id"), b.get("run_id"),
                            a.get("model"), b.get("model"), a.get("rubrics_sha"),
                            b.get("rubrics_sha"), a.get("arm"), b.get("arm"),
                            a.get("verdict"), b.get("verdict"), r.get("diff_blob"),
                            r.get("created_ts")))
            elif table == "judgments":
                judge = r.get("judge") or {}
                db.execute("INSERT OR REPLACE INTO judgments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (r["judgment_id"], r["pair_id"], r.get("sample"),
                            judge.get("spec"), judge.get("model"),
                            judge.get("prompt_sha"), r.get("winner_arm"),
                            r.get("confidence"), r.get("order"), r.get("rationale"),
                            r.get("rubric"), r.get("pr")))
            elif table == "resolutions":
                db.execute("INSERT OR REPLACE INTO resolutions VALUES (?,?,?,?,?,?,?,?)",
                           (r["pair_id"], r.get("policy"), r.get("status"), r.get("consensus"),
                            r.get("audit"), j(r.get("judgment_ids")), r.get("reason"),
                            r.get("ts")))
            else:
                db.execute("INSERT OR REPLACE INTO human_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (r["decision_id"], r["pair_id"], r.get("winner_arm"),
                            r.get("raw_choice"), r.get("presented_first_arm"),
                            r.get("labeller"), r.get("note"), r.get("revised"),
                            r.get("pr"), r.get("rubric"), r.get("duration_s"), r.get("ts"),
                            r.get("diff_blob"), j(r.get("arms"))))

    db.executescript("""
    CREATE VIEW ab_pairs AS
      SELECT a.pr, a.head_sha, a.rubric, a.prompt_policy,
             a.run_id AS run_a, b.run_id AS run_b,
             a.model AS model_a, b.model AS model_b,
             a.rubrics_sha AS rubrics_sha_a, b.rubrics_sha AS rubrics_sha_b,
             a.arm AS arm_a, b.arm AS arm_b,
             a.verdict AS verdict_a, b.verdict AS verdict_b,
             a.fidelity AS fidelity_a, b.fidelity AS fidelity_b
      FROM runs a JOIN runs b
        ON a.pr = b.pr AND a.head_sha = b.head_sha AND a.rubric = b.rubric
       AND a.run_id < b.run_id
       AND COALESCE(a.prompt_policy, 'fresh') = COALESCE(b.prompt_policy, 'fresh')
       AND (a.model != b.model
            OR COALESCE(a.rubrics_sha, '') != COALESCE(b.rubrics_sha, '')
            OR a.arm != b.arm);

    CREATE VIEW pr_outcomes AS
      SELECT r.* FROM rounds r
      JOIN (SELECT pr, MAX(round) AS round FROM rounds
            WHERE COALESCE(arm, 'production') = 'production' GROUP BY pr) last
        ON r.pr = last.pr AND r.round = last.round
      WHERE COALESCE(r.arm, 'production') = 'production';

    CREATE VIEW outcomes AS
      SELECT p.pair_id, p.pr, p.rubric, p.model_a, p.model_b,
             p.rubrics_sha_a, p.rubrics_sha_b,
             res.consensus AS ai_consensus, res.status, res.audit,
             hd.winner_arm AS human_decision, hd.labeller AS human_login,
             COALESCE(hd.winner_arm, res.consensus) AS final_label
      FROM pairs p
      LEFT JOIN resolutions res ON res.pair_id = p.pair_id
      LEFT JOIN (SELECT pair_id, winner_arm, labeller,
                        MAX(ts) OVER (PARTITION BY pair_id, labeller) AS _last, ts
                 FROM human_decisions) hd
        ON hd.pair_id = p.pair_id AND hd.ts = hd._last;
    """)
    db.commit()
    n = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
         for t in ("runs", "rounds", "findings", "posts", "pairs", "judgments",
                   "resolutions", "human_decisions")}
    print(f"built {DB.relative_to(ROOT)}: " + ", ".join(f"{k}={v}" for k, v in n.items()))


if __name__ == "__main__":
    main()
