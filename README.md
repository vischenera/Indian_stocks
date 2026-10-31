# Indian Stock Momentum Scanner (NSE)

A self-driving NSE-equity momentum scanner with trailing-stop analysis —
the Indian-market port of the US momentum scanner.

A scheduled worker pulls NSE's own end-of-day files (the daily **bhavcopy**)
into a Postgres database, scores every stock (momentum, trailing stop,
volatility), and a web dashboard reads the pre-computed results. No
third-party market-data feed is used. It runs at **$0/month** and
needs **no manual operation** — the backfill and daily updates run, retry, and
resume on their own.

> See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design.

```
GitHub Actions (worker)  →  Neon (Postgres)  →  Vercel (dashboard + read API)
```

## Layout

| Path | What |
|------|------|
| `worker/` | Python batch job: fetch → score → store (runs in GitHub Actions or locally) |
| `web/` | Next.js dashboard + read API routes (deploys to Vercel) |
| `db/schema.sql` | Canonical Postgres schema (also auto-applied by the worker) |
| `.github/workflows/` | `scan.yml` (the cron) + `keepalive.yml` (keeps the schedule alive) |

## Setup

### 1. Database (Neon — free)
1. Create a free project at [neon.tech](https://neon.tech).
2. Copy the connection string (`postgres://…`). The schema is created
   automatically on the first worker run.

### 2. Worker (GitHub Actions)
1. Add the connection string as a repo secret named **`DATABASE_URL`**
   (Settings → Secrets and variables → **Actions** → *Secrets* tab →
   **New repository secret**). It must be a *repository* secret: an Actions
   *Variable*, an *Environment* secret, or a Codespaces/Dependabot secret is
   not visible to the `scan` job, and setting it in Vercel does not set it
   here. If it's missing, the `scan` run fails at its first step with
   "DATABASE_URL secret is missing".
2. Optional: **Actions → scan → Run workflow** with **Probe only** ticked
   checks that GitHub's servers can reach NSE's files (no database needed).
3. Trigger **Actions → scan → Run workflow**. The first run backfills the
   last 90 trading days (~150 small downloads, a few minutes); the dashboard's
   status panel shows `Backfilling… %` meanwhile. The repo can stay private:
   a daily run takes about a minute of the free 2,000 min/month.

The `scan` workflow then runs automatically every day at 14:13 UTC (19:43 IST,
after NSE publishes the day's bhavcopy). Every run fills in whatever trading
days are missing, so an early, failed or skipped run is caught up by the next.

### 3. Dashboard (Vercel — free)
1. Import the repo at [vercel.com](https://vercel.com), set **Root Directory =
   `web`**.
2. Add an env var **`DATABASE_URL`** (same Neon string).
3. Deploy. The dashboard reads pre-computed results from Postgres — it
   never calls NSE or any other market-data source.

## Run the worker locally

```bash
cd worker
pip install -r requirements.txt
DATABASE_URL='postgres://…' python run.py
```

Each invocation ingests the missing trading days, rescans and exits.
`python run.py --probe` checks NSE reachability without a database.

## India-specific notes

- **Universe**: NSE main board, EQ series only (~2,000 symbols), from NSE's
  official `EQUITY_L.csv`, refreshed weekly. Trade-for-trade (BE/BZ) and SME
  (SM/ST) series are excluded.
- **Prices**: NSE's daily CM bhavcopy (UDiFF format) from
  `nsearchives.nseindia.com`, with the full bhavcopy (`sec_bhavdata_full`) as
  a fallback file. One file per trading day covers every stock, so the daily
  update is a single download. Splits/bonuses are back-adjusted using NSE's
  adjusted previous close on the ex-date.
- **Market cap**: issue size from NSE's daily `MCAP` file (in the PR bundle)
  × close. **Sector**: the *Industry* column of the Nifty Total Market /
  Nifty 500 constituent lists — stocks outside those indices show no sector.
- **Units**: prices in ₹, market caps in INR (the dashboard shows ₹ crores),
  volumes in K/L/Cr shares.
- **Presets**: thresholds recalibrated for NSE price ranges, liquidity, and
  the 5/10/20% circuit bands (see `worker/config.py`). Treat them as a first
  pass — tune to taste.

## Changing the data provider

All NSE file URLs live in `worker/config.py` (`BHAVCOPY_URLS`, `PR_ZIP_URL`,
`EQUITY_LIST_URLS`, `INDUSTRY_LIST_URLS`); if NSE moves a file, change the
template there. Parsers find columns by header name, so added or reordered
columns don't break them. If NSE stops publishing a bhavcopy for 6 weekdays
in a row, the run fails loudly instead of recording fake holidays.

To use a different provider (e.g. a broker API), implement the four `fetch_*`
methods of `NseArchiveSource` in a new class in `worker/datasource.py` and
return it from `get_source()`.

## Disclaimer

For informational purposes only. Not financial advice.
