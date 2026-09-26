# Human labelling — design

*Status: the CLI labeller (`scripts/label.py`) is built and producing human decisions — a
first round has landed in `eval/decisions/`. It currently selects pairs by reading
`eval/pairs/` and `eval/judgments/` directly, with a stratified queue (a balanced mix of
AI-consensus, AI-split, and AI-unstable pairs; the AI verdicts are used only to stratify,
never shown) — not the resolver-emitted `eval/bundles/` + `eval/queue.json` described below,
because the resolver is not yet built. `schema/human_decision.v1.json` is committed. The
website front-end, and the bundle/queue constraints it would share, remain future work.*

## Decision: CLI first, website later

Pasting a PAT into a static web page is unpleasant, and device-flow OAuth does not work
from a static page (GitHub's token endpoint sends no CORS headers — a proxy server would
be required). So the first labelling tool is a **CLI** in this repo: identity comes for
free from `gh auth` (`gh api user --jq .login`), and decisions are pushed as commits. A
website later is just another front-end over the same bundle/decision schemas; revisit
auth then (a tiny token-proxy on the tailnet upgrades the static app without a rewrite).

## Presentation bundles

The resolver (see `eval-design.md`) emits one **redacted presentation bundle** per pair
awaiting human input (`eval/bundles/<pair_id>.json`), plus an index `eval/queue.json`.
Bundles are what any front-end renders; they never contain model identity:

- `left`/`right` are pre-assigned **deterministically** from the pair id
  (`left = a if sha256(pair_id + "|display-v1")[0] is even else b`) so the same pair always
  renders identically (auditability), and the mapping is stored in the bundle so the click
  translates back to canonical arms without the front-end knowing models.
- Reviews are rendered from structured fields (verdict, summary, findings) through one
  uniform template — the strongest practical anonymization; residual writing-style leakage
  is accepted and noted in analysis.
- `diff_blob` points into `blobs/` — the exact reviewed diff, immune to force-pushes.
- `bundle_sha256` (self-hash of the canonical form) is echoed into each decision so what
  was on screen is reconstructible.

## The CLI labeller (`scripts/label.py`)

```
python3 scripts/label.py            # next pending pair: render, ask, record, push
python3 scripts/label.py --pair ID  # a specific pair
```

1. `git pull` the data repo; load `eval/queue.json`; skip pairs this login already decided.
2. Render: PR title + rubric header; the diff with ANSI red/green (plain ±-line coloring;
   `delta` if on PATH for side-by-side); then the two anonymized reviews as "Left" and
   "Right".
3. Prompt: `←/l` left better, `→/r` right better, `t` tie, then an optional free-text
   comment. Time-on-pair is recorded as `duration_s`.
4. Record `eval/decisions/<ulid>.json` per `schema/human_decision.v1.json` — including
   `raw_choice`, the presented mapping, `bundle_sha256`, the `gh` login, and the tool
   version — then commit and `pull --rebase` + push (ULID filenames are conflict-free
   across concurrent labellers).

Labellers should know the repo is public: their GitHub login and comments are published.

## Future website notes

- Diff rendering: github.com cannot be iframed (X-Frame-Options); render the cached diff
  with diff2html for GitHub-like red/green.
- Reads via the Contents API (CORS-clean, works for private repos too); writes as
  one-file-per-decision PUTs with 409 retry — same records the CLI produces.
- Decision compaction (folding `eval/decisions/` into the database) is `build_db.py`'s
  job; no server-side state exists anywhere.
