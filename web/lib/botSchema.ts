import { getSql } from "./db";

// The bot is fully web-side (no worker involvement), so the web app must
// bootstrap its own tables — schema.sql is only applied by the worker.
// Idempotent, and memoized per lambda instance so warm requests skip it.
let ensured = false;

export async function ensureBotTables(sql: ReturnType<typeof getSql>) {
  if (ensured) return;
  await sql`
    CREATE TABLE IF NOT EXISTS bot_config (
      id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
      capital       DOUBLE PRECISION NOT NULL DEFAULT 100000,
      slots         INTEGER NOT NULL DEFAULT 5,
      start_days    INTEGER NOT NULL DEFAULT 90,
      slippage_pct  DOUBLE PRECISION NOT NULL DEFAULT 0.25,
      stop_pct      DOUBLE PRECISION NOT NULL DEFAULT 10,
      mcap_min      DOUBLE PRECISION NOT NULL DEFAULT 0,
      mcap_max      DOUBLE PRECISION NOT NULL DEFAULT 0,
      min_price     DOUBLE PRECISION NOT NULL DEFAULT 20,
      min_slope     DOUBLE PRECISION NOT NULL DEFAULT 0.6,
      min_r2        DOUBLE PRECISION NOT NULL DEFAULT 0.7,
      min_up_ratio  DOUBLE PRECISION NOT NULL DEFAULT 0.6,
      min_vol_x     DOUBLE PRECISION NOT NULL DEFAULT 1.5,
      max_entry_age INTEGER NOT NULL DEFAULT 5,
      fade_age      INTEGER NOT NULL DEFAULT 5,
      updated_at    TIMESTAMPTZ DEFAULT now()
    )
  `;
  // Idempotent upgrades for configs created before the strategy knobs.
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS stop_pct      DOUBLE PRECISION NOT NULL DEFAULT 10`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS mcap_min      DOUBLE PRECISION NOT NULL DEFAULT 0`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS mcap_max      DOUBLE PRECISION NOT NULL DEFAULT 0`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS min_price     DOUBLE PRECISION NOT NULL DEFAULT 20`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS min_slope     DOUBLE PRECISION NOT NULL DEFAULT 0.6`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS min_r2        DOUBLE PRECISION NOT NULL DEFAULT 0.7`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS min_up_ratio  DOUBLE PRECISION NOT NULL DEFAULT 0.6`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS min_vol_x     DOUBLE PRECISION NOT NULL DEFAULT 1.5`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS max_entry_age INTEGER NOT NULL DEFAULT 5`;
  await sql`ALTER TABLE bot_config ADD COLUMN IF NOT EXISTS fade_age      INTEGER NOT NULL DEFAULT 5`;
  await sql`
    CREATE TABLE IF NOT EXISTS bot_trades (
      id            BIGSERIAL PRIMARY KEY,
      symbol        TEXT NOT NULL,
      entry_date    DATE NOT NULL,
      entry_price   DOUBLE PRECISION NOT NULL,
      shares        DOUBLE PRECISION NOT NULL,
      cost          DOUBLE PRECISION NOT NULL,
      stop_level    DOUBLE PRECISION,
      exit_date     DATE,
      exit_price    DOUBLE PRECISION,
      exit_reason   TEXT,
      pnl           DOUBLE PRECISION
    )
  `;
  await sql`
    CREATE TABLE IF NOT EXISTS bot_equity (
      date    DATE PRIMARY KEY,
      equity  DOUBLE PRECISION NOT NULL,
      cash    DOUBLE PRECISION NOT NULL
    )
  `;
  ensured = true;
}

// Every tunable strategy/config field, its DB column, validation range, and
// default — single source shared by the GET/POST/run routes.
export type BotConfigRow = {
  capital: number; slots: number; start_days: number; slippage_pct: number;
  stop_pct: number; mcap_min: number; mcap_max: number; min_price: number;
  min_slope: number; min_r2: number; min_up_ratio: number; min_vol_x: number;
  max_entry_age: number; fade_age: number;
};

export const CONFIG_FIELDS: {
  key: keyof BotConfigRow; min: number; max: number; int?: boolean; def: number;
}[] = [
  { key: "capital", min: 1, max: 1e12, def: 100000 },
  { key: "slots", min: 1, max: 50, int: true, def: 5 },
  { key: "start_days", min: 10, max: 90, int: true, def: 90 },
  { key: "slippage_pct", min: 0, max: 5, def: 0.25 },
  { key: "stop_pct", min: 1, max: 50, def: 10 },
  { key: "mcap_min", min: 0, max: 1e15, def: 0 },
  { key: "mcap_max", min: 0, max: 1e15, def: 0 }, // 0 = unbounded
  { key: "min_price", min: 0, max: 1e6, def: 20 },
  { key: "min_slope", min: 0, max: 20, def: 0.6 },
  { key: "min_r2", min: 0, max: 1, def: 0.7 },
  { key: "min_up_ratio", min: 0, max: 1, def: 0.6 },
  { key: "min_vol_x", min: 0, max: 20, def: 1.5 },
  { key: "max_entry_age", min: 0, max: 20, int: true, def: 5 },
  { key: "fade_age", min: 1, max: 30, int: true, def: 5 },
];
