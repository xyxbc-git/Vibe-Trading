// 诱多/诱空陷阱信号：数据契约类型 + 本地 mock 识别 + 图表标记换算，
// 全部纯函数（无 IO、无图表依赖），渲染交给 TrapSignalsPrimitive。
//
// 契约来源：陷阱识别引擎 GET /api/trap-signals?symbol=&interval=（B1 后端，
// 并行开发中）。引擎未就绪 / 请求失败时，mockTrapSignals 用真实 K 线找
// 「假突破」形态做本地规则识别：上破前高收回 = 诱多（bull_trap），下破前低
// 收回 = 诱空（bear_trap）。同一批 K 线输入恒产出相同结果（无随机数），
// 轮询刷新幂等、单测可断言；mock:true 标记让 UI 显示「演示数据」角标。

export type TrapType = "bull_trap" | "bear_trap";

/** GET /api/trap-signals 单条信号（B1 数据契约草案） */
export interface TrapSignal {
  id: string;
  /** unix 秒（信号所在 K 线的开盘时间） */
  ts: number;
  /** 陷阱发生价位（诱多 = 冲高点，诱空 = 杀跌点） */
  price: number;
  type: TrapType;
  /** 0..1 */
  confidence: number;
  /** 中文逐条证据 */
  reasons: string[];
  /** 中文操作建议 */
  suggestion: string;
}

/** GET /api/trap-signals 响应（ok/error 为项目封套惯例，可缺省） */
export interface TrapSignalsResponse {
  ok?: boolean;
  symbol?: string;
  interval?: string;
  signals: TrapSignal[];
  mock?: boolean;
  error?: string;
}

