"use client";

type Status = {
  run: {
    finished_at: string | null;
    mode: string;
    status: string;
    valid_count: number;
    error_count: number;
    symbols_total: number;
    rate_limited: boolean;
    message: string;
  } | null;
  backfill: { phase: string; cursor: number; total: number; pct: number; done: boolean };
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

export default function StatusPanel({ status }: { status: Status | null }) {
  if (!status || !status.backfill) {
    return <div className="rounded-lg border border-slate-800 p-4 text-slate-400">Loading status…</div>;
  }

  const { run, backfill } = status;
  let badge = { text: "Unknown", cls: "bg-slate-700" };

  if (!backfill.done) {
    badge = { text: `Backfilling… ${backfill.pct}% (${backfill.cursor}/${backfill.total})`, cls: "bg-amber-600" };
  } else if (run?.rate_limited) {
    badge = { text: "Rate-limited — auto-retry next run", cls: "bg-orange-600" };
  } else if (run?.status === "error") {
    badge = { text: "Last run failed", cls: "bg-rose-600" };
  } else if (run?.status === "success") {
    badge = { text: "Up to date", cls: "bg-emerald-600" };
  } else if (run) {
    badge = { text: run.status, cls: "bg-slate-600" };
  }

  return (
    <div className="rounded-lg border border-slate-800 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge.cls}`}>{badge.text}</span>
        <span className="text-sm text-slate-400">Last scan {ago(run?.finished_at ?? null)}</span>
        {run && (
          <span className="text-sm text-slate-400">
            · {run.valid_count} matches · {run.symbols_total} symbols
            {run.error_count > 0 && ` · ${run.error_count} errors`}
          </span>
        )}
      </div>
      {!backfill.done && (
        <div className="mt-3 h-2 w-full overflow-hidden rounded bg-slate-800">
          <div className="h-full bg-amber-500" style={{ width: `${backfill.pct}%` }} />
        </div>
      )}
    </div>
  );
}
