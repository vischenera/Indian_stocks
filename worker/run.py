#!/usr/bin/env python3
"""Worker entrypoint.

Used by the GitHub Actions cron and runnable locally:

    DATABASE_URL=postgres://... python worker/run.py

It runs one scan step (backfill chunk or incremental update) then exits. The
schedule re-invokes it; the state machine resumes automatically.
"""

import sys

from scan import run_once

if __name__ == "__main__":
    status = run_once()
    # Non-zero exit only on hard error so CI surfaces genuine failures while
    # 'partial'/'rate_limited' (expected, self-healing) stay green.
    sys.exit(1 if status == "error" else 0)
