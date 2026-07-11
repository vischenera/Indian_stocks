import { NextResponse } from "next/server";
import { getSql } from "@/lib/db";
import { ensureBotTables, CONFIG_FIELDS } from "@/lib/botSchema";
import { runBotSim, BotConfig } from "@/lib/botSim";
import { Bar } from "@/lib/metrics";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
// The replay loads ~600k OHLCV rows and simulates ~90 days; allow headroom.
export const maxDuration = 60;

// Run the simulation on demand, directly against the existing database —
// no worker, no external services. Persists bot_trades / bot_equity and
// returns a summary so the page can refresh immediately.
export async function POST() {
  try {
    const sql = getSql();
    await ensureBotTables(sql);

    const [cfgRows, barRows, metaRows] = await Promise.all([
      sql`
        INSERT INTO bot_config (id) VALUES (1)
        ON CONFLICT (id) DO UPDATE SET id = 1
        RETURNING *
      `,
      sql`
        SELECT symbol, to_char(date, 'YYYY-MM-DD') AS date, open, high, low, close, volume
        FROM daily_ohlcv
        WHERE close IS NOT NULL
        ORDER BY symbol, date ASC
      `,
      sql`SELECT symbol, market_cap FROM tickers`,
    ]);

    const cfg = Object.fromEntries(
      CONFIG_FIELDS.map((f) => {
        const v = Number(cfgRows[0][f.key]);
        return [f.key, Number.isFinite(v) ? v : f.def];
      }),
    ) as unknown as BotConfig;

    const allBars = new Map<string, Bar[]>();
    for (const r of barRows as any[]) {
      const bars = allBars.get(r.symbol) ?? [];
      bars.push({ date: r.date, open: r.open, high: r.high, low: r.low, close: r.close, volume: Number(r.volume) });
      allBars.set(r.symbol, bars);
    }
    const mcaps = new Map<string, number>();
    for (const r of metaRows as any[]) mcaps.set(r.symbol, Number(r.market_cap) || 0);

    const { trades, equity } = runBotSim(allBars, mcaps, cfg);

    // Persist atomically-ish: two bulk statements instead of per-row inserts
    // (the serverless driver pays a round trip per query).
    await sql`DELETE FROM bot_trades`;
    await sql`DELETE FROM bot_equity`;
    if (trades.length) {
      await sql`
        INSERT INTO bot_trades (symbol, entry_date, entry_price, shares, cost,
                                stop_level, exit_date, exit_price, exit_reason, pnl)
        SELECT symbol, entry_date::date, entry_price, shares, cost,
               stop_level, exit_date::date, exit_price, exit_reason, pnl
        FROM jsonb_to_recordset(${JSON.stringify(trades)}::jsonb) AS x(
          symbol text, entry_date text, entry_price float8, shares float8,
          cost float8, stop_level float8, exit_date text, exit_price float8,
          exit_reason text, pnl float8)
      `;
    }
    if (equity.length) {
      await sql`
        INSERT INTO bot_equity (date, equity, cash)
        SELECT date::date, equity, cash
        FROM jsonb_to_recordset(${JSON.stringify(equity)}::jsonb) AS x(
          date text, equity float8, cash float8)
      `;
    }

    const closed = trades.filter((t) => t.exit_date !== null);
    const open = trades.length - closed.length;
    const realized = closed.reduce((a, t) => a + (t.pnl ?? 0), 0);
    return NextResponse.json({
      ok: true,
      note: `Replay done: ${closed.length} closed trades (${realized >= 0 ? "+" : ""}$${realized.toFixed(2)} realized), ${open} open.`,
    });
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
