#!/usr/bin/env python3
"""Pairwise AI judge for A/B review pairs, with position-debiasing and self-consistency.

For each pair it runs the judge in BOTH presentation orders (A-first / B-first) and K samples
each, so we can measure: position bias (does the winner flip when A/B swap?) and self-consistency
(do repeated samples agree?). Verdicts are mapped back to the canonical arms a/b. Judgment records
go to eval/judgments/<id>.json (deterministic id -> idempotent re-runs).

    python3 scripts/judge.py --judge deepseek --pairs 5 --samples 3          # reliability pass
    python3 scripts/judge.py --judge deepseek --rubric correctness --all     # judge a cohort
    python3 scripts/judge.py --judge grok --models minimax,deepseek --all    # cover one matchup

The judge sees the cached diff + both reviews rendered through one uniform, anonymized template
(no model identity, order set by the pass). Model is invoked read-only via its CLI (pi for
OpenRouter, claude/codex for subscription); pricing comes from the cost the runner-style call
reports. This is text-grounded judging (diff + structured reviews); a later version can give the
judge the code checkout to verify findings by grep.

Part of the eval pipeline (make_pairs -> judge -> label -> calibration/power). Keep the README's
"Labelling and analysis" section in sync when these scripts' flags or behaviour change.
"""
import argparse
import collections
import gzip
import hashlib
import json
import pathlib
import secrets
import subprocess

import tcdata

DIFF_CAP = 120_000
PROMPT = ""        # set in main() from --prompt
PROMPT_SHA = ""
PROMPT_NAME = ""

# judge spec -> (transport, model). pi = OpenRouter via `pi`; claude/codex = subscription CLIs.
JUDGES = {
    "deepseek": ("pi", "deepseek/deepseek-v4-pro"),
    "grok": ("pi", "x-ai/grok-4.3"),
    "minimax": ("pi", "minimax/minimax-m3"),
    "sonnet": ("claude", "claude-sonnet-4-6"),
    "opus": ("claude", "claude-opus-4-8"),
    "gpt-5.5": ("codex", "gpt-5.5"),
}


def blob_text(sha):
    p = tcdata.ROOT / "blobs" / sha[:2] / (sha + ".gz")
    return gzip.decompress(p.read_bytes()).decode("utf-8", "replace")


def load_run(run_id, pr):
    return json.loads((tcdata.ROOT / "records" / "runs" / str(pr) / (run_id + ".json")).read_text())


def render_review(run):
    out = [f"Verdict: {run.get('verdict')}", f"Summary: {run.get('summary') or '(none)'}", "Findings:"]
    fs = run.get("findings") or []
    if not fs:
        out.append("  (no findings)")
    for f in fs:
        out.append(f"  - {f.get('file', '')}:{f.get('line', '')} — {f.get('issue', '')}"
                   f"  [fix: {f.get('fix', '')}]")
    return "\n".join(out)


def call_pi(model, prompt):
    cmd = ["pi", "--provider", "openrouter", "--model", model, "--print", "--mode", "json",
           "--no-session", "--no-context-files", "--no-skills", "--no-extensions",
           "--no-prompt-templates", prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    text = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            content = (ev["message"].get("content") or [])
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if parts:
                text = "\n".join(parts)
    return text


def call_claude(model, prompt):
    r = subprocess.run(["claude", "--print", "--model", model, prompt],
                       capture_output=True, text=True, timeout=600)
    return r.stdout


def call_codex(model, prompt):
    cmd = ["codex", "exec", "--json", "-s", "read-only", "--skip-git-repo-check",
           "-c", "shell_environment_policy.inherit=none", "-m", model, prompt]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    text = ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            text = ev["item"].get("text", "")  # final assistant message carries the verdict
    return text


def call_judge(spec, prompt):
    transport, model = JUDGES[spec]
    return {"pi": call_pi, "claude": call_claude, "codex": call_codex}[transport](model, prompt)


def extract(text, marker):
    """Parse the JSON object emitted after the (last) marker. Fail-closed to None."""
    idx = text.rfind(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker):]
    s = tail.find("{")
    if s < 0:
        return None
    depth = 0
    for i, ch in enumerate(tail[s:], s):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            try:
                v = json.loads(tail[s:i + 1])
            except Exception:
                return None
            if v.get("winner") in ("first", "second", "tie"):
                return v
            return None
    return None


