// Phase A · "真闭环" — results log.
//
// Drawing + scoring tells us how a line *would* have done on one snapshot. To
// actually get "越画越准" we have to *accumulate* those outcomes over time, so a
// growing training set builds up (the raw material Phase D's ML will learn from).
//
// Each sample records: when it was taken, the bar count (a time proxy), which
// mode, and its hit/miss outcome vs. the default-params baseline. Samples are
// deduped by mode+bars (the same data reloaded must not inflate the set) and the
// store is capped as a ring buffer so localStorage never grows unbounded.
//
// The mutation/summary logic is kept as pure functions (storage-agnostic) so it
// is unit-testable; the localStorage wrappers are thin.

import type { DrawMode } from "./drawings";

export interface DrawingSample {
  ts: number;             // wall-clock when sampled
  bars: number;           // bar count at sample time — doubles as the dedupe/time key
  mode: DrawMode;
  touches: number;        // future-segment interactions
  hits: number;           // correctly predicted interactions
  hitRate: number;        // hits / touches (tuned params), 0..1
  baselineHitRate: number; // same mode under DEFAULT_PARAMS, 0..1
  uplift: number;         // hitRate - baselineHitRate
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

const KEY = (sym: string) => `vibe.draw.log.${sym}`;

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
// accumulated cross-session average for the same mode, weighting the historical
// average more as evidence (sample count) grows. With no history it is just the
// live rate; with lots of history it converges to the validated mean. So the
// reliability that drives line emphasis "越用越准" across sessions instead of
// reacting to one noisy snapshot. This is the first increment that actually
// *learns* from the growing training set the results log accumulates.
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
