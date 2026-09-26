# EpsilonEridaniData

The durable record of AI code reviews in the Tau Ceti project: every review
execution on [eic/EpsilonEridani](https://github.com/eic/EpsilonEridani)
PRs, archived with full provenance (exact rubric version, model, exact diff,
runtime, tokens, cost), plus the evaluation data built on top of it (pairwise
judgments by AI judges and human meta-reviewers).

Reviews are produced by
[eic/EpsilonEridaniReview](https://github.com/eic/EpsilonEridaniReview);
this repo is the analytics archive. The operational state (scheduling,
staleness, budgets) stays in EpsilonEridaniReview's `reviews` branch ledger — nothing
in the worker reads from here.

## Design

- **Write-time archival is the primary record.** The runner writes records to
  a local outbox as it reviews; a separate sync step pushes them here. The
  scoreboard comment on a PR is edited in place across rounds, so GitHub is a
  bad primary record — scraping it (see `scripts/scrape_github.py`) is only a
  backfill/repair path.
- **One file per record**, named by a unique id. Two independent writers exist
  (the local worker and CI), and shadow A/B arms add more; unique filenames
  make pushes conflict-free by construction and re-ingestion idempotent.
- **SQLite is derived, never committed.** `scripts/build_db.py` rebuilds
  `db/epsiloneridani.db` from the record files in seconds.
- **Public, redacted.** Records are built from an explicit field allowlist;
  provider session ids and raw stderr never leave the producing machine, and
  transcripts are scrubbed before upload.

## Layout

```
schema/        JSON Schemas, one per record kind, versioned (run.v1, round.v1, …)
records/
  runs/<pr>/<run_id>.json      one per review execution (pr × head × rubric × model)
  rounds/<pr>/<round_id>.json  one per review round (a scheduling envelope, may be partial)
  posts/<pr>/<round_id>.json   comment ids confirmed posted, for linking to live threads
blobs/<aa>/<sha256>.gz         content-addressed reviewed diffs and reviewer transcripts
eval/          pairwise A/B evaluation data (pairs, judgments, resolutions, decisions)
scripts/       ingest/scrape/build tooling + A/B pairing (make_pairs, ab_fill)
state/         scrape cursors (per writer)
docs/          design docs for the evaluation pipeline
db/            derived SQLite (gitignored)
```

## The analysis unit

A **run record** (`epsiloneridani.run/v1`) is one review execution. A/B queries group
runs by `(pr, head_sha, rubric)` and compare across `(model, rubrics_sha, arm)`
— restricted to matching `prompt_policy`, because a production re-review
carries prior-case-file context that a fresh shadow run does not see.

Rounds are scheduling envelopes (they halt on a block verdict, skip on budget,
or run a single contested rubric); don't treat them as evaluation units.

## Producing records

The EpsilonEridaniReview runner writes records into `<store>/outbox/` during a review
(`runner/archive.py`), and `runner/archive.py sync --data-dir <checkout>`
drains the outbox into this repo: write-if-absent, commit, rebase, push, then
clear. Safe to re-run; a push outage never fails a review.

## A/B pairs

```
python3 scripts/make_pairs.py                       # register pairs from coexisting runs
python3 scripts/ab_fill.py --target deepseek        # estimate cost of filling deepseek's arm
python3 scripts/ab_fill.py --target deepseek --commands --limit 25 > fill.sh   # then run a sample
```

`make_pairs.py` registers an `eval/pairs/<id>.json` for every two runs of the same
(pr, head_sha, rubric) that differ in model, rubric version, or arm — the naturally-occurring
model-vs-model collisions already in the history, plus any shadow arm added later.
`ab_fill.py` finds tasks lacking a given provider's arm and either estimates the cost of
filling them (calibrated against that provider's own historical token usage) or emits the
`epsiloneridani-review --shadow` commands that produce the arms. The shadow runs archive back here and
pair up the next time `make_pairs.py` runs.

## Evaluation so far

The pipeline on top of the run archive: `make_pairs.py` registers A/B pairs; `judge.py` runs
AI judges over them (both presentation orders, a cross-family panel, grounded in the
checked-out code) into `eval/judgments/`; `label.py` records human meta-review decisions in
`eval/decisions/`; `calibration.py` compares judge consensus against the human labels.

The archive currently holds several thousand review runs (with their reviewed-diff and
transcript blobs), a few hundred A/B pairs, over a thousand AI judgments — across five judge
models and three judge-prompt versions — and a first round of human decisions. Resolutions and
presentation bundles (see `docs/eval-design.md` and `docs/labelling-design.md`) are still being
wired up.

### Labelling and analysis

```
gh auth status                                      # label.py records your gh login as the labeller
python3 scripts/label.py                            # human meta-review: pick the better review per pair
python3 scripts/label.py --models minimax,deepseek  # focus one matchup (or --pr / --rubric)
python3 scripts/calibration.py                      # do the AI judges agree with the human labels?
python3 scripts/power.py                            # labels needed to decide a matchup; AI-boost savings
```

`label.py` shows the reviewed diff and two anonymized, order-randomized reviews and asks which
leads to the better PR (the AI verdicts are hidden; they only stratify the queue). It writes one
durable `eval/decisions/<id>.json` per (pair, labeller), committed and pushed as you go. The
queue round-robins across PRs so topics interleave; `--models`/`--pr`/`--rubric` narrow it.
`calibration.py` and `power.py` read the live decisions, so their numbers sharpen as you label.

## Rebuilding the database

```
python3 scripts/build_db.py            # writes db/epsiloneridani.db
sqlite3 db/epsiloneridani.db "SELECT * FROM ab_pairs LIMIT 5"
```

## Cost analysis

`epsiloneridani-review-costs` (in
[EpsilonEridaniReview](https://github.com/eic/EpsilonEridaniReview/blob/main/runner/COSTS.md))
attributes review spend — tokens **and** imputed dollars — to PRs and to merged
lines of code, reading the `records/runs/` files here. Because the token counts
are the immutable fact, it recomputes cost from them at the rate in effect on
each run's date (`runner/prices-history.json`), so the numbers are faithful to
when a run happened and reproducible by anyone from this public archive:

```
git clone --depth 1 https://github.com/eic/EpsilonEridaniData /tmp/EpsilonEridaniData
uvx --from git+https://github.com/eic/EpsilonEridaniReview epsiloneridani-review-costs \
  --source data --data-dir /tmp/EpsilonEridaniData all
```

It defaults to the production arm; `--include-shadows` includes the A/B arms.
