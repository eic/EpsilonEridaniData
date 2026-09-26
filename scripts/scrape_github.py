#!/usr/bin/env python3
"""Scrape Tau Ceti review comments (and their edit history) from GitHub — the repair path.

Write-time archival (EpsilonEridaniReview runner/archive.py) is the primary record and the store
ledger import (ingest_ledger.py) the primary backfill; this script exists to (a) capture the
raw comment bodies as they appear(ed) on GitHub, including in-place edits of the sticky
scoreboard, and (b) repair gaps if a store is ever lost. It stores immutable SCRAPE EVENTS —
one per (comment, revision) under records/scrape/ with the body in blobs/ — rather than
synthesizing run records: every revision whose meta block parses is cross-checked against the
archive instead (post-provenance comments embed `tauceti-meta:v1`).

Honest limits: GraphQL `userContentEdits.diff` returns the full body per revision (verified),
but `diff` is nullable (a lost revision is recorded as a gap) and GitHub may coalesce rapid
successive edits, so revision counts can undercount rounds. Pre-meta-block bodies are captured
as events but not parsed into records — the ledger import already covers that history.

Idempotent: events are keyed by (comment_id, editedAt, body sha256); the per-writer cursor in
state/ advances only after every touched comment paginated fully, and re-scrapes overlap the
cursor by an hour, so races cause re-reads (deduped), never gaps.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import re
import socket
import subprocess
import sys

import tcdata

REPO = "eic/EpsilonEridani"
MARKERS = ("<!--tauceti-scoreboard-->", "<!--tauceti-rubric:")
META_RE = re.compile(r"<!--tauceti-meta:v1 (\{.*\})-->\s*$")


def gh_json(args):
    r = subprocess.run(["gh", "api", *args], text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:1])} failed: {r.stderr[-300:]}")
    return json.loads(r.stdout)


def list_comments(repo, endpoint, since):
    q = f"/repos/{repo}/{endpoint}?sort=updated&direction=asc&per_page=100"
    if since:
        q += f"&since={since}"
    return gh_json(["--paginate", q])


def edit_history(node_id, typename):
    """All revisions of a comment via userContentEdits. Each node's `diff` is the full body at
    that revision (editedAt is the revision time; node createdAt is unreliable)."""
    out, cursor = [], None
    while True:
        q = ("query($id:ID!,$cursor:String){node(id:$id){... on %s {"
             "userContentEdits(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
             "nodes{editedAt editor{login} diff deletedAt}}}}}" % typename)
        args = ["graphql", "-f", f"query={q}", "-F", f"id={node_id}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        d = gh_json(args)["data"]["node"]
        edits = (d or {}).get("userContentEdits") or {}
        out.extend(edits.get("nodes") or [])
        page = edits.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        cursor = page["endCursor"]


def event_for(comment, kind, revision, counts):
    body = revision.get("diff")
    edited_at = revision.get("editedAt") or ""
    ev = {
        "schema": "tauceti.scrape_event/v1",
        "comment_id": comment["id"], "node_id": comment.get("node_id"),
        "kind": kind, "pr": comment["_pr"],
        "author": (comment.get("user") or {}).get("login"),
        "edited_at": edited_at,
        "editor": (revision.get("editor") or {}).get("login"),
    }
    # No scraped_at field: event content must be a pure function of (comment, revision) so a
    # cursor-overlap re-read reproduces byte-identical records (git history dates the scrape).
    if body is None:
        ev["body_lost"] = True  # nullable diff: a revision GitHub no longer has
    if body is not None:
        ev["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        ev["body_blob"] = tcdata.write_blob(body)
        m = META_RE.search(body.strip())
        if m:
            try:  # cross-reference, trusted only because the author is the known poster
                ev["meta"] = json.loads(m.group(1))
            except Exception:
                ev["meta_parse_error"] = True
    key = hashlib.sha256(f"{comment['id']}|{edited_at}|{ev.get('body_sha256', '')}"
                         .encode()).hexdigest()[:10]
    ts = edited_at.replace("-", "").replace(":", "")[:15] or "unknown"
    counts[tcdata.write_record(
        f"records/scrape/{comment['_pr']}/{comment['id']}-{ts}-{key}.json", ev)] += 1


def scrape(repo, endpoint, kind_of, typename, since, counts):
    """Returns the max updated_at seen iff every matched comment was captured fully."""
    max_updated, complete = since, True
    for c in list_comments(repo, endpoint, since):
        body = c.get("body") or ""
        if not any(mk in body for mk in MARKERS):
            continue
        url = c.get("issue_url") or c.get("pull_request_url") or ""
        c["_pr"] = int(url.rsplit("/", 1)[-1]) if url else 0
        kind = kind_of(body)
        try:
            revisions = edit_history(c["node_id"], typename)
        except RuntimeError as e:
            print(f"warning: edit history failed for comment {c['id']}: {e}", file=sys.stderr)
            complete = False
            revisions = []
        # When edits exist, the revision list includes the current body (last revision) and
        # the creation-time body (first); a synthetic "current" event would duplicate the last
        # revision under a slightly different record. Only never-edited comments need one.
        if revisions:
            for rev in revisions:
                event_for(c, kind, rev, counts)
        else:
            event_for(c, kind, {"diff": body, "editedAt": c.get("updated_at")}, counts)
        max_updated = max(max_updated or "", c.get("updated_at") or "")
    return max_updated, complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--writer", default=socket.gethostname().split(".")[0],
                    help="cursor namespace; each scraping machine keeps its own")
    ap.add_argument("--full", action="store_true", help="ignore the cursor; rescan everything")
    a = ap.parse_args()

    cursor_path = tcdata.ROOT / "state" / f"scrape-cursor-{a.writer}.json"
    state = json.loads(cursor_path.read_text()) if cursor_path.exists() else {}
    counts = {"new": 0, "same": 0, "conflict": 0}

    def overlap(ts):  # re-read an hour behind the cursor; dedup absorbs the repeats
        if not ts or a.full:
            return None
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (t - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for name, endpoint, typename, kind_of in (
            ("issues", "issues/comments", "IssueComment",
             lambda b: "scoreboard"),
            ("pulls", "pulls/comments", "PullRequestReviewComment",
             lambda b: "thread")):
        new_cursor, complete = scrape(a.repo, endpoint, kind_of, typename,
                                      overlap(state.get(name)), counts)
        if complete and new_cursor:
            state[name] = new_cursor
        elif not complete:
            print(f"note: {name} scrape incomplete; cursor not advanced", file=sys.stderr)

    cursor_path.parent.mkdir(exist_ok=True)
    cursor_path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    print(f"scrape: new={counts['new']} unchanged={counts['same']} "
          f"conflicts={counts['conflict']}; cursor={state}")


if __name__ == "__main__":
    main()
