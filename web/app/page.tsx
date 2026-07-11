"use client";

import { useEffect, useState } from "react";
import StatusPanel from "@/components/StatusPanel";
import ScannerTable, { Row, SortKey } from "@/components/ScannerTable";
import { PRESETS, PRESET_DEFS } from "@/lib/presets";

// Market-cap range in ₹ crores, kept as strings so the inputs can be blank
// (blank = unbounded on that side). Converted to rupees at fetch time.
type Filters = { preset: string; days: number; onlyActive: boolean; mcapMinB: string; mcapMaxB: string };

const DEFAULT_FILTERS: Filters = {
  preset: PRESETS[0].key,
  days: PRESET_DEFS[PRESETS[0].key]?.periodDays ?? 90,
  onlyActive: true,
  mcapMinB: "",
  mcapMaxB: "",
};

const STORAGE_KEY = "scanner-settings-v2";

type Saved = { filters: Filters; sortKey: SortKey; sortDir: "asc" | "desc" };

function loadSaved(): Saved | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (!s?.filters?.preset || !PRESET_DEFS[s.filters.preset]) return null;
    if (typeof s.filters.mcapMinB !== "string" || typeof s.filters.mcapMaxB !== "string") return null;
    return s;
  } catch {
    return null;
  }
}

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);

  // Form state is staged: nothing refetches until Apply. Sorting is the one
  // exception — header clicks apply immediately (cheap, and users expect it).
  const [form, setForm] = useState<Filters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>("period_gain_pct");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  // Settings restore from localStorage on mount; hold fetching until then so
  // the first request uses the restored selection, not the defaults.
  const [hydrated, setHydrated] = useState(false);

  const dirty =
    form.preset !== applied.preset ||
    form.days !== applied.days ||
    form.onlyActive !== applied.onlyActive ||
    form.mcapMinB !== applied.mcapMinB ||
    form.mcapMaxB !== applied.mcapMaxB;

  useEffect(() => {
    const saved = loadSaved();
    if (saved) {
      setForm(saved.filters);
      setApplied(saved.filters);
      setSortKey(saved.sortKey);
      setSortDir(saved.sortDir);
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ filters: applied, sortKey, sortDir } satisfies Saved));
    } catch {}
  }, [hydrated, applied, sortKey, sortDir]);

  useEffect(() => {
    const load = () =>
      fetch("/api/status")
        .then((r) => (r.ok ? r.json() : null))
        .then(setStatus)
        .catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    setLoading(true);
    const qs = new URLSearchParams({
      preset: applied.preset,
      days: String(applied.days),
      sort: sortKey,
      dir: sortDir,
      limit: "300",
      onlyActive: applied.onlyActive ? "1" : "0",
    });
    const minB = Number(applied.mcapMinB);
    const maxB = Number(applied.mcapMaxB);
    if (applied.mcapMinB !== "" && Number.isFinite(minB) && minB > 0) qs.set("mcapMin", String(minB * 1e7));
    if (applied.mcapMaxB !== "" && Number.isFinite(maxB) && maxB > 0) qs.set("mcapMax", String(maxB * 1e7));
    fetch(`/api/results?${qs}`)
      .then((r) => r.json())
      .then((d) => setRows(d.rows || []))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [hydrated, applied, sortKey, sortDir]);

  const onSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex justify-end">
        <a href="/bot" className="text-sm text-sky-400 hover:underline">Paper Bot →</a>
      </div>
      <StatusPanel status={status} />

      <div className="flex flex-wrap items-end gap-4">
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">Preset</span>
          <select
            value={form.preset}
            onChange={(e) => {
              const key = e.target.value;
              setForm((f) => ({ ...f, preset: key, days: PRESET_DEFS[key]?.periodDays ?? f.days }));
            }}
            className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5"
          >
            {PRESETS.map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">Period (days)</span>
          <input
            type="number" min={1} max={90} value={form.days}
            onChange={(e) => setForm((f) => ({ ...f, days: Math.min(90, Math.max(1, Number(e.target.value) || 1)) }))}
            className="w-20 rounded border border-slate-700 bg-slate-900 px-3 py-1.5"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">MCap min (₹ Cr)</span>
          <input
            type="number" min={0} step="any" placeholder="any" value={form.mcapMinB}
            onChange={(e) => setForm((f) => ({ ...f, mcapMinB: e.target.value }))}
            className="w-24 rounded border border-slate-700 bg-slate-900 px-3 py-1.5"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">MCap max (₹ Cr)</span>
          <input
            type="number" min={0} step="any" placeholder="any" value={form.mcapMaxB}
            onChange={(e) => setForm((f) => ({ ...f, mcapMaxB: e.target.value }))}
            className="w-24 rounded border border-slate-700 bg-slate-900 px-3 py-1.5"
          />
        </label>
        <label className="flex items-center gap-2 pb-2 text-sm">
          <input
            type="checkbox" checked={form.onlyActive}
            onChange={(e) => setForm((f) => ({ ...f, onlyActive: e.target.checked }))}
          />
          <span>Active only (stop not triggered)</span>
        </label>
        <button
          onClick={() => {
            // Switching into the breakout preset auto-ranks by its score;
            // header clicks still override afterwards.
            if (form.preset === "breakout" && applied.preset !== "breakout") {
              setSortKey("breakout_score");
              setSortDir("desc");
            }
            setApplied(form);
          }}
          disabled={!dirty || loading}
          className={`rounded px-4 py-1.5 text-sm font-medium ${
            dirty && !loading
              ? "bg-sky-600 text-white hover:bg-sky-500"
              : "cursor-default bg-slate-800 text-slate-500"
          }`}
        >
          Apply
        </button>
        {loading && <span className="pb-2 text-sm text-slate-500">Loading…</span>}
      </div>

      <ScannerTable rows={rows} sortKey={sortKey} sortDir={sortDir} onSort={onSort} />
    </div>
  );
}
