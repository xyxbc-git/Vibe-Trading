import { useState, useMemo, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useApi, usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { useLivePrice } from "@/hooks/usePrice";
import { api, formatPrice, type TwelveSignal, type ConsensusTradePlan, type KeyLevel, type LiqMapResponse } from "@/api/client";
import KlineChart from "@/components/charts/KlineChart";
import { tradesToMarks } from "@/lib/signalTrades";
import {
  computeDrawings,
  computeSmartLevels,
  computeBias,
  gridSearchParams,
  evaluateParams,
  scoreDrawings,
  DEFAULT_PARAMS,
  type BaseData,
  type DrawMode,
  type DrawParams,
  type SmartBias,
  type DrawingResult,
} from "@/lib/drawings";
import {
  appendLog,
  loadLog,
  clearLog,
  summarize,
  blendReliability,
  type DrawingSample,
} from "@/lib/drawingLog";
import {
  extractFeatures,
  trainModel,
  predictProba,
  buildTrainingSet,
  MODEL_MIN_SAMPLES,
} from "@/lib/drawingModel";
import { planToOverlay } from "@/lib/tradePlan";
import {
  buildPositionZoneView,
  positionZoneFromQuery,
  stripPositionZoneQuery,
} from "@/lib/positionZone";
import {
  composeChartView,
  isStaleEcho,
  loadViewMode,
  saveViewMode,
  VIEW_MODES,
  type ViewMode,
  type ChartComposition,
} from "@/lib/chartView";
import {
  mockPredict,
  buildPredictionOverlay,
  type PredictResponse,
  type PredictBar,
} from "@/lib/predict";
import { mockDelta, normalizeDeltaResponse, type DeltaResponse, type DeltaKline } from "@/lib/deltaFlow";
import DeltaPane from "@/components/charts/DeltaPane";
import DeltaAiExplainCard from "@/components/cards/DeltaAiExplainCard";
import { CandlestickChart, HelpCircle, Target, X } from "lucide-react";
import PositionAdvisor from "@/components/cards/PositionAdvisor";
import PredictionCard from "@/components/cards/PredictionCard";
import ReversalScorePanel from "@/components/cards/ReversalScorePanel";
import { clsx } from "clsx";
import type {
  CandlestickData,
  HistogramData,
  Time,
} from "lightweight-charts";

const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

const LIMITS: Record<Timeframe, number> = {
  "1m": 300,
  "5m": 200,
  "15m": 200,
  "30m": 200,
  "1h": 168,
  "4h": 200,
  "1d": 180,
};

const DRAW_OPTIONS: { id: DrawMode; label: string }[] = [
  { id: "trend", label: "趋势线" },
  { id: "sr", label: "支撑压力" },
  { id: "fib", label: "斐波那契" },
  { id: "channel", label: "平行通道" },
  { id: "rect", label: "矩形区间" },
];

const DRAW_COLORS = {
  up: "#3fb950",
  down: "#f85149",
  sr: "#d29922",
  fib: "#a855f7",
  channel: "#58a6ff",
  rect: "#58a6ff",
  rectFill: "#58a6ff1f",
};

// Structured-model blend weight once MODEL_MIN_SAMPLES featured samples exist
// (gate itself lives in drawingModel.ts; mirrors the web side).
const MODEL_BLEND = 0.25;

// Above this bar count the exhaustive grid (324×4 walk-forward scorings) gets
// expensive enough to risk jank, so fall back to coordinate-descent search.
// Desktop kline windows are LIMITS-bound (max 300 for 1m, ≤200 otherwise), so
// the threshold sits at 250: 1m/300 takes the cheap coordinate path, all other
// timeframes keep the exhaustive full grid.
const COORDINATE_BARS = 250;

// Cache the self-tuned drawing params per symbol+timeframe so the grid-search
// result survives reloads ("remembers what worked").
const TUNE_KEY = (sym: string, tf: string) => `jarvis.draw.params.${sym}.${tf}`;
// Warm start: best params independent of bar count, survives streaming.
const WARM_KEY = (sym: string, tf: string) => `jarvis.draw.warm.${sym}.${tf}`;

function loadTunedParams(sym: string, tf: string, bars: number): DrawParams | null {
  try {
    const raw = localStorage.getItem(TUNE_KEY(sym, tf));
    if (!raw) return null;
    const cached = JSON.parse(raw) as { params: DrawParams; bars: number };
    return cached.bars === bars ? cached.params : null;
  } catch {
    return null;
  }
}

function saveTunedParams(sym: string, tf: string, params: DrawParams, score: number, bars: number) {
  try {
    localStorage.setItem(TUNE_KEY(sym, tf), JSON.stringify({ params, score, bars, ts: Date.now() }));
  } catch {
    /* localStorage unavailable — tuning still works, just not persisted */
  }
}

function loadWarmParams(sym: string, tf: string): DrawParams | null {
  try {
    const raw = localStorage.getItem(WARM_KEY(sym, tf));
    return raw ? (JSON.parse(raw) as DrawParams) : null;
  } catch {
    return null;
  }
}

function saveWarmParams(sym: string, tf: string, params: DrawParams): void {
  try {
    localStorage.setItem(WARM_KEY(sym, tf), JSON.stringify(params));
  } catch {
    /* storage unavailable — warm start simply won't persist */
  }
}

const ALL_DRAW_MODES: DrawMode[] = ["trend", "sr", "fib", "channel", "rect"];

/** 只删「盈损点」的 sig* 键，保留其它 query（与 pz* 区间图参数可并存互不干扰） */
function stripSigMarksQuery(q: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(q);
  for (const k of ["sigmarks", "sigtf", "sigside"]) next.delete(k);
  return next;
}

/** 预测覆盖的未来 bar 数（与预测引擎契约默认值一致） */
const PREDICT_HORIZON = 16;

export default function Chart() {
  const [tf, setTf] = useState<Timeframe>("15m");
  // 三档视图（简洁/进阶/专业），选择持久化；细粒度开关只在专业模式生效
  const [viewMode, setViewModeState] = useState<ViewMode>(() => loadViewMode());
  const setViewMode = (m: ViewMode) => {
    setViewModeState(m);
    saveViewMode(m);
  };
  const [legendOpen, setLegendOpen] = useState(false);
  const [smart, setSmart] = useState(true);
  const [draws, setDraws] = useState<Set<DrawMode>>(new Set());
  const [autoTune, setAutoTune] = useState(true);
  const [twelve, setTwelve] = useState(false);
  const [plan, setPlan] = useState(true);
  // 走势预测层（概率锥/路径/研判卡片）独立开关；默认关，避免干扰常规看盘
  const [predictOn, setPredictOn] = useState(false);
  const { symbol } = useSymbol();

  // ── 信号历史盈损标记（信号矩阵「盈损点」跳转携带 query 进入） ──
  // sigmarks=系统slug & sigtf=回测周期 & sigside=long|short
  const [searchParams, setSearchParams] = useSearchParams();
  const sigSystem = searchParams.get("sigmarks");
  const rawSigTf = searchParams.get("sigtf");
  const sigTf: Timeframe | null =
    rawSigTf && (TIMEFRAMES as readonly string[]).includes(rawSigTf)
      ? (rawSigTf as Timeframe)
      : null;
  const rawSigSide = searchParams.get("sigside");
  const sigSide: "long" | "short" | undefined =
    rawSigSide === "long" || rawSigSide === "short" ? rawSigSide : undefined;
  const clearSigMarks = () => {
    // 只清 sig* 键，保留可能并存的多空区间图参数
    setSearchParams(stripSigMarksQuery(searchParams), { replace: true });
  };

  // 进入/切换标记目标时自动对齐周期：胜率样本按 sigTf 回测，标记只有画在
  // 同周期 K 线上才与样本口径一致（任务要求：周期不一致自动切换）
  useEffect(() => {
    if (sigSystem && sigTf) setTf(sigTf);
  }, [sigSystem, sigTf]);

  // ── 信号多空区间图（信号矩阵「K线区间」跳转携带 pz* query 进入） ──
  // 三价 + 方向经 query 传递；几何非法（如多单 SL ≥ 入场）解析为 null 不画
  const zoneParams = useMemo(
    () => positionZoneFromQuery(searchParams),
    [searchParams],
  );
  const positionZone = useMemo(
    () => buildPositionZoneView(zoneParams),
    [zoneParams],
  );
  const clearPositionZone = () => {
    // 只清 pz* 键，保留可能并存的盈损标记参数
    setSearchParams(stripPositionZoneQuery(searchParams), { replace: true });
  };

  // 进入区间图时对齐信号周期（一次性：点位按该周期算出，画在同周期图上口径才对；
  // 之后用户可自由切走周期，区间图价格几何不随周期变化仍然成立）
  const zoneTf: Timeframe | null =
    zoneParams?.tf && (TIMEFRAMES as readonly string[]).includes(zoneParams.tf)
      ? (zoneParams.tf as Timeframe)
      : null;
  useEffect(() => {
    if (positionZone && zoneTf) setTf(zoneTf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoneTf, zoneParams?.entry, zoneParams?.side]);

  // 各数据源的实际启用条件：简洁/进阶忽略专业模式的细粒度开关
  const isPro = viewMode === "pro";
  const planActive = !isPro || plan;
  const smartActive = !isPro || smart;
  const twelveActive = viewMode === "advanced" || (isPro && twelve);

  const { data: rawKline, loading, error } = usePolling(
    () => api.kline(symbol, tf, LIMITS[tf]),
    tf === "1m" ? 10_000 : 60_000,
    [tf, symbol],
  );

  const { candles, volumes } = useMemo(() => {
    const rows = (rawKline as Record<string, unknown>)?.rows;
    if (!rawKline || !Array.isArray(rows)) {
      return { candles: [] as CandlestickData<Time>[], volumes: [] as HistogramData<Time>[] };
    }
    const c: CandlestickData<Time>[] = [];
    const v: HistogramData<Time>[] = [];
    for (const k of rows as Record<string, number>[]) {
      const time = (k.ts / 1000) as Time;
      c.push({
        time,
        open: k.o,
        high: k.h,
        low: k.l,
        close: k.c,
      });
      v.push({
        time,
        value: k.v ?? 0,
        color:
          k.c >= k.o
            ? "rgba(63, 185, 80, 0.3)"
            : "rgba(248, 81, 73, 0.3)",
      });
    }
    return { candles: c, volumes: v };
  }, [rawKline]);

  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null;

  // ── 实时价对齐：与顶栏同源的全局 ticker（10s），只认当前币种的报价 ──
  // kline 链路（前端 60s 轮询 + 后端 60s 缓存）最坏滞后约 2 分钟，图表最新价
  // 标签与顶栏价格会肉眼可见不一致；把 ticker 补进最后一根未收线蜡烛后两处
  // 同源同频。画线/信号等计算仍基于 kline 收线数据，不受此补丁影响。
  const livePrice = useLivePrice();
  const liveForChart = useMemo(
    () =>
      livePrice && livePrice.symbol === symbol
        ? { price: livePrice.price, timeSec: Math.floor(livePrice.at / 1000) }
        : null,
    [livePrice, symbol],
  );

  // 底部 OHLC 信息条与图表口径一致：收=实时价，高/低随之扩展
  const displayCandle = useMemo(() => {
    if (!lastCandle) return null;
    if (!liveForChart || liveForChart.timeSec < Number(lastCandle.time)) {
      return lastCandle;
    }
    const p = liveForChart.price;
    return {
      ...lastCandle,
      close: p,
      high: Math.max(lastCandle.high, p),
      low: Math.min(lastCandle.low, p),
    };
  }, [lastCandle, liveForChart]);

  // ── 信号盈损标记：拉该系统的逐笔回测明细（与信号矩阵聚合胜率同源） ──
  const { data: sigTradesResp, loading: sigTradesLoading } = useApi(
    () =>
      sigSystem && sigTf
        ? api.twelveSignalWinrateTrades(symbol, sigTf, sigSystem, sigSide)
        : Promise.resolve(null),
    [sigSystem, sigTf, sigSide, symbol],
  );

  // 样本 → K 线 L/S 徽章标记（仅当前周期 = 样本回测周期时映射；手动切走周期
  // 则暂不打标——4h 样本画在 15m 图上位置口径不对，会误导）。bar 高低点用于
  // 把徽章锚到影线外侧（多单挂低点下方 / 空单挂高点上方）。
  const sigMarks = useMemo(() => {
    if (!sigSystem || !sigTf || tf !== sigTf) return null;
    if (!sigTradesResp?.ok || !Array.isArray(sigTradesResp.trades) || candles.length === 0) {
      return null;
    }
    const fromSec = Number(candles[0].time);
    const toSec = Number(candles[candles.length - 1].time);
    const bars = new Map<number, { high: number; low: number }>();
    for (const c of candles) bars.set(Number(c.time), { high: c.high, low: c.low });
    return tradesToMarks(
      sigTradesResp.trades,
      sigTradesResp.name_cn || sigSystem,
      fromSec,
      toSec,
      bars,
    );
  }, [sigSystem, sigTf, tf, sigTradesResp, candles]);

  // Drawing-engine input arrays, derived once per kline refresh. `dates` is
  // index-aligned (the engine keys outputs by bar index, not by label).
  const baseData = useMemo<BaseData>(() => ({
    dates: candles.map((c) => String(c.time)),
    closes: candles.map((c) => c.close),
    highs: candles.map((c) => c.high),
    lows: candles.map((c) => c.low),
  }), [candles]);

  const toggleDraw = (id: DrawMode) => {
    setDraws((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // 参与计算的画线类型：专业=用户开关；进阶=全部 5 类（组合器再按可靠度裁剪）；
  // 简洁=不画自动画线
  const effectiveDraws = useMemo<Set<DrawMode>>(() => {
    if (viewMode === "pro") return draws;
    if (viewMode === "advanced") return new Set(ALL_DRAW_MODES);
    return new Set<DrawMode>();
  }, [viewMode, draws]);

  // ── 自调参数（Phase B/D-2）：网格搜索 + warm start，按 symbol+tf 缓存 ──
  const tuneInfo = useMemo(() => {
    if (!autoTune || effectiveDraws.size === 0 || baseData.closes.length < 40) {
      return { params: DEFAULT_PARAMS, score: 0, tuned: false };
    }
    const bars = baseData.closes.length;
    const strategy: "full" | "coordinate" = bars > COORDINATE_BARS ? "coordinate" : "full";

    const cached = loadTunedParams(symbol, tf, bars);
    if (cached) return { params: cached, score: 0, tuned: true };

    const warm = loadWarmParams(symbol, tf);
    if (warm) {
      const ev = evaluateParams(baseData, warm);
      if (ev.uplift > 0) {
        saveTunedParams(symbol, tf, warm, ev.score, bars);
        return { params: warm, score: ev.score, tuned: true };
      }
      const res = gridSearchParams(baseData, { seed: warm, strategy });
      saveWarmParams(symbol, tf, res.params);
      saveTunedParams(symbol, tf, res.params, res.score, bars);
      return { params: res.params, score: res.score, tuned: true };
    }

    const res = gridSearchParams(baseData, { strategy });
    saveWarmParams(symbol, tf, res.params);
    saveTunedParams(symbol, tf, res.params, res.score, bars);
    return { params: res.params, score: res.score, tuned: true };
  }, [autoTune, effectiveDraws.size, baseData, symbol, tf]);

  // ── 命中率（tuned vs 默认参数基线，同一验证段） ──
  const modeScores = useMemo(() => {
    if (!tuneInfo.tuned || baseData.closes.length < 40) return null;
    return scoreDrawings(baseData, tuneInfo.params);
  }, [tuneInfo, baseData]);

  const baseScores = useMemo(() => {
    if (!tuneInfo.tuned || baseData.closes.length < 40) return null;
    return scoreDrawings(baseData, DEFAULT_PARAMS);
  }, [tuneInfo.tuned, baseData]);

  const reliability = useMemo(() => {
    if (!modeScores || effectiveDraws.size === 0) return undefined;
    const map: Partial<Record<DrawMode, number>> = {};
    for (const mode of effectiveDraws) map[mode] = modeScores[mode].hitRate;
    return map;
  }, [modeScores, effectiveDraws]);

  const weightedHitRate = (scores: ReturnType<typeof scoreDrawings> | null) => {
    if (!scores || effectiveDraws.size === 0) return null;
    let wSum = 0;
    let acc = 0;
    for (const mode of effectiveDraws) {
      const s = scores[mode];
      const w = Math.min(s.touches, 20);
      acc += s.hitRate * w;
      wSum += w;
    }
    return wSum > 0 ? acc / wSum : null;
  };

  const activeHitRate = useMemo(() => weightedHitRate(modeScores), [modeScores, effectiveDraws]);
  const baselineHitRate = useMemo(() => weightedHitRate(baseScores), [baseScores, effectiveDraws]);
  const uplift =
    activeHitRate !== null && baselineHitRate !== null
      ? activeHitRate - baselineHitRate
      : null;

  // ── Phase A · 真闭环：把每次验证结果落进 per-symbol 学习日志 ──
  // 去重键用「最后一根 K 线的时间戳」而非 bar 数：桌面端 kline 接口返回固定窗口
  // （bar 数恒等于 LIMITS[tf]），若按 bar 数去重则每个 mode 永远只剩 1 条样本，
  // D-3 情境模型（≥ MODEL_MIN_SAMPLES）永远无法激活。按最后一根 K 线时间去重，
  // 每收一根新 K 线累积一条样本，同一根 K 线内的轮询刷新保持幂等。
  const [logVersion, setLogVersion] = useState(0);
  const logKey = `${symbol}.${tf}`;
  const lastBarKey = baseData.dates.length > 0 ? Number(baseData.dates[baseData.dates.length - 1]) || 0 : 0;
  useEffect(() => {
    if (!modeScores || !baseScores || effectiveDraws.size === 0 || lastBarKey <= 0) return;
    const now = Date.now();
    const samples: DrawingSample[] = [];
    for (const mode of effectiveDraws) {
      const s = modeScores[mode];
      const b = baseScores[mode];
      samples.push({
        ts: now,
        bars: lastBarKey,
        mode,
        touches: s.touches,
        hits: s.hits,
        hitRate: s.hitRate,
        baselineHitRate: b.hitRate,
        uplift: s.hitRate - b.hitRate,
        features: extractFeatures(baseData, s.touches),
      });
    }
    appendLog(logKey, samples);
    setLogVersion((v) => v + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logKey, modeScores, baseScores, effectiveDraws, lastBarKey]);

  const logSamples = useMemo(() => {
    void logVersion;
    return loadLog(logKey);
  }, [logKey, logVersion]);

  const logSummary = useMemo(() => summarize(logSamples), [logSamples]);

  // ── Phase D-3 · 市场情境模型（样本足够才发言） ──
  const contextModel = useMemo(() => {
    const data = buildTrainingSet(logSamples);
    if (data.length < MODEL_MIN_SAMPLES) return null;
    return trainModel(data);
  }, [logSamples]);

  // ── Phase D · 在线学习：live 命中率 × 历史均值 × 情境预测 → 线条强调度 ──
  const learnedReliability = useMemo(() => {
    if (!reliability) return undefined;
    const clamp01 = (x: number) => (x < 0 ? 0 : x > 1 ? 1 : x);
    const map: Partial<Record<DrawMode, number>> = {};
    for (const mode of effectiveDraws) {
      let r = blendReliability(reliability[mode] ?? 0, logSummary.perMode[mode]);
      if (contextModel && modeScores) {
        const p = predictProba(contextModel, extractFeatures(baseData, modeScores[mode].touches));
        r = (1 - MODEL_BLEND) * r + MODEL_BLEND * p;
      }
      map[mode] = clamp01(r);
    }
    return map;
  }, [reliability, logSummary, effectiveDraws, contextModel, modeScores, baseData]);

  // ── 画线引擎输出（专业模式全量 payload；简洁/进阶由组合器另行裁剪） ──
  const drawings = useMemo(() => {
    if (!isPro || draws.size === 0 || candles.length < 20) return null;
    return computeDrawings(draws, baseData, DRAW_COLORS, tuneInfo.params, learnedReliability);
  }, [isPro, draws, baseData, candles.length, tuneInfo.params, learnedReliability]);

  // 进阶模式：每类线型单独计算，供组合器按可靠度裁剪（每类只保留最可靠的 1~2 条）
  const perTypeDrawings = useMemo(() => {
    if (viewMode !== "advanced" || candles.length < 20) return null;
    const map: Partial<Record<DrawMode, DrawingResult>> = {};
    for (const m of ALL_DRAW_MODES) {
      map[m] = computeDrawings(new Set([m]), baseData, DRAW_COLORS, tuneInfo.params, learnedReliability);
    }
    return map;
  }, [viewMode, baseData, candles.length, tuneInfo.params, learnedReliability]);

  // ── 智能视图（最近强支撑/压力 + 现价），复用自调后的参数 ──
  const smartLevels = useMemo(() => {
    if (!smartActive || candles.length < 20) return null;
    return computeSmartLevels(baseData, tuneInfo.params);
  }, [smartActive, baseData, candles.length, tuneInfo.params]);

  // A · 纯几何方向：现价相对支撑/压力的位置 → 偏多/偏空/观望（双向）
  const smartBias = useMemo<SmartBias | null>(
    () => (smartLevels ? computeBias(smartLevels) : null),
    [smartLevels],
  );

  // ── 十二套技术体系信号（接口未就绪时优雅降级为空） ──
  // 原始 signals 交给组合器按视图模式过滤/去重成关键位。
  // failed = 后端返回封套 ok:false（信号源取数失败）；unavailable = 请求本身抛错（接口未就绪）
  const [twelveSignals, setTwelveSignals] = useState<TwelveSignal[]>([]);
  const [twelveState, setTwelveState] = useState<"idle" | "loading" | "ok" | "failed" | "unavailable">("idle");

  useEffect(() => {
    if (!twelveActive) {
      setTwelveSignals([]);
      setTwelveState("idle");
      return;
    }
    let cancelled = false;
    setTwelveState("loading");
    (async () => {
      try {
        const res = await api.twelveSignals(symbol, tf);
        if (cancelled) return;
        // 回声校验：慢返回的旧币种响应不得写入当前币种的图（防交易误导）
        if (isStaleEcho(symbol, res?.symbol)) return;
        if (res?.ok === false) {
          // 封套显式失败（HTTP 200 + ok:false）：区别于"暂无关键位"，标记取数失败
          setTwelveSignals([]);
          setTwelveState("failed");
          return;
        }
        setTwelveSignals(Array.isArray(res?.signals) ? res.signals : []);
        setTwelveState("ok");
      } catch {
        if (cancelled) return;
        // 接口未就绪 / 网络失败 → 空数组降级，UI 不崩
        setTwelveSignals([]);
        setTwelveState("unavailable");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [twelveActive, symbol, tf]);

  // ── 交易计划线（共识 trade_plan → 入场区/止损/止盈 priceLine） ──
  // 轮询 90s（后端 /twelve/consensus 有 180s 缓存，成本低），与 Dashboard 同步。
  // 状态语义：
  //   none-neutral     = 共识中性/分歧，本来就无计划 →「无计划（中性）」
  //   none-directional = 有方向但无同向系统计划（后端宁缺毋滥）→「无可执行计划」
  //   failed = 封套 ok:false；unavailable = 请求抛错（接口未就绪）
  const {
    data: planRes,
    loading: planLoading,
    error: planError,
  } = usePolling(
    () => (planActive ? api.twelveConsensus(symbol) : Promise.resolve(null)),
    planActive ? 90_000 : 0,
    [planActive, symbol],
  );

  const planInfo = useMemo<{
    tradePlan: ConsensusTradePlan | null;
    direction: string;
    state: "idle" | "loading" | "ok" | "none-neutral" | "none-directional" | "failed" | "unavailable";
    meta: string;
    /** 无计划原因（后端 v3 plan_status：RR 不达标/结构不支持等观望说明） */
    watchReason: string;
  }>(() => {
    if (!planActive) return { tradePlan: null, direction: "neutral", state: "idle", meta: "", watchReason: "" };
    if (planError) return { tradePlan: null, direction: "neutral", state: "unavailable", meta: "", watchReason: "" };
    if (!planRes) return { tradePlan: null, direction: "neutral", state: planLoading ? "loading" : "unavailable", meta: "", watchReason: "" };
    // 回声校验：usePolling 无请求取消，快速切币种时旧币种的慢响应可能后到——
    // symbol 对不上一律不采用（旧币种计划线画到新币种图上会造成交易误导）
    if (isStaleEcho(symbol, planRes.symbol)) {
      return { tradePlan: null, direction: "neutral", state: "loading", meta: "", watchReason: "" };
    }
    if (planRes.ok === false) return { tradePlan: null, direction: "neutral", state: "failed", meta: "", watchReason: "" };
    // 旧后端无 trade_plan 字段 / 中性无计划 → null
    const tp = planRes.consensus?.trade_plan ?? null;
    const direction = planRes.consensus?.direction ?? "neutral";
    const overlay = planToOverlay(tp);
    if (overlay.hlines.length === 0) {
      return {
        tradePlan: null,
        direction,
        state: direction === "neutral" ? "none-neutral" : "none-directional",
        meta: "",
        watchReason: planRes.consensus?.plan_status?.reason ?? "",
      };
    }
    const bits: string[] = [];
    if (tp?.rr != null && Number.isFinite(tp.rr)) {
      const gate = tp?.min_rr != null && Number.isFinite(tp.min_rr) ? `(≥${Number(tp.min_rr).toFixed(1)})` : "";
      bits.push(`RR ${Number(tp.rr).toFixed(1)}${gate}`);
    }
    if (tp?.position_pct != null && Number.isFinite(tp.position_pct)) bits.push(`仓位 ${tp.position_pct}%`);
    if (tp?.source_tf) bits.push(`级别 ${tp.source_tf}`);
    if (tp?.basis?.length) bits.push(`依据 ${tp.basis.slice(0, 3).join("/")}`);
    if (tp?.sl_basis) bits.push(`止损锚定 ${tp.sl_basis}`);
    return { tradePlan: tp, direction, state: "ok", meta: bits.join(" · "), watchReason: "" };
  }, [planActive, planRes, planLoading, planError, symbol]);

  // ── 走势预测（预测引擎 GET /api/predict；未就绪/失败回退本地演示推演）──
  // 开启开关 / 切 symbol / 切周期时重新拉取；bars 从无到有时补拉一次（mock
  // 推演依赖 K 线）。K 线常规轮询刷新不重拉——overlay 依 generatedAt 锚定，
  // 新 K 线收线也不漂移。predictSeq 供卡片「重试」手动触发。
  const [predictResp, setPredictResp] = useState<PredictResponse | null>(null);
  const [predictLoading, setPredictLoading] = useState(false);
  const [predictError, setPredictError] = useState<string | null>(null);
  const [predictSeq, setPredictSeq] = useState(0);
  const hasBars = candles.length > 0;

  useEffect(() => {
    if (!predictOn) {
      setPredictResp(null);
      setPredictError(null);
      setPredictLoading(false);
      return;
    }
    let cancelled = false;
    setPredictLoading(true);
    setPredictError(null);

    // 引擎不可用（接口未部署/取数失败）→ 本地 mock 推演兜底（卡片带「演示
    // 数据」角标）；K 线也不足时才落失败态
    const fallbackToMock = (reason?: string) => {
      const bars: PredictBar[] = candles.map((c) => ({
        timeSec: Number(c.time),
        close: c.close,
        high: c.high,
        low: c.low,
      }));
      const mock = mockPredict(symbol, tf, PREDICT_HORIZON, bars);
      if (mock) {
        setPredictResp(mock);
        setPredictError(null);
      } else {
        setPredictResp(null);
        setPredictError(reason ?? "预测接口未就绪，K 线数据也不足以生成演示推演");
      }
      setPredictLoading(false);
    };

    (async () => {
      try {
        const res = await api.predict(symbol, tf, PREDICT_HORIZON);
        if (cancelled) return;
        // 回声校验：慢返回的旧币种/旧周期响应不得写入当前图（防交易误导）
        if (isStaleEcho(symbol, res?.symbol)) return;
        if (res?.timeframe && res.timeframe !== tf) return;
        if (res && res.ok !== false && Array.isArray(res.path)) {
          setPredictResp(res);
          setPredictError(null);
          setPredictLoading(false);
          return;
        }
        fallbackToMock(res?.error);
      } catch {
        if (!cancelled) fallbackToMock();
      }
    })();
    return () => {
      cancelled = true;
    };
    // candles 内容故意不进依赖：常规轮询刷新不重拉预测（见上方注释）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [predictOn, symbol, tf, hasBars, predictSeq]);

  // 响应 → 图表载荷（bar 逻辑索引 + 价格）；K 线轮询增长时重算投影，
  // anchor 依 generatedAt 锚定在生成时刻的 bar 上，不随新 K 线漂移
  const predictOverlay = useMemo(() => {
    if (!predictOn || !predictResp || candles.length === 0) return null;
    const bars: PredictBar[] = candles.map((c) => ({
      timeSec: Number(c.time),
      close: c.close,
      high: c.high,
      low: c.low,
    }));
    return buildPredictionOverlay(predictResp, bars);
  }, [predictOn, predictResp, candles]);

  // ── [M2 s5] 磁吸位叠加：清算/止损密集区水平线（priceLine，复用 keyLevels 通道）──
  const [liqOn, setLiqOn] = useState(false);
  const { data: liqMap } = usePolling(
    () => (liqOn ? api.liqMap(symbol, "15m") : Promise.resolve(null)),
    120_000,
    [liqOn, symbol],
  );
  const liqLevels: KeyLevel[] = useMemo(() => {
    const d = liqOn ? (liqMap as LiqMapResponse | null) : null;
    if (!d?.ok || !d.magnets) return [];
    // 只画强度 ≥0.4 的簇，避免线太多糊图；标签带类型缩写与强度
    const kindTag = { long_liq: "多清", short_liq: "空清", stop_cluster: "止损" };
    return d.magnets
      .filter((m) => m.strength >= 0.4)
      .slice(0, 8)
      .map((m) => ({
        label: `🧲${kindTag[m.kind]} ${(m.strength * 100).toFixed(0)}%`,
        price: m.price_mid,
      }));
  }, [liqOn, liqMap]);

  // ── Delta/CVD 订单流副图（「安全带」层）：引擎 GET /api/delta，未就绪时
  // 回退 K 线本地演示推演（角标标注），与预测层同一套降级模式 ──
  const [deltaOn, setDeltaOn] = useState(false);
  const [deltaResp, setDeltaResp] = useState<DeltaResponse | null>(null);
  const [deltaLoading, setDeltaLoading] = useState(false);
  const [deltaError, setDeltaError] = useState<string | null>(null);

  useEffect(() => {
    if (!deltaOn) {
      setDeltaResp(null);
      setDeltaError(null);
      setDeltaLoading(false);
      return;
    }
    let cancelled = false;
    setDeltaLoading(true);
    setDeltaError(null);

    const klines = (): DeltaKline[] =>
      candles.map((c, i) => ({
        timeSec: Number(c.time),
        open: c.open,
        close: c.close,
        high: c.high,
        low: c.low,
        volume: volumes[i]?.value,
      }));

    const fallbackToMock = (reason?: string) => {
      const mock = mockDelta(symbol, tf, klines());
      if (mock) {
        setDeltaResp(mock);
        setDeltaError(null);
      } else {
        setDeltaResp(null);
        setDeltaError(reason ?? "Delta 引擎未就绪，K 线数据也不足以生成演示推演");
      }
      setDeltaLoading(false);
    };

    (async () => {
      try {
        const res = await api.delta(symbol, tf, 200);
        if (cancelled) return;
        // 回声校验：慢返回的旧币种/旧周期响应不得写入当前图
        if (isStaleEcho(symbol, res?.symbol)) return;
        if (res?.timeframe && res.timeframe !== tf) return;
        if (res && res.ok !== false && Array.isArray(res.bars) && res.bars.length > 0) {
          // 引擎 bars[].t / anchors[].t 为 ISO 字符串，图表需要 unix 秒——
          // 不归一会让 lightweight-charts 按 yyyy-mm-dd 解析而崩溃
          const norm = normalizeDeltaResponse(res);
          if (norm && norm.bars.length > 0) {
            setDeltaResp(norm);
            setDeltaError(null);
            setDeltaLoading(false);
            return;
          }
        }
        fallbackToMock(res?.error);
      } catch {
        if (!cancelled) fallbackToMock();
      }
    })();
    return () => {
      cancelled = true;
    };
    // candles 内容故意不进依赖：常规轮询刷新不重拉（与预测层同策略）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deltaOn, symbol, tf, hasBars]);

  // ── 三档视图组合：把 计划/智能S·R/画线/关键位 裁剪成最终渲染载荷 + 图例 ──
  const composition = useMemo<ChartComposition>(
    () =>
      composeChartView({
        mode: viewMode,
        tradePlan: planInfo.tradePlan ?? null,
        planDirection: planInfo.direction,
        smart: smartLevels,
        fullDrawings: drawings,
        perTypeDrawings,
        reliability: learnedReliability,
        price: lastCandle?.close ?? 0,
        signals: twelveSignals,
        twelveOn: twelve,
      }),
    [viewMode, planInfo, smartLevels, drawings, perTypeDrawings, learnedReliability, lastCandle, twelveSignals, twelve],
  );

  // B · AI 决策方向：拉 /actions/brief 的 偏多/偏空/中性（含信心分、建议仓位）
  type AiDir = { label: string; dir: "long" | "short" | "neutral"; score: number; pos: number };
  const [aiDir, setAiDir] = useState<AiDir | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);

  // Stale once the symbol changes — clear the previous symbol's decision.
  useEffect(() => {
    setAiDir(null);
    setAiErr(null);
  }, [symbol]);

  const loadBrief = async () => {
    setAiLoading(true);
    setAiErr(null);
    try {
      const res = (await api.actionBrief(symbol)) as {
        ok?: boolean;
        data?: { decision?: Record<string, unknown> };
        error?: string;
      };
      const dec = res?.data?.decision;
      if (!dec) {
        setAiErr(res?.error ?? "无决策数据");
        setAiDir(null);
        return;
      }
      const direction = String(dec.direction ?? "");
      const dir: AiDir["dir"] = direction.startsWith("偏多")
        ? "long"
        : direction.startsWith("偏空")
          ? "short"
          : "neutral";
      setAiDir({
        label: direction || "中性观望",
        dir,
        score: Number(dec.conviction_score ?? 0),
        pos: Number(dec.suggested_position_pct ?? 0),
      });
    } catch (e) {
      setAiErr(e instanceof Error ? e.message : String(e));
      setAiDir(null);
    } finally {
      setAiLoading(false);
    }
  };

  const biasCls = (dir: string) =>
    clsx(
      "px-2 py-1 rounded text-sm font-medium border",
      dir === "short"
        ? "text-jarvis-red border-jarvis-red"
        : dir === "long"
          ? "text-jarvis-green border-jarvis-green"
          : "text-jarvis-text-secondary border-jarvis-border",
    );

  const pillCls = (active: boolean) =>
    clsx(
      "px-2.5 py-1 text-xs rounded-md border transition-colors",
      active
        ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
        : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
    );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <CandlestickChart size={22} />
          {symbol.replace("USDT", "/USDT")}
        </h1>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {isPro && (
            <button
              onClick={() => setSmart((v) => !v)}
              title="智能：在图上标注离现价最近的压力位、支撑位和现价，一眼看懂"
              className={clsx(
                "px-3 py-1 text-sm rounded-md border transition-colors",
                smart
                  ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                  : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              智能 {smart ? "·开" : "·关"}
            </button>
          )}

          {/* A · 几何方向（双向，含明确做空提示），随智能视图自动出 */}
          {smartActive && smartBias && (
            <span className={biasCls(smartBias.dir)} title={smartBias.detail}>
              {smartBias.dir === "short" ? "▼ " : smartBias.dir === "long" ? "▲ " : "= "}
              {smartBias.label} · {smartBias.detail}
            </span>
          )}

          {/* B · AI 决策方向（按需拉 brief，含偏空） */}
          <button
            onClick={loadBrief}
            disabled={aiLoading}
            title="拉取 AI 决策简报：偏多 / 偏空 / 中性观望（含信心分与建议仓位）"
            className={clsx(
              "px-3 py-1 text-sm rounded-md border transition-colors",
              "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              aiLoading && "opacity-60 cursor-wait",
            )}
          >
            {aiLoading ? "AI 决策…" : "AI 决策"}
          </button>
          {aiDir && (
            <span className={biasCls(aiDir.dir)} title={`AI 决策：${aiDir.label}`}>
              {aiDir.dir === "short" ? "▼ " : aiDir.dir === "long" ? "▲ " : "= "}
              {aiDir.label} · 信心 {aiDir.score} · 仓位 {aiDir.pos}%
            </span>
          )}
          {aiErr && (
            <span className="px-2 py-1 rounded text-sm text-jarvis-yellow" title={aiErr}>
              AI 决策失败
            </span>
          )}

          <div className="flex gap-1 bg-jarvis-card border border-jarvis-border rounded-lg p-1">
            {TIMEFRAMES.map((t) => (
              <button
                key={t}
                onClick={() => setTf(t)}
                className={clsx(
                  "px-3 py-1 text-sm rounded-md transition-colors",
                  t === tf
                    ? "bg-jarvis-blue text-jarvis-accent-fg"
                    : "text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                {t}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 视图工具栏：三档模式（简洁/进阶/专业）+ 图例；细粒度开关仅专业模式显示 */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="flex gap-1 bg-jarvis-card border border-jarvis-border rounded-lg p-1">
          {VIEW_MODES.map((m) => (
            <button
              key={m.id}
              onClick={() => setViewMode(m.id)}
              title={m.hint}
              className={clsx(
                "px-3 py-1 text-sm rounded-md transition-colors",
                viewMode === m.id
                  ? "bg-jarvis-blue text-jarvis-accent-fg"
                  : "text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              {m.label}
            </button>
          ))}
        </div>

        {/* 预测层：概率锥 + 路径虚线画在 K 线右侧未来区域 + 图上方研判卡片 */}
        <button
          onClick={() => setPredictOn((v) => !v)}
          title="AI 走势预测：在 K 线右侧未来区域画预测路径（虚线）与目标区间（概率锥），并给出方向概率与研判理由。预测仅供参考，不构成投资建议"
          className={pillCls(predictOn)}
        >
          预测{predictOn ? "·开" : "·关"}
        </button>

        {/* Delta/CVD 副图（「安全带」层）：只有 Delta 与价格背离（吸收证据）才是真反转 */}
        <button
          onClick={() => setDeltaOn((v) => !v)}
          title="Delta/CVD 订单流副图：每根主动买卖差（正绿负红）+ CVD 累计曲线；价格创新低但 CVD 抬高 = 吸收背离（安全带确认信号）。引擎未就绪时显示演示推演"
          className={pillCls(deltaOn)}
        >
          Delta{deltaOn ? "·开" : "·关"}
        </button>

        {/* [M2 s5] 磁吸位：清算/止损密集区水平线（庄家扫单/插针目标位预判） */}
        <button
          onClick={() => setLiqOn((v) => !v)}
          title="磁吸位叠加：清算簇（多/空爆仓密集触发区）与止损/整数关口聚集区的水平线。价格倾向被吸向流动性密集处——接近强磁吸位时警惕扫单插针。估算模型：VP 入场分布 × 常见杠杆档 + 摆动点/关口，forceOrder 实时校准"
          className={pillCls(liqOn)}
        >
          磁吸位{liqOn ? "·开" : "·关"}
        </button>

        {/* 图例：解释当前模式下每类线的含义 */}
        <div className="relative">
          <button
            onClick={() => setLegendOpen((v) => !v)}
            title="解释图上每类线代表什么"
            className={clsx(
              "flex items-center gap-1 px-2.5 py-1 text-xs rounded-md border transition-colors",
              legendOpen
                ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
            )}
          >
            <HelpCircle size={13} />
            这些线是什么？
          </button>
          {legendOpen && (
            <div
              className="absolute top-full left-0 mt-1 z-50 bg-jarvis-card border border-jarvis-border rounded-lg shadow-lg p-3 w-80"
              onMouseLeave={() => setLegendOpen(false)}
            >
              {composition.legend.length === 0 ? (
                <p className="text-xs text-jarvis-text-secondary">当前没有叠加线（等待数据或计划生成）</p>
              ) : (
                <div className="space-y-2">
                  {composition.legend.map((e) => (
                    <div key={e.name} className="flex items-start gap-2">
                      <span
                        className="mt-1.5 inline-block w-5 shrink-0"
                        style={{
                          borderTop: `2px ${e.dashed ? "dashed" : "solid"} ${e.color}`,
                        }}
                      />
                      <div className="min-w-0">
                        <p className="text-xs text-jarvis-text font-medium">{e.name}</p>
                        <p className="text-xs text-jarvis-text-secondary">{e.explain}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <p className="text-[10px] text-jarvis-text-secondary mt-2 pt-2 border-t border-jarvis-border">
                当前共 {composition.lineCount} 条线 · {VIEW_MODES.find((m) => m.id === viewMode)?.hint}
              </p>
            </div>
          )}
        </div>

        {isPro && (
          <>
            <div className="w-px h-4 bg-jarvis-border" />
            <span className="text-xs text-jarvis-text-secondary">自动画线</span>
            {DRAW_OPTIONS.map((o) => (
              <button
                key={o.id}
                onClick={() => toggleDraw(o.id)}
                title={`自动${o.label}：随 K 线增长实时重算`}
                className={pillCls(draws.has(o.id))}
              >
                {o.label}
              </button>
            ))}
          </>
        )}
        {isPro && draws.size > 0 && (
          <>
            <button
              onClick={() => setAutoTune((v) => !v)}
              title="自调：用历史 K 线回测，自动选命中率最高的画线参数（越画越准）"
              className={clsx(
                "px-2.5 py-1 text-xs rounded-md border transition-colors",
                autoTune
                  ? "bg-jarvis-green/15 border-jarvis-green text-jarvis-green"
                  : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              自调{autoTune ? "·开" : "·关"}
            </button>
            {autoTune && activeHitRate !== null && (
              <span
                title={
                  (uplift !== null && baselineHitRate !== null
                    ? `自调命中率 ${(activeHitRate * 100).toFixed(0)}% vs 默认参数 ${(baselineHitRate * 100).toFixed(0)}%（历史验证段）`
                    : "当前画线在历史验证段的命中率") +
                  (logSummary.count > 0
                    ? `\n累计样本 ${logSummary.count} 条 · 平均提升 ${logSummary.avgUplift >= 0 ? "+" : ""}${(logSummary.avgUplift * 100).toFixed(1)}pp`
                    : "")
                }
                className={clsx(
                  "px-2 py-1 rounded text-xs font-mono border border-jarvis-border",
                  activeHitRate >= 0.6
                    ? "text-jarvis-green"
                    : activeHitRate >= 0.4
                      ? "text-jarvis-yellow"
                      : "text-jarvis-text-secondary",
                )}
              >
                命中 {(activeHitRate * 100).toFixed(0)}%
                {uplift !== null && baselineHitRate !== null && (
                  <span
                    className={clsx(
                      "ml-1",
                      uplift > 0.0005
                        ? "text-jarvis-green"
                        : uplift < -0.0005
                          ? "text-jarvis-yellow"
                          : "text-jarvis-text-secondary",
                    )}
                  >
                    {uplift > 0.0005 ? "▲+" : uplift < -0.0005 ? "▼" : "±"}
                    {(Math.abs(uplift) * 100).toFixed(0)}pp（默认 {(baselineHitRate * 100).toFixed(0)}%）
                  </span>
                )}
              </span>
            )}
            {autoTune && logSummary.count > 0 && (
              <button
                onClick={() => {
                  clearLog(logKey);
                  setLogVersion((v) => v + 1);
                }}
                title={`累计学习样本 ${logSummary.count} 条（点击重置本标的学习记录）`}
                className="px-1.5 py-1 rounded text-xs font-mono text-jarvis-text-secondary hover:text-jarvis-red transition-colors"
              >
                ↺{logSummary.count}
              </button>
            )}
            <button
              onClick={() => setDraws(new Set())}
              className="px-2 py-1 rounded text-xs text-jarvis-text-secondary hover:text-jarvis-red transition-colors"
            >
              清除
            </button>
          </>
        )}

        {isPro && (
          <>
            <div className="w-px h-4 bg-jarvis-border" />
            <button
              onClick={() => setTwelve((v) => !v)}
              title="十二套技术体系的关键价位（趋势/动量/量价等信号系统输出）叠加到图上"
              className={pillCls(twelve)}
            >
              十二套关键位{twelve ? "·开" : "·关"}
            </button>
          </>
        )}
        {twelveActive && twelveState === "loading" && (
          <span className="text-xs text-jarvis-text-secondary">加载中…</span>
        )}
        {twelveActive && twelveState === "ok" && (
          <span className="text-xs text-jarvis-text-secondary">
            {composition.keyLevels.length > 0
              ? `${composition.keyLevels.length} 个关键位`
              : viewMode === "advanced"
                ? "暂无强信号关键位"
                : "暂无关键位"}
          </span>
        )}
        {twelveActive && twelveState === "failed" && (
          <span className="text-xs text-jarvis-yellow" title="后端信号引擎返回 ok:false（K线取数或计算失败），稍后自动重试">
            信号源取数失败
          </span>
        )}
        {twelveActive && twelveState === "unavailable" && (
          <span className="text-xs text-jarvis-yellow" title="GET /api/twelve/signals 不可用，可能后端尚未部署该接口">
            信号接口未就绪
          </span>
        )}

        {isPro && (
          <>
            <div className="w-px h-4 bg-jarvis-border" />
            <button
              onClick={() => setPlan((v) => !v)}
              title="共识交易计划：把入场区、止损、止盈价位直接画到 K 线图上"
              className={pillCls(plan)}
            >
              交易计划{plan ? "·开" : "·关"}
            </button>
          </>
        )}
        {planActive && planInfo.state === "loading" && (
          <span className="text-xs text-jarvis-text-secondary">加载中…</span>
        )}
        {planActive && planInfo.state === "ok" && planInfo.meta && (
          <span className="text-xs text-jarvis-text-secondary" title="共识计划参数（依据十二套系统汇总）">
            {planInfo.meta}
          </span>
        )}
        {planActive && planInfo.state === "none-neutral" && (
          <span className="text-xs text-jarvis-text-secondary" title="共识为中性或分歧，本来就没有交易计划">
            无计划（中性）
          </span>
        )}
        {planActive && planInfo.state === "none-directional" && (
          <span
            className="text-xs text-jarvis-yellow"
            title={planInfo.watchReason || "共识有方向，但结构/盈亏比不达标（后端宁缺毋滥，不硬造）"}
          >
            {planInfo.watchReason ? `观望：${planInfo.watchReason}` : "无可执行计划"}
          </span>
        )}
        {planActive && planInfo.state === "failed" && (
          <span className="text-xs text-jarvis-yellow" title="后端共识引擎返回 ok:false（取数或计算失败），稍后自动重试">
            计划源取数失败
          </span>
        )}
        {planActive && planInfo.state === "unavailable" && (
          <span className="text-xs text-jarvis-yellow" title="GET /api/twelve/consensus 不可用，可能后端尚未部署该接口">
            计划接口未就绪
          </span>
        )}
      </div>

      {/* 信号盈损标记状态条：来自信号矩阵「盈损点」跳转，标出该信号每笔历史盈亏 */}
      {sigSystem && (
        <div className="flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border border-jarvis-blue/40 rounded-lg px-3 py-2">
          <Target size={13} className="text-jarvis-blue shrink-0" />
          <span className="text-jarvis-text font-medium">
            「{sigTradesResp?.name_cn || sigSystem}」历史盈损标记
          </span>
          {sigSide && (
            <span
              className={clsx(
                "px-1.5 py-px rounded text-[10px] font-medium text-white",
                sigSide === "long" ? "bg-jarvis-green" : "bg-jarvis-red",
              )}
            >
              {sigSide === "long" ? "做多信号" : "做空信号"}
            </span>
          )}
          {sigTf && tf !== sigTf ? (
            <span className="flex items-center gap-1 text-jarvis-yellow">
              样本按 {sigTf} 周期回测，当前 {tf} 周期不展示标记
              <button
                onClick={() => setTf(sigTf)}
                className="px-1.5 py-px rounded border border-jarvis-yellow/50 hover:bg-jarvis-yellow/10 transition-colors"
              >
                切回 {sigTf}
              </button>
            </span>
          ) : sigTradesLoading ? (
            <span className="text-jarvis-text-secondary">加载逐笔明细…</span>
          ) : sigTradesResp && !sigTradesResp.ok ? (
            <span className="text-jarvis-yellow">
              {sigTradesResp.need_run
                ? "该周期暂无逐笔明细——回总览页信号矩阵点「胜率回测」跑一次后再来"
                : sigTradesResp.error ?? "明细获取失败"}
            </span>
          ) : sigMarks ? (
            <span className="text-jarvis-text-secondary">
              当前窗口 {sigMarks.visible}/{sigMarks.total} 笔 · 徽章=入场（
              <span className="text-jarvis-green">绿 L 多</span> /
              <span className="text-jarvis-red"> 红 S 空</span>
              ），角标 ✓盈 ✕亏 · 圆点=出场 · 悬停看每笔「入场→出场」详情
            </span>
          ) : null}
          <button
            onClick={clearSigMarks}
            title="清除盈损标记"
            className="ml-auto flex items-center gap-0.5 px-1.5 py-0.5 rounded text-jarvis-text-secondary hover:text-jarvis-red transition-colors"
          >
            <X size={12} />
            清除
          </button>
        </div>
      )}

      {/* 多空区间图状态条：来自信号矩阵「K线区间」跳转，TradingView position 风格 */}
      {zoneParams && (
        <div className="flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border border-jarvis-purple/40 rounded-lg px-3 py-2">
          <Target size={13} className="text-jarvis-purple shrink-0" />
          <span className="text-jarvis-text font-medium">
            「{zoneParams.name || "信号计划"}」多空区间图
          </span>
          <span
            className={clsx(
              "px-1.5 py-px rounded text-[10px] font-medium text-white",
              zoneParams.side === "long" ? "bg-jarvis-green" : "bg-jarvis-red",
            )}
          >
            {zoneParams.side === "long" ? "做多" : "做空"}
          </span>
          {positionZone ? (
            <span className="text-jarvis-text-secondary font-mono">
              入 {formatPrice(positionZone.entry)} · 损{" "}
              <span className="text-jarvis-red">{formatPrice(positionZone.stopLoss)}</span> · 盈{" "}
              <span className="text-jarvis-green">{formatPrice(positionZone.takeProfit)}</span>
              {" "}· 盈亏比 1:{positionZone.rr}
              <span className="ml-1 text-jarvis-text-secondary/80">
                （<span className="text-jarvis-green">绿块=盈利目标区</span>、
                <span className="text-jarvis-red">红块=止损风险区</span>，向右延伸为持仓预期）
              </span>
            </span>
          ) : (
            <span className="text-jarvis-yellow">
              点位几何不合法（方向与止损/止盈位置矛盾），不绘制区间
            </span>
          )}
          {zoneTf && tf !== zoneTf && (
            <button
              onClick={() => setTf(zoneTf)}
              title={`点位按 ${zoneTf} 周期信号算出，切回同周期看口径最准`}
              className="px-1.5 py-px rounded border border-jarvis-yellow/50 text-jarvis-yellow hover:bg-jarvis-yellow/10 transition-colors"
            >
              切回 {zoneTf}
            </button>
          )}
          <button
            onClick={clearPositionZone}
            title="清除多空区间图"
            className="ml-auto flex items-center gap-0.5 px-1.5 py-0.5 rounded text-jarvis-text-secondary hover:text-jarvis-red transition-colors"
          >
            <X size={12} />
            清除
          </button>
        </div>
      )}

      {/* AI 走势研判卡片：方向概率 / 信心 / 目标区 / 理由 / 依据信号 / 免责声明 */}
      {predictOn && (
        <PredictionCard
          resp={predictResp}
          loading={predictLoading}
          error={predictError}
          onRetry={() => setPredictSeq((s) => s + 1)}
        />
      )}

      <div className="card p-0 overflow-hidden">
        {candles.length > 0 ? (
          <KlineChart
            data={candles}
            volumeData={volumes}
            height={Math.max(400, window.innerHeight - 320)}
            smartLevels={composition.smartLevels}
            drawings={composition.drawings}
            keyLevels={(() => {
              const merged = [...composition.keyLevels, ...liqLevels];
              return merged.length > 0 ? merged : undefined;
            })()}
            planLines={composition.planLines.length > 0 ? composition.planLines : undefined}
            tradeMarks={sigMarks?.marks}
            prediction={predictOverlay}
            positionZone={positionZone}
            livePrice={liveForChart}
          />
        ) : (
          <div
            className="flex flex-col items-center justify-center text-jarvis-text-secondary"
            style={{ height: Math.max(400, window.innerHeight - 320) }}
          >
            {loading ? (
              <>
                <div className="w-6 h-6 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin mb-3" />
                <p className="text-sm">正在获取 K 线数据...</p>
                <p className="text-xs mt-1">首次加载可能需要 10-30 秒</p>
              </>
            ) : error ? (
              <>
                <p className="text-sm text-jarvis-yellow mb-1">数据获取失败</p>
                <p className="text-xs">{error}</p>
                <p className="text-xs mt-2">可能原因：Binance API 不可达（需科学上网）</p>
              </>
            ) : (
              <p className="text-sm">暂无 K 线数据</p>
            )}
          </div>
        )}
      </div>

      {/* ── Delta/CVD 订单流副图（安全带层，可折叠）：吸收背离 = 真反转证据 ── */}
      {deltaOn && (
        <>
          <DeltaPane resp={deltaResp} loading={deltaLoading} error={deltaError} />
          {/* [M2 s7] AI 解读卡：把订单流数据翻译成大白话（默认折叠，点击才请求） */}
          <div className="mt-3">
            <DeltaAiExplainCard symbol={symbol} timeframe={tf} />
          </div>
        </>
      )}

      {/* ── 高胜率反转四条件叠加：Delta 背离 + 多分布 + 三连确认 + 止损扫单 ── */}
      <ReversalScorePanel symbol={symbol} timeframe={tf} />

      {/* ── 仓位与风控建议：共识计划 ×（本金/杠杆/风险%）→ 可执行下单参数 ── */}
      <PositionAdvisor
        symbol={symbol}
        tf={(["5m", "15m", "30m", "1h", "4h", "1d"] as const).includes(tf as never) ? (tf as "5m" | "15m" | "30m" | "1h" | "4h" | "1d") : "auto"}
        compact
      />

      {displayCandle && (
        <div className="flex gap-6 text-sm text-jarvis-text-secondary px-1">
          <span>
            开:{" "}
            <span className="text-jarvis-text font-mono">
              {displayCandle.open.toLocaleString()}
            </span>
          </span>
          <span>
            高:{" "}
            <span className="text-jarvis-text font-mono">
              {displayCandle.high.toLocaleString()}
            </span>
          </span>
          <span>
            低:{" "}
            <span className="text-jarvis-text font-mono">
              {displayCandle.low.toLocaleString()}
            </span>
          </span>
          <span>
            收:{" "}
            <span
              className={clsx("font-mono", {
                "text-jarvis-green": displayCandle.close >= displayCandle.open,
                "text-jarvis-red": displayCandle.close < displayCandle.open,
              })}
            >
              {displayCandle.close.toLocaleString()}
            </span>
          </span>
        </div>
      )}
    </div>
  );
}
