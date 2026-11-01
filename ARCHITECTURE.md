# Indian Stock Momentum Scanner — Architecture

A self-driving NSE-equity momentum scanner, ported from the US momentum
scanner. A scheduled worker pulls NSE's own end-of-day files into a database,
scores each stock (momentum, trailing-stop, volatility), and a web dashboard
reads the pre-computed results.

Designed to run at **$0/month** and to require **zero manual operation** — the
backfill and daily updates run, retry, and resume on their own.

---

## High-level shape

```
┌──────────────────── VERCEL (Frontend + read API) ─────────────────┐
│  Next.js (App Router) + Tailwind + Lightweight Charts              │
│   • Scanner table   (filters: preset / mcap ₹Cr / volume / sort)  │
│   • Stock chart     (candles + auto-flipping trailing stop)       │
│   • Data Status panel (freshness, backfill %, NSE errors)         │
│   • API routes (/app/api/*) read Neon directly via SQL            │
└───────────────────────────────┬───────────────────────────────────┘
                                │  reads (never calls a data source)
                                ▼
┌──────────────────────────── NEON (Postgres) ──────────────────────┐
│  tickers · daily_ohlcv · market_days · scan_runs · scan_results   │
│  job_state · bot_*                                                │
└───────────────────────────────▲───────────────────────────────────┘
                                │  writes
┌────────────────────── GITHUB ACTIONS (worker) ────────────────────┐
│  Daily cron, idempotent:                                          │
│    ingest every missing trading day (1 bhavcopy = whole market)   │
│    → back-adjust splits/bonuses → trim to 90 trading days         │
│    → market caps (MCAP file) → score every preset → scan_results  │
│  Weekly: universe (EQUITY_L.csv) + industries (index lists)       │
│  Source: nsearchives.nseindia.com, behind worker/datasource.py    │
└────────────────────────────────────────────────────────────────────┘
```

### Why this split (and not an always-on backend)
A separate always-on backend isn't needed: the scan is a once-a-day batch, and
the API only reads a tiny pre-computed table. So:
- the **worker** lives in **GitHub Actions** (free cron, no idle server), and
- the **read API** collapses into **Next.js route handlers on Vercel** (no
  cold-start backend, no extra deploy).

The Python logic (fetching + scoring) lives only in the worker. The data layer
sits behind `worker/datasource.py`, so moving to another provider (e.g. a
broker API) touches one file.

### Why NSE files instead of a per-symbol quote API
NSE publishes one **bhavcopy** per trading day: OHLCV and previous close for
every listed security. Per-symbol APIs need ~2,000 requests a day and
throttle datacenter IPs; the bhavcopy needs **one**. The 90-day backfill is
~145 downloads (one per calendar day; weekends/holidays just 404), not
~4,000.

---

## Repository layout

```
worker/                 Python batch job (runs in GitHub Actions or locally)
  config.py             Presets, thresholds, NSE file URL templates, tunables
  db.py                 Postgres connection, schema bootstrap, per-day ingest
  datasource.py         NSE file fetching + header-driven parsers (swappable)
  metrics.py            Momentum / trailing-stop / volatility / auto-flip trail
  scan.py               Orchestrator: metadata → ingest → trim → mcap → score
  run.py                CLI entrypoint (cron); --probe checks NSE reachability
  requirements.txt      psycopg2-binary only

web/                    Next.js dashboard + read API (deploys to Vercel)
  app/
    page.tsx            Dashboard (status panel + scanner table + filters)
    stock/[ticker]/page.tsx   Chart page
    bot/page.tsx        Paper-trading bot (config + results)
    api/                Route handlers reading Neon
  components/
  lib/

db/schema.sql           Canonical schema (also applied by worker/db.py)
.github/workflows/scan.yml   The scheduled worker
```

---

## Data flow / lifecycle

**Every run (daily 14:13 UTC = 19:43 IST, or manual):**

1. **Metadata (weekly).** Download `EQUITY_L.csv` → upsert the EQ-series
   universe with official company names; drop symbols no longer listed. Tag
   industries from the Nifty Total Market (fallback Nifty 500) constituent list.
