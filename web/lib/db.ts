import { neon, NeonQueryFunction } from "@neondatabase/serverless";

// Lazy singleton so a missing env var can't throw at import/build time —
// it only errors when a query actually runs.
let _sql: NeonQueryFunction<false, false> | null = null;

export function getSql(): NeonQueryFunction<false, false> {
  if (!_sql) {
    const url = process.env.DATABASE_URL;
    if (!url) throw new Error("DATABASE_URL is not set");
    // The driver issues queries via fetch() under the hood, which Next.js
    // patches and caches by default — without this, dashboard reads can
    // silently return a stale cached response despite dynamic route config.
    _sql = neon(url, { fetchOptions: { cache: "no-store" } });
  }
  return _sql;
}
