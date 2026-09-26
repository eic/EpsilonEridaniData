#!/usr/bin/env python3
"""Patch approximate round durations into backfilled round records, from worker logs.

EpsilonEridaniWorker's logs/task-*.log capture each round's output, but review.py's per-rubric lines
carry no timestamps — per-RUN durations are unrecoverable historically (live records carry them
going forward). What IS recoverable is round wall-clock: the log's first timestamped line marks
the round start and the file's mtime its last write. This patches `duration_s` (with
`duration_source: "log-mtime"`) onto matching `fidelity: reconstructed` round records; exact
records are never touched.

    python3 scripts/mine_logs.py --logs ~/EpsilonEridaniWorker/logs
"""
import argparse
import datetime
import json
import pathlib
import re
import sys

import tcdata

REVIEW_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) round: reviewing PR #(\d+) @ ([0-9a-f]+)", re.M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", required=True, help="EpsilonEridaniWorker logs/ directory")
    ap.add_argument("--tolerance-s", type=int, default=900,
                    help="max gap between the log start and the round record ts to match")
    a = ap.parse_args()

    spans = []  # (pr, head12, start_dt, duration_s)
    for log in sorted(pathlib.Path(a.logs).expanduser().glob("task-*.log")):
        m = REVIEW_LINE.search(log.read_text(errors="replace"))
        if not m:
            continue
        start = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        start = start.astimezone()  # log timestamps are local time
        end = datetime.datetime.fromtimestamp(log.stat().st_mtime).astimezone()
        dur = (end - start).total_seconds()
        if dur > 0:
            spans.append((int(m.group(2)), m.group(3)[:12], start, round(dur, 0)))

    patched = skipped = 0
    for path in sorted((tcdata.ROOT / "records" / "rounds").rglob("*.json")):
        rec = json.loads(path.read_text())
        if rec.get("fidelity") != "reconstructed" or rec.get("duration_s") or not rec.get("ts"):
            continue
        ts = datetime.datetime.fromisoformat(rec["ts"])
        best = None
        for pr, head12, start, dur in spans:
            if pr != rec.get("pr") or not (rec.get("head_sha") or "").startswith(head12):
                continue
            gap = abs((ts - start).total_seconds()) - dur  # record ts lands at round END
            if abs(gap) <= a.tolerance_s and (best is None or abs(gap) < best[0]):
                best = (abs(gap), dur)
        if best:
            rec["duration_s"] = best[1]
            rec["duration_source"] = "log-mtime"
            path.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
            patched += 1
        else:
            skipped += 1
    print(f"mine_logs: {len(spans)} review spans in logs; patched {patched} round records "
          f"({skipped} unmatched)")


if __name__ == "__main__":
    main()
