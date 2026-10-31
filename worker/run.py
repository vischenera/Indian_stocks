#!/usr/bin/env python3
"""Worker entrypoint.

Used by the GitHub Actions cron and runnable locally:

    DATABASE_URL=postgres://... python worker/run.py     # one scan step
    python worker/run.py --probe                         # check NSE reachability, no DB

Each run ingests whatever trading days are missing (the whole ~90-day backfill
on a fresh database, normally just the new day afterwards), recomputes the
scan, and exits. The schedule re-invokes it; nothing needs resuming by hand.
"""

import sys

from scan import probe, run_once

if __name__ == "__main__":
    if "--probe" in sys.argv[1:]:
        sys.exit(0 if probe() else 1)
    status = run_once()
    # Non-zero exit when the run failed or NSE served nothing at all, so CI
    # surfaces it; 'rate_limited' (partial progress, resumes next run) stays green.
    sys.exit(1 if status == "error" else 0)
