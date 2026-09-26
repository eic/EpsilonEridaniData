# Pairwise review evaluation — design

*Status: partly built and running. `scripts/make_pairs.py` and `scripts/judge.py` (both in
this repo, not EpsilonEridaniReview as an earlier draft of this doc said) produce the pairs and AI
judgments today — `eval/pairs/` and `eval/judgments/` already hold hundreds of pairs and over a
thousand judgments across several judge models and prompt versions. Still unbuilt: the
resolver, the `escalation-v1` policy, and `eval/resolutions/` below. Schemas are committed
(`schema/pair.v1.json`, `schema/judgment.v1.json`, `schema/resolution.v1.json`).*

## Goal

Given two review executions for the same `(pr, head_sha, rubric)` — e.g. a production run
and a shadow arm with a different model or rubric version — decide which review is better.
First pass: AI judges. Hard cases: human meta-reviewers (see `labelling-design.md`).
Outputs feed win-rates per model / rubric version and judge–human agreement metrics.

## Pair identity

Canonical and order-free, so registering the same pair twice is a no-op:

```
arm a = the run with the lexicographically smaller run_id; arm b = the other
pair_id = sha256("pair-v1|{pr}|{head_sha}|{rubric}|{run_id_a}|{run_id_b}")[:16]
```

A pair-maker script scans run records for matching `(pr, head_sha, rubric)` with equal
`prompt_policy` (a fresh shadow run must not be compared against a reactivation-context
production run) and `fidelity: exact`, and writes `eval/pairs/<pair_id>.json`. Arm
metadata (provider, model, rubrics_sha, verdict) is denormalized onto the pair record —
it is what every metric query needs, and it survives run-record schema evolution.

## Judge harness (`scripts/judge.py`)

Reuses `review.py`'s provider runners (`run_claude` / `run_codex` / `run_pi`), env
isolation (`reviewer_env`), one-time-marker verdict extraction, and budget ledger.

Per (pair, judge model, sample):

1. **Resolve inputs.** Both run records and the reviewed diff (from `blobs/`, keyed by the
   pair's `diff_blob` — never refetched from GitHub, so force-pushes can't change the
   evidence). Refuse to judge if the two records' `(pr, head_sha, rubric)` differ.
2. **Idempotency.** Skip if `eval/judgments/` already holds a record for
   `(pair_id, judge.model, prompt_sha256, sample)`.
3. **Workspace.** The same read-only checkout reviewers get: code at `head_sha`, roadmap,
   optional Mathlib. This is the load-bearing call: **judging review quality is mostly
   verifying findings** — a fluent hallucinated finding must lose to a terse real one, and
   that is only detectable by grepping the actual code. Text-only judging would be ~3×
   cheaper but judges the prose, not the review.
4. **Prompt.** `eval/prompts/pairwise-judge-v1.md` (versioned by content; the judgment
   records its sha256) + the rubric text pinned at the arms' `rubrics_sha` (when the SHAs
   differ — a rubric-version A/B — include both, labelled, and instruct the judge to score
   usefulness-to-the-maintainer, not rubric compliance) + the PR description (untrusted
   framing per `_common.md`) + the diff + the two reviews rendered through a **uniform
   template** (verdict, summary, findings table — never raw transcripts, so formatting
   tics don't leak model identity) + a fresh one-time marker.
5. **Two passes, both orders.** Pass 2 presents the reviews in the opposite order to
   pass 1; first-pass order is itself randomized deterministically from `pair_id`. Each
   pass returns `{winner: first|second|tie, confidence, rationale}` after the marker;
   extraction fails closed like `extract_verdict`.
6. **Combine.** Map `first/second` back to canonical `a/b`. Both passes agree → that is
   the decision, confidence = min of the two. Disagree → `decision: "inconsistent"`.
7. **Record** `eval/judgments/<judgment_id>.json` and account spend.

## Escalation policy `escalation-v1`

Panel: 2 judge models from **different families** (claude + codex by default — cross-family
dilutes self-preference bias), 1 sample each. After both judgments exist, the resolver
writes `eval/resolutions/<pair_id>-<policy>.json`:

- **ai_decided** iff both judgments are internally consistent, agree on the decision
  (including an agreed tie), and at least one has confidence ≥ medium.
- **escalated** otherwise — any `inconsistent` judgment, panel disagreement, unanimous-low
  confidence, or provider failure after retry (never silently dropped).
- **audit**: a deterministic ~10% of ai_decided pairs
  (`int(sha256(pair_id + "|audit-v1"), 16) % 10 == 0`) are *also* queued for human review.
  Without this, judge–human agreement would be measured only on hard cases — a biased
  validation set. The human decision still wins for the pair's final label.

State machine (derived; materialized by `build_db.py`'s `outcomes` view):

```
registered → judging → ai_decided —(10% sample)→ audit_pending → audited
                     ↘ escalated → human_decided
```

Where human and AI disagree, the **human decision is the final label**; the AI consensus
is retained for agreement metrics.

## Metrics the schema supports

- Win-rates per model / rubric version: each non-tie `outcomes` row is a Bradley-Terry
  comparison; binomial CIs for fixed head-to-heads. (Tie handling — drop vs Davidson —
  decide before publishing numbers.)
- Judge–human agreement, split by context (`audit` rows estimate agreement on the typical
  distribution, `escalated` rows on the hard one); per-judge via the judgments table.
- Self-preference: judge agreement conditioned on whether the judge's family authored an arm.
- Confidence calibration: judge confidence vs human-overturn rate.

## Open questions (decide during build)

1. Judging budget: own daily cap vs shared with reviews (judging is ~2 reviewer-priced
   calls per pass-pair, ~$1–3/pair with workspace grounding).
2. Subscription-mode judging (free, rate-limited, less reproducible) vs API-only.
3. Tie semantics for Bradley-Terry.
4. Rubric-version A/Bs: is usefulness-to-the-maintainer the right criterion, or does this
   deserve a distinct prompt variant?
5. Comparing a posted review against a later shadow run: production rounds fold in author
   replies via case files. First-round-only production arms are the clean comparand;
   `prompt_policy` matching enforces this, but the pair-maker should also prefer round-1
   production runs.
