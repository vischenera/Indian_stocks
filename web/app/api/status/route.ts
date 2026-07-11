import { NextResponse } from "next/server";
import { getSql } from "@/lib/db";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

// Latest run status + backfill progress for the dashboard "Data Status" panel.
export async function GET() {
  try {
    const sql = getSql();
    const runs = await sql`
      SELECT id, started_at, finished_at, mode, status, symbols_total,
             symbols_processed, valid_count, error_count, rate_limited, message
      FROM scan_runs ORDER BY id DESC LIMIT 1
    `;
    const phaseRow = await sql`SELECT value FROM job_state WHERE key = 'phase'`;
    const cursorRow = await sql`SELECT value FROM job_state WHERE key = 'backfill_cursor'`;
    const totalRow = await sql`SELECT count(*)::int AS n FROM tickers`;

    const phase = phaseRow[0]?.value ?? "backfill";
    const cursor = Number(cursorRow[0]?.value ?? 0);
    const total = Number(totalRow[0]?.n ?? 0);
    const pct = total > 0 ? Math.min(100, Math.round((cursor / total) * 100)) : 0;

    return NextResponse.json(
      {
        run: runs[0] ?? null,
        backfill: { phase, cursor, total, pct, done: phase === "incremental" },
      },
      { headers: { "Cache-Control": "no-store, max-age=0" } },
    );
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