/** 陷阱识别输入 K 线（轻量结构，与 lightweight-charts 解耦，便于纯函数单测） */
export interface TrapBar {
  timeSec: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

/** 陷阱标记配色：诱多红色警示（别追多）、诱空绿色警示（别追空），与涨绿跌红铁律一致 */
export const TRAP_COLORS: Record<TrapType, string> = {
  bull_trap: "#f85149",
  bear_trap: "#3fb950",
};

/**
 * 诱多诱空开关的 localStorage 键：K 线页与盘口透视页共用（"1"=开 / "0"=关），
 * 任一页开关即两页状态一致，用户不用两处分别开。
 */
export const TRAP_TOGGLE_KEY = "jarvis.chart.trap";

export const TRAP_LABELS: Record<TrapType, string> = {
  bull_trap: "诱多陷阱",
  bear_trap: "诱空陷阱",
};

/** 图表渲染载荷：信号 + 锚定 bar 的高低点（标记画在影线之外，不遮 K 线） */
export interface TrapMark {
  /** 吸附后的 bar 开盘时间（unix 秒） */
  timeSec: number;
  /** 锚定 bar 高点（诱多标记画在其上方） */
  anchorHigh: number;
  /** 锚定 bar 低点（诱空标记画在其下方） */
  anchorLow: number;
  signal: TrapSignal;
  mock: boolean;
  /** 悬停提示（单行） */
  tooltip: string;
}

function fmtTrapPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function fmtTrapTime(tsSec: number): string {
  return new Date(tsSec * 1000).toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

// ── 本地 mock 识别 ──────────────────────────────────────────────────────
//
// 规则骨架（假突破 = 陷阱的最典型形态）：
//   诱多  bar 上破前 LOOKBACK 根高点，但收盘收回突破位下方 + 长上影
//   诱空  bar 下破前 LOOKBACK 根低点，但收盘收回突破位上方 + 长下影
//   置信度 = 0.35 + 影线占比·0.25 + 收回深度·0.2 + 突破幅度·0.1 + 量能·0.1
//   相邻同类型信号（间隔 < DEDUP_BARS 根）只保留置信度最高的一个

/** 前窗口回看根数 / ATR 回看根数 / 邻近去重根数 / 最多保留信号数 */
const LOOKBACK = 20;
const ATR_BARS = 14;
const DEDUP_BARS = 6;
const MAX_SIGNALS = 12;
/** 影线占整根 K 线的最低比例（低于此不算「长影线」，不构成陷阱形态） */
const MIN_SHADOW_RATIO = 0.45;

interface TrapCandidate {
  idx: number;
  signal: TrapSignal;
}

function avgRange(bars: TrapBar[], endIdx: number): number {
  let sum = 0;
  let n = 0;
  for (let i = Math.max(0, endIdx - ATR_BARS + 1); i <= endIdx; i++) {
    const r = bars[i].high - bars[i].low;
    if (Number.isFinite(r) && r >= 0) {
      sum += r;
      n++;
    }
  }
  return n > 0 ? sum / n : 0;
}

function avgVolume(bars: TrapBar[], endIdx: number): number | null {
  let sum = 0;
  let n = 0;
  for (let i = Math.max(0, endIdx - LOOKBACK); i < endIdx; i++) {
    const v = bars[i].volume;
    if (v != null && Number.isFinite(v) && v > 0) {
      sum += v;
      n++;
    }
  }
  return n > 0 ? sum / n : null;
}

/**
 * 本地规则识别诱多/诱空陷阱（引擎未就绪时的演示回退）。
 * bars 需按时间升序；窗口不足（< LOOKBACK+5 根）返回 null。
 */
export function mockTrapSignals(
  symbol: string,
  interval: string,
  bars: TrapBar[],
): TrapSignalsResponse | null {
  if (bars.length < LOOKBACK + 5) return null;

  const candidates: TrapCandidate[] = [];

  for (let i = LOOKBACK; i < bars.length; i++) {
    const b = bars[i];
    const range = b.high - b.low;
    if (!(range > 0) || !Number.isFinite(b.close)) continue;

    let prevHigh = -Infinity;
    let prevLow = Infinity;
    for (let j = i - LOOKBACK; j < i; j++) {
      if (bars[j].high > prevHigh) prevHigh = bars[j].high;
      if (bars[j].low < prevLow) prevLow = bars[j].low;
    }
    if (!Number.isFinite(prevHigh) || !Number.isFinite(prevLow)) continue;

    const atr = avgRange(bars, i);
    if (!(atr > 0)) continue;
    const avgVol = avgVolume(bars, i);
    const volRatio =
      avgVol !== null && b.volume != null && Number.isFinite(b.volume)
        ? b.volume / avgVol
        : null;

    // ── 诱多：上破前高收回 + 长上影 ──
    const upperShadow = (b.high - Math.max(b.open, b.close)) / range;
    if (b.high > prevHigh && b.close < prevHigh && upperShadow >= MIN_SHADOW_RATIO) {
      const breakout = b.high - prevHigh;
      const shadowScore = clamp(upperShadow, 0, 1);
      const pullbackScore = clamp((prevHigh - b.close) / Math.max(breakout, atr * 0.1), 0, 1);
      const breakoutScore = clamp(breakout / atr, 0, 1);
      const volScore = volRatio !== null ? clamp((volRatio - 1) / 1.5, 0, 1) : 0.3;
      const confidence = clamp(
        0.35 + 0.25 * shadowScore + 0.2 * pullbackScore + 0.1 * breakoutScore + 0.1 * volScore,
        0,
        0.95,
      );

      const reasons = [
        `价格上破近 ${LOOKBACK} 根 K 线高点 ${fmtTrapPrice(prevHigh)} 后未能站稳，收盘回落至突破位下方 ${fmtTrapPrice(b.close)}`,
        `上影线占整根 K 线 ${(upperShadow * 100).toFixed(0)}%，冲高买盘被卖压完全吞没`,
        `突破幅度约 ${((breakout / prevHigh) * 100).toFixed(2)}%（${fmtTrapPrice(breakout)}），随即全部回吐，假突破特征明显`,
      ];
      if (volRatio !== null) {
        reasons.push(
          volRatio >= 1.2
            ? `成交量为近 ${LOOKBACK} 根均量的 ${volRatio.toFixed(1)} 倍，放量冲高回落常见于诱多出货`
            : `突破未伴随放量（量能仅均量 ${volRatio.toFixed(1)} 倍），缺乏真实买盘支撑`,
        );
      }

      candidates.push({
        idx: i,
        signal: {
          id: `trap-bull-${b.timeSec}`,
          ts: b.timeSec,
          price: b.high,
          type: "bull_trap",
          confidence,
          reasons,
          suggestion:
            `疑似诱多陷阱：不宜在此追多。已持多单建议把止损收紧至该 K 线低点 ${fmtTrapPrice(b.low)} 下方；` +
            `等价格重新放量站稳 ${fmtTrapPrice(prevHigh)} 上方，再考虑恢复多头思路。`,
        },
      });
      continue;
    }

    // ── 诱空：下破前低收回 + 长下影 ──
    const lowerShadow = (Math.min(b.open, b.close) - b.low) / range;
    if (b.low < prevLow && b.close > prevLow && lowerShadow >= MIN_SHADOW_RATIO) {
      const breakout = prevLow - b.low;
      const shadowScore = clamp(lowerShadow, 0, 1);
      const pullbackScore = clamp((b.close - prevLow) / Math.max(breakout, atr * 0.1), 0, 1);
      const breakoutScore = clamp(breakout / atr, 0, 1);
      const volScore = volRatio !== null ? clamp((volRatio - 1) / 1.5, 0, 1) : 0.3;
      const confidence = clamp(
        0.35 + 0.25 * shadowScore + 0.2 * pullbackScore + 0.1 * breakoutScore + 0.1 * volScore,
        0,
        0.95,
      );

      const reasons = [
        `价格下破近 ${LOOKBACK} 根 K 线低点 ${fmtTrapPrice(prevLow)} 后迅速收回，收盘拉回破位上方 ${fmtTrapPrice(b.close)}`,
        `下影线占整根 K 线 ${(lowerShadow * 100).toFixed(0)}%，杀跌卖盘被买方完全承接`,
        `破位幅度约 ${((breakout / prevLow) * 100).toFixed(2)}%（${fmtTrapPrice(breakout)}），随即全部收复，假破位特征明显`,
      ];
      if (volRatio !== null) {
        reasons.push(
          volRatio >= 1.2
            ? `成交量为近 ${LOOKBACK} 根均量的 ${volRatio.toFixed(1)} 倍，放量下杀被吸收常见于诱空洗盘`
            : `破位未伴随放量（量能仅均量 ${volRatio.toFixed(1)} 倍），空头动能不足`,
        );
      }

      candidates.push({
        idx: i,
        signal: {
          id: `trap-bear-${b.timeSec}`,
          ts: b.timeSec,
          price: b.low,
          type: "bear_trap",
          confidence,
          reasons,
          suggestion:
            `疑似诱空陷阱：不宜在此追空。已持空单建议把止损收紧至该 K 线高点 ${fmtTrapPrice(b.high)} 上方；` +
            `价格若再次有效跌破 ${fmtTrapPrice(prevLow)}，才重新考虑空头思路。`,
        },
      });
    }
  }

  // 邻近去重：同类型信号在 DEDUP_BARS 根内只保留置信度最高的
  const deduped: TrapCandidate[] = [];
  for (const c of candidates) {
    const last = deduped[deduped.length - 1];
    if (
      last &&
      last.signal.type === c.signal.type &&
      c.idx - last.idx < DEDUP_BARS
    ) {
      if (c.signal.confidence > last.signal.confidence) deduped[deduped.length - 1] = c;
      continue;
    }
    deduped.push(c);
  }

  return {
    ok: true,
    symbol,
    interval,
    signals: deduped.slice(-MAX_SIGNALS).map((c) => c.signal),
    mock: true,
  };
}

// ── 响应 → 图表标记载荷 ────────────────────────────────────────────────

/** buildTrapMarks 的锚定输入（只需时间与高低点） */
export interface TrapAnchorBar {
  timeSec: number;
  high: number;
  low: number;
}

/**
 * 把信号吸附到当前窗口的 K 线上（正常契约 ts 即 bar 开盘时间，直接命中；
 * 二分吸附兜底容错），窗口外的信号丢弃。返回按时间升序的标记数组。
 */
export function buildTrapMarks(
  resp: TrapSignalsResponse | null,
  bars: TrapAnchorBar[],
): TrapMark[] {
  if (!resp || !Array.isArray(resp.signals) || bars.length === 0) return [];
  const firstTs = bars[0].timeSec;
  const lastTs = bars[bars.length - 1].timeSec;
  const mock = resp.mock === true;

  const snapIdx = (ts: number): number => {
    let lo = 0;
    let hi = bars.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (bars[mid].timeSec < ts) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0) {
      const cur = bars[lo].timeSec;
      const prev = bars[lo - 1].timeSec;
      if (ts - prev <= cur - ts) return lo - 1;
    }
    return lo;
  };

  const marks: TrapMark[] = [];
  for (const s of resp.signals) {
    const ts = Number(s?.ts);
    const price = Number(s?.price);
    if (!Number.isFinite(ts) || !Number.isFinite(price)) continue;
    if (ts < firstTs || ts > lastTs) continue;
    if (s.type !== "bull_trap" && s.type !== "bear_trap") continue;
    const bar = bars[snapIdx(ts)];
    marks.push({
      timeSec: bar.timeSec,
      anchorHigh: bar.high,
      anchorLow: bar.low,
      signal: s,
      mock,
      tooltip:
        `⚠ ${TRAP_LABELS[s.type]}${mock ? "（演示数据）" : ""} · 置信 ${(clamp(s.confidence, 0, 1) * 100).toFixed(0)}%` +
        ` · ${fmtTrapTime(ts)} @ ${fmtTrapPrice(price)} · 点击看原因`,
    });
  }
  marks.sort((a, b) => a.timeSec - b.timeSec);
  return marks;
}
