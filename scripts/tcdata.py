"""Shared helpers for EpsilonEridaniData scripts: redaction, content-addressed blobs, and
write-if-absent records. Mirrors the semantics of EpsilonEridaniReview's runner/archive.py — the
two must agree on blob addressing and record immutability, since live archival and backfill
write into the same tree."""
import gzip
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

_REDACT = [
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
                r"github_pat_[A-Za-z0-9_]{8,}|xoxb-[A-Za-z0-9-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=\S+"),
     r"\1=[REDACTED]"),
    (re.compile(r"(/home/|/Users/)[^/\s]+"), r"\1[user]"),
]


def redact(text):
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


def write_blob(text, root=None):
    data = redact(text).encode()
    sha = hashlib.sha256(data).hexdigest()
    path = (root or ROOT) / "blobs" / sha[:2] / f"{sha}.gz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as f:
            f.write(data)
    return sha


def write_record(rel, record, root=None, id_field=None):
    """Write-if-absent and never lossy. Returns 'new', 'same', or 'preserved'.

    A same-path/different-content write is a real id collision. Rather than clobber the existing
    record or silently drop the newcomer (the old behaviour, which lost data), the newcomer is
    kept under a content-disambiguated sibling `<stem>-<disc>.json`, with its own primary key
    (`id_field`, e.g. round_id / run_id) rewritten to carry the same `disc` — otherwise the
    derived DB, which keys those columns PRIMARY KEY, would collapse the two rows back into one.
    Records carrying git conflict markers are refused so a botched merge can never enter as data."""
    root = root or ROOT
    body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if "<<<<<<<" in body or "\n>>>>>>>" in body:
        raise RuntimeError(f"refusing to write record with conflict markers at {rel}")
    path = root / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return "new"
    if path.read_text() == body:
        return "same"
    disc = hashlib.sha256(body.encode()).hexdigest()[:12]
    if id_field and record.get(id_field):
        record = dict(record, **{id_field: f"{record[id_field]}-{disc}"})
        body = json.dumps(record, indent=1, sort_keys=True) + "\n"
    sib = path.with_name(f"{path.stem}-{disc}{path.suffix}")
    if sib.exists():
        if sib.read_text() == body:
            return "same"
        raise RuntimeError(f"collision at {rel}: sibling {sib.name} already differs")
    sib.write_text(body)
    print(f"note: id collision at {rel}; preserved newcomer as {sib.name}", file=sys.stderr)
    return "preserved"


def arm_id(arm):
    return ((arm.get("provider") or "") + "/" + (arm.get("model") or "")).lower()


def pair_matches_models(pair, models):
    """True if a pair's two arms correspond to the given model tokens (substring match against
    each arm's provider/model). `models` is a list like ['minimax', 'deepseek']. For two tokens
    it requires one arm to match each (either presentation order); for any other count it requires
    every token to match some arm. Empty `models` matches everything."""
    toks = [m.strip().lower() for m in (models or []) if m.strip()]
    if not toks:
        return True
    ids = [arm_id(pair["arms"]["a"]), arm_id(pair["arms"]["b"])]
    if len(toks) == 2:
        x, y = ids
        return (toks[0] in x and toks[1] in y) or (toks[0] in y and toks[1] in x)
    return all(any(t in i for i in ids) for t in toks)


VERDICTS = {"approve", "request_changes", "block"}


def is_informative(run_a, run_b):
    """True if a pair is worth judging/labelling — i.e. it's two REAL reviews that differ.

    Skipped as uninformative:
    - either side isn't a real review: its verdict is `error` / missing / unparseable (a failed
      run is nothing to compare against);
    - a 'forced tie': both sides share a verdict and neither raised findings (overwhelmingly both
      `approve` with no findings).
    Kept: differing verdicts, or the same verdict but different findings.
    """
    va, vb = (run_a or {}).get("verdict"), (run_b or {}).get("verdict")
    if va not in VERDICTS or vb not in VERDICTS:
        return False
    fa = (run_a or {}).get("findings") or []
    fb = (run_b or {}).get("findings") or []
    return not (not fa and not fb and va == vb)