2. **Ingest.** Work out which dates are missing: on a fresh database, the last
   145 calendar days; otherwise every date after the newest ingested day. Fetch
   them **oldest first**, one bhavcopy each:
   - file found → store that day in one transaction (see adjustment below)
     and mark it `ok` in `market_days`;
   - 404 → keep it pending; once a *later* day's file exists, the pending
     dates are recorded as holidays and never fetched again. A 404 on the
     newest date just means "not published yet" — the next run retries it;
   - blocked/throttled/garbage → stop; everything so far is kept and the next
     run resumes at this date.
3. **Split/bonus back-adjustment.** On an ex-date NSE reports the previous
   close already adjusted. If it differs from our stored close by > 0.5%, all
   older bars of that symbol are scaled by the ratio (volume inversely) —
   the series stays continuous, like an adjusted-price feed. If a large share
   of a day's symbols mismatch at once, that's a data gap, not corporate
   actions, and the day's adjustments are skipped.
4. **Trim** `daily_ohlcv` to the last 90 trading days.
5. **Market caps.** Issue size (shares) from the `MCAP` file in NSE's daily
   PR bundle × close.
6. **Score** every preset → `scan_results`, plus a `scan_runs` status row.

**When you open the dashboard:** the browser hits a Next.js route handler that
runs one indexed query against Neon and returns pre-computed results.
Millisecond response, works even when NSE is unreachable.

---

## India-specific adaptations

- **Universe**: NSE EQ series from `EQUITY_L.csv` on `nsearchives.nseindia.com`
  (fallback `archives.nseindia.com`). NSE blocks datacenter IPs on its main
  website; the archive hosts serve static files and are more lenient.
- **Prices**: CM UDiFF bhavcopy
  (`content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip`), fallback
  `products/content/sec_bhavdata_full_<DDMMYYYY>.csv`. Weekends are tried with
  the primary file only (it also covers special weekend sessions).
- **Market cap / sector**: see above. Stocks outside the Nifty Total Market
  have no sector; stocks missing from the MCAP file keep their last value.
- **Symbols**: stored bare (`RELIANCE`) everywhere.
- **Presets**: price bands in ₹, market caps in INR (1e7 = ₹1 Cr), volume
  floors tuned to NSE liquidity. Momentum/slope thresholds slightly looser
  than the US originals because the 5/10/20% circuit bands cap single-day
  moves.
- **UI units**: ₹ prices, market cap in ₹ Cr / ₹ L Cr, volumes in K/L/Cr.

## Reliability & autonomy

- **Self-resuming** via `market_days`: any date not recorded is fetched next
  run, so missed, early or interrupted runs heal themselves. No cursor.
- **Polite fetching**: 1 request/second, retries with exponential backoff on
  403/429/5xx/network errors; 404 is never retried (it means "not published").
- **Fails loudly, never silently**: 6 weekdays in a row without a bhavcopy
  means NSE moved the files — the run errors out (CI goes red, the status
  panel shows the reason) instead of recording fake holidays. Unparseable
  responses (e.g. an HTML block page) are errors, never holidays.
- **Header-driven parsers**: columns are matched by name, so NSE adding or
  reordering columns doesn't break ingestion. All URLs are templates in
  `config.py`.
- **Status surfaced in the UI**: `scan_runs` records mode, counts, and a
  message → the Data Status panel shows freshness / backfill % / the error.
- **Idempotent** per-day transactions → safe to re-run anytime.
- **Keep-alive**: a workflow commits a small heartbeat file on the 1st and 15th
  so GitHub doesn't auto-disable the schedule after 60 days of inactivity.
- **Probe**: `python run.py --probe` (or *Run workflow → Probe only*) checks
  every NSE endpoint without a database.

## Cost & free-tier notes

- **GitHub Actions** — a daily run is ~1 minute (one or two downloads plus
  scoring); the first backfill a few minutes. Fits the free 2,000 min/month
  even for a private repo.
- **Neon** — 0.5 GB free; a full 90-day backfill for ~2,000 symbols is ≈ 40 MB
  including scan results.
- **Vercel** — Hobby (free) for the dashboard + API routes.
- **Total: $0/month.**

## Configuration (secrets / env)

| Name | Where | Purpose |
|------|-------|---------|
| `DATABASE_URL` | GitHub Actions **repository secret** + Vercel env | Neon Postgres connection string |

See `README.md` for setup steps.
