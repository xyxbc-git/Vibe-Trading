// Phase A · "真闭环" — results log. Ported from frontend/src/lib/drawingLog.ts.
//
// Drawing + scoring tells us how a line *would* have done on one snapshot. To
// actually get "越画越准" we accumulate those outcomes over time, so a growing
// training set builds up (the raw material the ML layer learns from).
//
// Each sample records: when it was taken, a monotonic time key, which mode,
// and its hit/miss outcome vs. the default-params baseline. Samples are
// deduped by mode+key and the store is capped as a ring buffer so
// localStorage never grows unbounded.
//
// Note on the dedupe key (`bars`): the web frontend uses the bar COUNT because
// its data grows within a session. The desktop kline API returns a FIXED
// window (e.g. always 200 bars), so a count-based key would collapse every
// sample onto one entry and the training set could never grow. Desktop callers
// therefore pass the LAST BAR'S TIMESTAMP as `bars` — a new closed bar ⇒ a new
// key ⇒ one accumulated sample per bar per mode, while reloads within the same
// bar stay idempotent.
//
// The mutation/summary logic is kept as pure functions (storage-agnostic) so it
// is unit-testable; the localStorage wrappers are thin.

import type { DrawMode } from "./drawings";

export interface DrawingSample {
  ts: number;             // wall-clock when sampled
  bars: number;           // monotonic time key (desktop: last-bar epoch seconds) — the dedupe key
  mode: DrawMode;
  touches: number;        // future-segment interactions
  hits: number;           // correctly predicted interactions
  hitRate: number;        // hits / touches (tuned params), 0..1
  baselineHitRate: number; // same mode under DEFAULT_PARAMS, 0..1
  uplift: number;         // hitRate - baselineHitRate
  // Market-context feature vector captured at sample time (optional so older
  // stored samples without it still load). Fed to the structured model.
  features?: number[];
}

export interface ModeSummary {
  samples: number;
  avgHitRate: number;
  avgUplift: number;
  lastHitRate: number;
}

export interface LogSummary {
  count: number;
  avgUplift: number;
  perMode: Partial<Record<DrawMode, ModeSummary>>;
}

export const MAX_SAMPLES = 500;

// Separate key prefix from the web frontend so the two apps' stores never mix.
const KEY = (sym: string) => `jarvis.draw.log.${sym}`;

// Merge incoming samples into the existing log: dedupe by `mode:bars` (latest
// wins), keep chronological order, and cap to the most recent `max` samples.
export function mergeSamples(
  existing: DrawingSample[],
  incoming: DrawingSample[],
  max = MAX_SAMPLES,
): DrawingSample[] {
  const byKey = new Map<string, DrawingSample>();
  for (const s of existing) byKey.set(`${s.mode}:${s.bars}`, s);
  for (const s of incoming) byKey.set(`${s.mode}:${s.bars}`, s);
  const merged = [...byKey.values()].sort((a, b) => a.bars - b.bars || a.ts - b.ts);
  return merged.length > max ? merged.slice(merged.length - max) : merged;
}

// Roll the accumulated samples up into per-mode averages + an overall uplift.
export function summarize(samples: DrawingSample[]): LogSummary {
  const groups = new Map<DrawMode, DrawingSample[]>();
  for (const s of samples) {
    const g = groups.get(s.mode);
    if (g) g.push(s); else groups.set(s.mode, [s]);
  }
  const perMode: Partial<Record<DrawMode, ModeSummary>> = {};
  let upliftSum = 0;
  for (const [mode, g] of groups) {
    const n = g.length;
    const hitSum = g.reduce((a, s) => a + s.hitRate, 0);
    const upSum = g.reduce((a, s) => a + s.uplift, 0);
    perMode[mode] = {
      samples: n,
      avgHitRate: hitSum / n,
      avgUplift: upSum / n,
      lastHitRate: g[n - 1].hitRate,
    };
    upliftSum += upSum;
  }
  return {
    count: samples.length,
    avgUplift: samples.length > 0 ? upliftSum / samples.length : 0,
    perMode,
  };
}

// ---------------------------------------------------------------------------
// Phase D · online learning — blend the current snapshot's hit rate with the
// accumulated cross-session average for the same mode, weighting the
// historical average more as evidence (sample count) grows.
// ---------------------------------------------------------------------------

export const LEARN_PRIOR_STRENGTH = 5;

function clamp01(x: number): number {
  if (!Number.isFinite(x)) return 0;
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

export function blendReliability(
  live: number,
  modeSummary: ModeSummary | undefined,
  priorStrength = LEARN_PRIOR_STRENGTH,
): number {
  const liveC = clamp01(live);
  if (!modeSummary || modeSummary.samples <= 0) return liveC;
  const n = modeSummary.samples;
  // history weight grows toward 1 as samples accumulate (n/(n+k))
  const w = n / (n + Math.max(priorStrength, 0));
  return clamp01(w * clamp01(modeSummary.avgHitRate) + (1 - w) * liveC);
}

// --- localStorage wrappers (thin; tolerate private mode / quota) ----------

export function loadLog(symbol: string): DrawingSample[] {
  try {
    const raw = localStorage.getItem(KEY(symbol));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as DrawingSample[]) : [];
  } catch {
    return [];
  }
}

// Append samples and persist; returns the merged log (also useful for the UI).
export function appendLog(symbol: string, incoming: DrawingSample[]): DrawingSample[] {
  const merged = mergeSamples(loadLog(symbol), incoming);
  try {
    localStorage.setItem(KEY(symbol), JSON.stringify(merged));
  } catch {
    /* storage unavailable — in-memory merge still returned, just not persisted */
  }
  return merged;
}

export function clearLog(symbol: string): void {
  try {
    localStorage.removeItem(KEY(symbol));
  } catch {
    /* ignore */
  }
}