def one_judgment(pair, spec, order, sample):
    """Run the judge once for (order, sample); return a judgment dict (winner mapped to a/b)."""
    a = load_run(pair["arms"]["a"]["run_id"], pair["pr"])
    b = load_run(pair["arms"]["b"]["run_id"], pair["pr"])
    first, second = (a, b) if order == "ab" else (b, a)
    marker = "JUDGE-" + secrets.token_hex(8)
    prompt = PROMPT.format(rubric=pair["rubric"], marker=marker, diff=blob_text(pair["diff_blob"])[:DIFF_CAP],
                           review_first=render_review(first), review_second=render_review(second))
    try:
        out = call_judge(spec, prompt)
    except Exception:
        out = ""  # timeout / transport failure -> "error" sample below; the batch keeps going
    v = extract(out, marker)
    if v is None:
        winner_arm, conf, rat = "error", None, "(unparseable judge output)"
    else:
        m = {"first": "a", "second": "b"} if order == "ab" else {"first": "b", "second": "a"}
        winner_arm = m.get(v["winner"], "tie")
        conf, rat = v.get("confidence"), v.get("rationale")
    return {"winner_arm": winner_arm, "confidence": conf, "rationale": rat}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default="deepseek", choices=list(JUDGES))
    ap.add_argument("--prompt", default="pairwise-judge-v2", help="prompt file stem in eval/prompts/")
    ap.add_argument("--pairs", type=int, default=5, help="number of pairs (ignored with --all)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rubric", default="")
    ap.add_argument("--pr", default="", help="comma-separated PR numbers to restrict to")
    ap.add_argument("--models", default="",
                    help="comma-separated arm tokens; restrict to a matchup (e.g. minimax,deepseek)")
    ap.add_argument("--samples", type=int, default=3, help="samples per order (per pair: 2*samples calls)")
    a = ap.parse_args()

    global PROMPT, PROMPT_SHA, PROMPT_NAME
    PROMPT_NAME = a.prompt
    PROMPT = (tcdata.ROOT / "eval" / "prompts" / (PROMPT_NAME + ".md")).read_text()
    PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()[:16]

    pairs = [json.loads(p.read_text()) for p in sorted((tcdata.ROOT / "eval" / "pairs").glob("*.json"))]
    if a.rubric:
        pairs = [p for p in pairs if p["rubric"] == a.rubric]
    if a.pr:
        prset = {int(x) for x in a.pr.split(",") if x.strip()}
        pairs = [p for p in pairs if p["pr"] in prset]
    models = [m for m in a.models.split(",") if m.strip()]
    if models:
        pairs = [p for p in pairs if tcdata.pair_matches_models(p, models)]

    def has_diff(p):
        b = p.get("diff_blob")
        return bool(b) and (tcdata.ROOT / "blobs" / b[:2] / (b + ".gz")).exists()

    nodiff = [p for p in pairs if not has_diff(p)]
    pairs = [p for p in pairs if has_diff(p)]
    if nodiff:
        print(f"skipping {len(nodiff)} pairs with no cached diff (not judgeable)")

    def informative(p):  # drop forced ties and pairs with an errored/non-review arm
        try:
            return tcdata.is_informative(load_run(p["arms"]["a"]["run_id"], p["pr"]),
                                         load_run(p["arms"]["b"]["run_id"], p["pr"]))
        except Exception:
            return False
    uninf = [p for p in pairs if not informative(p)]
    pairs = [p for p in pairs if informative(p)]
    if uninf:
        print(f"skipping {len(uninf)} uninformative pairs (forced tie / errored arm)")

    if not a.all:
        pairs = pairs[:a.pairs]
    print(f"judging {len(pairs)} pairs with {a.judge}, {a.samples} samples x 2 orders each")

    by_pair = collections.defaultdict(list)  # pair_id -> list of (order, sample, winner_arm)
    new = skip = err = 0
    for pair in pairs:
        for order in ("ab", "ba"):
            for s in range(a.samples):
                jid = "j-" + hashlib.sha256(
                    f"{pair['pair_id']}|{a.judge}|{PROMPT_SHA}|{order}|{s}".encode()).hexdigest()[:16]
                dst = tcdata.ROOT / "eval" / "judgments" / (jid + ".json")
                if dst.exists():
                    rec = json.loads(dst.read_text()); skip += 1
                else:
                    res = one_judgment(pair, a.judge, order, s)
                    rec = {"schema": "tauceti.judgment/v1", "judgment_id": jid,
                           "pair_id": pair["pair_id"], "pr": pair["pr"], "rubric": pair["rubric"],
                           "judge": {"spec": a.judge, "model": JUDGES[a.judge][1],
                                     "prompt_file": f"eval/prompts/{PROMPT_NAME}.md",
                                     "prompt_sha": PROMPT_SHA},
                           "order": order, "sample": s, **res}
                    if res["winner_arm"] == "error":
                        err += 1  # don't persist failures (timeout/unparseable) — let a re-run retry
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(json.dumps(rec, indent=2) + "\n"); new += 1
                by_pair[pair["pair_id"]].append((order, rec["winner_arm"]))

    # Reliability summary
    flips = consistent_pairs = 0
    intra = []  # self-consistency per (pair, order)
    for pid, obs in by_pair.items():
        per_order = collections.defaultdict(list)
        for order, w in obs:
            per_order[order].append(w)
        # self-consistency: modal fraction within each order
        for order, ws in per_order.items():
            top = collections.Counter(ws).most_common(1)[0][1]
            intra.append(top / len(ws))
        # order-flip: modal winner in ab vs ba
        def modal(ws):
            return collections.Counter(ws).most_common(1)[0][0] if ws else None
        wab, wba = modal(per_order.get("ab", [])), modal(per_order.get("ba", []))
        if wab is not None and wba is not None:
            if wab == wba:
                consistent_pairs += 1
            else:
                flips += 1
    n = consistent_pairs + flips
    print(f"\nwrote {new} judgments ({skip} cached, {err} errored/timed-out, not persisted). "
          f"judge={a.judge}")
    print(f"self-consistency (modal share within an order, mean): "
          f"{sum(intra)/len(intra):.2f}" if intra else "n/a")
    print(f"order-stability: {consistent_pairs}/{n} pairs keep the same winner across A/B swap "
          f"({flips} flips)")


if __name__ == "__main__":
    main()
