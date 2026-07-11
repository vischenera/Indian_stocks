import { NextRequest, NextResponse } from "next/server";
import { getSql } from "@/lib/db";

export const dynamic = "force-dynamic";

// OHLCV bars for the chart page (ascending by date).
export async function GET(
  req: NextRequest,
  { params }: { params: { ticker: string } }
) {
  try {
    const sql = getSql();
    const symbol = params.ticker.toUpperCase();
    const days = Math.min(90, Math.max(5, Number(req.nextUrl.searchParams.get("days") || 90)));
    const rows = await sql`
      SELECT to_char(date, 'YYYY-MM-DD') AS date, open, high, low, close, volume
      FROM daily_ohlcv
      WHERE symbol = ${symbol} AND close IS NOT NULL
      ORDER BY date ASC LIMIT ${days}
    `;
    const meta = await sql`
      SELECT company_name, sector, market_cap FROM tickers WHERE symbol = ${symbol}
    `;
    // Coerce BIGINT volume (returned as string) to number for the chart.
    const bars = rows.map((r: any) => ({ ...r, volume: Number(r.volume) }));
    return NextResponse.json({ symbol, meta: meta[0] ?? null, bars });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
