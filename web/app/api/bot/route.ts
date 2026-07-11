import { NextRequest, NextResponse } from "next/server";
import { getSql } from "@/lib/db";
import { ensureBotTables, CONFIG_FIELDS } from "@/lib/botSchema";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const NO_STORE = { headers: { "Cache-Control": "no-store, max-age=0" } };

const num = (v: any) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

// Bot state: config, equity curve, open positions (marked to latest close),
// and the closed-trade log. All data is produced by the worker's replay.
export async function GET() {
  try {
    const sql = getSql();
    await ensureBotTables(sql);
    const [cfgRows, equityRows, tradeRows] = await Promise.all([
      sql`SELECT * FROM bot_config WHERE id = 1`,
      sql`SELECT to_char(date, 'YYYY-MM-DD') AS date, equity, cash FROM bot_equity ORDER BY date ASC`,
      sql`
        SELECT t.id, t.symbol, to_char(t.entry_date, 'YYYY-MM-DD') AS entry_date,
               t.entry_price, t.shares, t.cost, t.stop_level,
               to_char(t.exit_date, 'YYYY-MM-DD') AS exit_date,
               t.exit_price, t.exit_reason, t.pnl,
               lc.close AS last_close
        FROM bot_trades t
        LEFT JOIN LATERAL (
          SELECT close FROM daily_ohlcv
          WHERE symbol = t.symbol AND close IS NOT NULL
          ORDER BY date DESC LIMIT 1
        ) lc ON true
        ORDER BY t.entry_date DESC, t.id DESC
      `,
    ]);

    const config = cfgRows[0]
      ? Object.fromEntries([
          ...CONFIG_FIELDS.map((f) => [f.key, num(cfgRows[0][f.key])]),
          ["updated_at", cfgRows[0].updated_at],
        ])
      : null;

    const open: any[] = [];
    const closed: any[] = [];
    for (const t of tradeRows as any[]) {
      const base = {
        id: t.id,
        symbol: t.symbol,
        entry_date: t.entry_date,
        entry_price: num(t.entry_price),
        shares: num(t.shares),
        cost: num(t.cost),
      };
      if (t.exit_date) {
        closed.push({
          ...base,
          exit_date: t.exit_date,
          exit_price: num(t.exit_price),
          exit_reason: t.exit_reason,
          pnl: num(t.pnl),
          pnl_pct: num(t.cost) > 0 ? (num(t.pnl) / num(t.cost)) * 100 : 0,
        });
      } else {
        const last = num(t.last_close) || num(t.entry_price);
        const value = num(t.shares) * last;
        open.push({
          ...base,
          last_close: last,
          value,
          stop_level: num(t.stop_level),
          unrealized_pnl: value - num(t.cost),
          unrealized_pct: num(t.cost) > 0 ? ((value - num(t.cost)) / num(t.cost)) * 100 : 0,
        });
      }
    }

    const realized = closed.reduce((a, t) => a + t.pnl, 0);
    const wins = closed.filter((t) => t.pnl > 0).length;
    const equity = (equityRows as any[]).map((r) => ({
      date: r.date, equity: num(r.equity), cash: num(r.cash),
    }));

    return NextResponse.json(
      {
        config,
        equity,
        open,
        closed,
        summary: {
          realized_pnl: realized,
          open_pnl: open.reduce((a, t) => a + t.unrealized_pnl, 0),
          trades: closed.length,
          wins,
          win_rate: closed.length ? (wins / closed.length) * 100 : 0,
          equity_now: equity.length ? equity[equity.length - 1].equity : config?.capital ?? 0,
          cash_now: equity.length ? equity[equity.length - 1].cash : config?.capital ?? 0,
        },
      },
      NO_STORE,
    );
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}

// Update config. Takes effect on the next "Run now" replay.
export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const vals: Record<string, number> = {};
    for (const f of CONFIG_FIELDS) {
      const v = Number(body[f.key]);
      if (!Number.isFinite(v) || v < f.min || v > f.max || (f.int && !Number.isInteger(v))) {
        return NextResponse.json(
          { error: `${f.key} must be ${f.int ? "an integer " : ""}between ${f.min} and ${f.max}` },
          { status: 400 },
        );
      }
      vals[f.key] = v;
    }
    const sql = getSql();
    await ensureBotTables(sql);
    await sql`
      INSERT INTO bot_config (id, capital, slots, start_days, slippage_pct,
        stop_pct, mcap_min, mcap_max, min_price, min_slope, min_r2,
        min_up_ratio, min_vol_x, max_entry_age, fade_age, updated_at)
      VALUES (1, ${vals.capital}, ${vals.slots}, ${vals.start_days}, ${vals.slippage_pct},
        ${vals.stop_pct}, ${vals.mcap_min}, ${vals.mcap_max}, ${vals.min_price},
        ${vals.min_slope}, ${vals.min_r2}, ${vals.min_up_ratio}, ${vals.min_vol_x},
        ${vals.max_entry_age}, ${vals.fade_age}, now())
      ON CONFLICT (id) DO UPDATE SET
        capital = EXCLUDED.capital, slots = EXCLUDED.slots,
        start_days = EXCLUDED.start_days, slippage_pct = EXCLUDED.slippage_pct,
        stop_pct = EXCLUDED.stop_pct, mcap_min = EXCLUDED.mcap_min,
        mcap_max = EXCLUDED.mcap_max, min_price = EXCLUDED.min_price,
        min_slope = EXCLUDED.min_slope, min_r2 = EXCLUDED.min_r2,
        min_up_ratio = EXCLUDED.min_up_ratio, min_vol_x = EXCLUDED.min_vol_x,
        max_entry_age = EXCLUDED.max_entry_age, fade_age = EXCLUDED.fade_age,
        updated_at = now()
    `;
    return NextResponse.json({ ok: true, note: "Config saved — hit Run now to replay." }, NO_STORE);
  } catch (e: any) {
    return NextResponse.json({ error: e.message }, { status: 500 });
  }
}
