import { useState, useMemo, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useApi, usePolling } from "@/hooks/useApi";
import { useKlineHistory } from "@/hooks/useKlineHistory";
import { enumOr, listOr, loadChartToggles, saveChartToggles, toggleOr } from "@/lib/chartToggles";
import { useSymbol } from "@/hooks/useSymbol";
import { useLivePrice } from "@/hooks/usePrice";
import { api, formatPrice, type TwelveSignal, type ConsensusTradePlan, type KeyLevel, type LiqMapResponse, type SignalDirection } from "@/api/client";
import KlineChart from "@/components/charts/KlineChart";
import type { FvgZoneView, PremiumDiscountView } from "@/components/charts/FvgPrimitive";
import type { StructureEventView } from "@/components/charts/SmcStructurePrimitive";
import { EventRibbon } from "@/components/cards/EventCalendarCard";
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
import {
  computeIchimoku,
  ichimokuReadout,
  ichimokuTipAt,
  type IchimokuBar,
} from "@/lib/ichimoku";
import {
  mockTrapSignals,
  buildTrapMarks,
  TRAP_LABELS,
  TRAP_TOGGLE_KEY,
  fmtTrapTime,
  type TrapSignalsResponse,
  type TrapBar,
  type TrapMark,
} from "@/lib/trapSignals";
import {
  buildWyckoffBand,
  buildWyckoffMarks,
  WYCKOFF_EVENT_META,
  WYCKOFF_SIDE_LABELS,
  WYCKOFF_TOGGLE_KEY,
  type WyckoffOverlay,
  type WyckoffResponse,
} from "@/lib/wyckoff";
import { detectPatterns, type DetectedPattern } from "@/lib/patterns";
import {
  patternToChartOverlay,
  mergePatternDrawings,
  mergePatternMarkers,
} from "@/lib/patternOverlay";
import type { IchimokuOverlay } from "@/components/charts/KlineChart";
import DeltaPane from "@/components/charts/DeltaPane";
import MacdPane from "@/components/charts/MacdPane";
import DeltaAiExplainCard from "@/components/cards/DeltaAiExplainCard";
import TrapReasonCard from "@/components/cards/TrapReasonCard";
import ConfluenceHud from "@/components/cards/ConfluenceHud";
import PatternExplainCard from "@/components/charts/PatternExplainCard";
import { AlertTriangle, CandlestickChart, Cloudy, HelpCircle, Layers, Target, Waypoints, X } from "lucide-react";
import { planSide } from "@/components/cards/SignalBoard";
import PositionAdvisor from "@/components/cards/PositionAdvisor";
import PredictionCard from "@/components/cards/PredictionCard";
import ReversalScorePanel from "@/components/cards/ReversalScorePanel";
import SupplyDemandCard from "@/components/cards/SupplyDemandCard";
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

/** 只删「信号结构叠加」的 sys* 键，保留其它 query（与 sig*、pz* 参数可并存互不干扰） */
function stripSysOverlayQuery(q: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(q);
  for (const k of ["sysoverlay", "systf"]) next.delete(k);
  return next;
}

/** 结构叠加关键位颜色：按信号方向取涨绿/跌红/中性灰（与信号矩阵方向色一致） */
const SYS_OVERLAY_COLORS: Record<SignalDirection, string> = {
  bullish: "#3fb950",
  bearish: "#f85149",
  neutral: "#8b949e",
};

/** 预测覆盖的未来 bar 数（与预测引擎契约默认值一致） */
const PREDICT_HORIZON = 16;

/**
 * 把画线引擎输出的 bar 索引整体平移 offset：画线基于「最近固定窗口」
 * （recentCandles）计算，图表渲染的是含懒加载历史的全量数据——窗口在全量
 * 中的起始偏移即 offset，平移后线段/矩形落回正确的 K 线上。
 */
function shiftDrawingIndexes(
  d: DrawingResult | null,
  offset: number,
): DrawingResult | null {
  if (!d || offset <= 0) return d;
  return {
    ...d,
    segments: d.segments.map((s) => ({ ...s, i1: s.i1 + offset, i2: s.i2 + offset })),
    bands: d.bands.map((b) => ({ ...b, i1: b.i1 + offset, i2: b.i2 + offset })),
  };
}

/** 云图开关持久化键（跨会话记住用户偏好） */
const ICHIMOKU_KEY = "jarvis.chart.ichimoku";

// 诱多诱空开关持久化键：TRAP_TOGGLE_KEY（lib/trapSignals），与盘口透视页共用

/** 云图状态提示条的色调样式（多绿/空红/震荡灰，与信号方向色一致） */
const ICHIMOKU_TONE_CLS = {
  bullish: "border-jarvis-green/40 text-jarvis-green",
  bearish: "border-jarvis-red/40 text-jarvis-red",
  neutral: "border-jarvis-border text-jarvis-text-secondary",
} as const;

/** 图例弹层里的「云图怎么看」速览（开启云图时追加展示） */
const ICHIMOKU_LEGEND: { name: string; color: string; dashed?: boolean; explain: string }[] = [
  { name: "绿云（看涨云）", color: "#3fb950", explain: "先行带A在B上方。价格站在云上=多头格局，云带是脚下支撑区；云越厚支撑越强" },
  { name: "红云（看跌云）", color: "#f85149", explain: "先行带A在B下方。价格压在云下=空头格局，云带是头顶压力区；反弹进云易受阻" },
  { name: "基准线 Kijun(26)", color: "#39c5cf", explain: "中期多空分水岭（青线）。价在线上偏多、线下偏空；也是回调常见的支撑/阻力" },
  { name: "转换线 Tenkan(9)", color: "#d29922", explain: "短期动量线。上穿基准线=金叉（转强），下穿=死叉（转弱）" },
  { name: "迟行线 Chikou", color: "#bc8cff", dashed: true, explain: "收盘价后移26根，与历史价格比对确认趋势；在K线上方助涨、下方助跌" },
  { name: "未来云（右侧淡色区）", color: "#8b949e", dashed: true, explain: "云图独有的前瞻区：未来26根的云已经画好——云变薄或翻色=趋势可能转变。风险提醒：云图是趋势工具，震荡市信号质量下降，勿单独作为下单依据" },
];

export default function Chart() {
  // [R6] 指标开关持久化：挂载读一次快照，各开关据此恢复；变更统一写回（见下方 effect）
  const [persistedToggles] = useState(loadChartToggles);
  const [tf, setTf] = useState<Timeframe>(
    enumOr(persistedToggles, "tf", TIMEFRAMES, "15m"),
  );
  // 三档视图（简洁/进阶/专业），选择持久化；细粒度开关只在专业模式生效
  const [viewMode, setViewModeState] = useState<ViewMode>(() => loadViewMode());
  const setViewMode = (m: ViewMode) => {
    setViewModeState(m);
    saveViewMode(m);
  };
  const [legendOpen, setLegendOpen] = useState(false);
  const [smart, setSmart] = useState(toggleOr(persistedToggles, "smart", true));
  const [draws, setDraws] = useState<Set<DrawMode>>(
    () => new Set(listOr(persistedToggles, "draws", ALL_DRAW_MODES)),
  );
  const [autoTune, setAutoTune] = useState(toggleOr(persistedToggles, "autoTune", true));
  const [twelve, setTwelve] = useState(toggleOr(persistedToggles, "twelve", false));
  const [plan, setPlan] = useState(toggleOr(persistedToggles, "plan", true));
  // [C2] 盘上合流仪表（环境合流分 HUD）：默认开（主控 D1 裁决——用户点名主打，
  // 折叠态单行视觉预算极小；「新层默认关」惯例适用于会洗图的叠加层，不适用于此）
  const [confluenceOn, setConfluenceOn] = useState(toggleOr(persistedToggles, "confluenceOn", true));
  // [C2] 反转四条件面板已收编进合流仪表展开态（D2 裁决）；图下原面板默认收起
  // 保留回退，点开才挂载（避免收起状态下的后台轮询）
  const [reversalOpen, setReversalOpen] = useState(false);
  // 走势预测层（概率锥/路径/研判卡片）独立开关；默认关，避免干扰常规看盘
  const [predictOn, setPredictOn] = useState(toggleOr(persistedToggles, "predictOn", false));
  // 云图（一目均衡表）开关：localStorage 记住偏好，默认关
  const [ichimokuOn, setIchimokuOnState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(ICHIMOKU_KEY) === "1";
    } catch {
      return false;
    }
  });
  const setIchimokuOn = (v: boolean) => {
    setIchimokuOnState(v);
    try {
      localStorage.setItem(ICHIMOKU_KEY, v ? "1" : "0");
    } catch {
      /* storage unavailable — 开关仍生效，只是不持久化 */
    }
  };
  // 诱多诱空陷阱信号开关：localStorage 记住偏好，默认关
  const [trapOn, setTrapOnState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(TRAP_TOGGLE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const setTrapOn = (v: boolean) => {
    setTrapOnState(v);
    try {
      localStorage.setItem(TRAP_TOGGLE_KEY, v ? "1" : "0");
    } catch {
      /* storage unavailable — 开关仍生效，只是不持久化 */
    }
  };
  // 威科夫阶段带/事件标记开关：localStorage 记住偏好，默认关
  const [wyckoffOn, setWyckoffOnState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(WYCKOFF_TOGGLE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const setWyckoffOn = (v: boolean) => {
    setWyckoffOnState(v);
    try {
      localStorage.setItem(WYCKOFF_TOGGLE_KEY, v ? "1" : "0");
    } catch {
      /* storage unavailable — 开关仍生效，只是不持久化 */
    }
  };
  // 形态分析开关（专业模式）：识别经典形态 → 图上标注 + 解释卡片；默认关
  const [patternOn, setPatternOn] = useState(toggleOr(persistedToggles, "patternOn", false));
  // 当前图上标注/卡片展开的形态下标（多形态时卡片 tab 切换联动图上标注）
  const [patternIdx, setPatternIdx] = useState(0);
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

  // ── 诱多诱空开关的跳转入口（提醒页「去图表查看」带 ?trap=1 进入）──
  // 一次性消费：开启开关后即从 query 里删掉，不影响其它并存参数
  useEffect(() => {
    if (searchParams.get("trap") === "1") {
      setTrapOn(true);
      const next = new URLSearchParams(searchParams);
      next.delete("trap");
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

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

  // ── 信号结构叠加（信号矩阵「结构」跳转携带 sys* query 进入） ──
  // sysoverlay=系统slug & systf=信号周期：把该系统的关键位画成水平线、
  // 交易计划画成多空区间图，方向/强度/理由展示在状态条
  const sysSystem = searchParams.get("sysoverlay");
  const rawSysTf = searchParams.get("systf");
  const sysTf: Timeframe | null =
    rawSysTf && (TIMEFRAMES as readonly string[]).includes(rawSysTf)
      ? (rawSysTf as Timeframe)
      : null;
  const clearSysOverlay = () => {
    // 只清 sys* 键，保留可能并存的盈损标记/区间图参数
    setSearchParams(stripSysOverlayQuery(searchParams), { replace: true });
  };

  // 进入/切换结构目标时对齐信号周期：关键位与计划按 systf 周期信号算出，
  // 画在同周期 K 线上口径才一致（之后用户可自由切走周期，价格几何仍成立）
  useEffect(() => {
    if (sysSystem && sysTf) setTf(sysTf);
  }, [sysSystem, sysTf]);

  // 拉该周期的十二套信号并锁定目标系统。useApi 自带请求序号防乱序，再叠加
  // 回声校验：symbol/tf 与当前请求不一致（慢响应/后端周期回退）一律不采用
  const { data: sysResp, loading: sysLoading } = useApi(
    () =>
      sysSystem && sysTf
        ? api.twelveSignals(symbol, sysTf)
        : Promise.resolve(null),
    [sysSystem, sysTf, symbol],
  );

  const sysSignal = useMemo<TwelveSignal | null>(() => {
    if (!sysSystem || !sysTf || !sysResp || sysResp.ok === false) return null;
    if (isStaleEcho(symbol, sysResp.symbol)) return null;
    if (sysResp.tf != null && sysResp.tf !== sysTf) return null;
    return sysResp.signals?.find((s) => s.system === sysSystem) ?? null;
  }, [sysSystem, sysTf, sysResp, symbol]);

  // 方向归一（后端异常值按中性处理）与强度截断，供关键位配色与状态条共用
  const sysDir: SignalDirection =
    sysSignal?.direction === "bullish" || sysSignal?.direction === "bearish"
      ? sysSignal.direction
      : "neutral";
  const sysStrength = Math.max(0, Math.min(1, Number(sysSignal?.strength ?? 0)));

  // 结构画线几何（缠论笔折线/买卖点箭头/中枢框/水平位）。跟随当前显示周期
  // 取数（tf 进依赖）：折线/框按 bar 开盘时间锚定，只有画在同周期 K 线上
  // 才对得上；用户切走周期即按新周期重拉重画。
  const { data: sysStructResp, loading: sysStructLoading } = useApi(
    () =>
      sysSystem
        ? api.twelveStructure(symbol, tf, sysSystem)
        : Promise.resolve(null),
    [sysSystem, tf, symbol],
  );

  // 回声校验：symbol/tf/system 任一与当前请求不符（慢响应/后端回退）一律丢弃
  const sysStructure = useMemo(() => {
    if (!sysSystem || !sysStructResp || sysStructResp.ok === false) return null;
    if (isStaleEcho(symbol, sysStructResp.symbol)) return null;
    if (sysStructResp.tf != null && sysStructResp.tf !== tf) return null;
    if (sysStructResp.system != null && sysStructResp.system !== sysSystem) return null;
    return sysStructResp.drawings ?? null;
  }, [sysSystem, sysStructResp, symbol, tf]);

  // 该系统的 key_levels → 图上水平线（label 冠系统名缩写，颜色随信号方向）。
  // 兼容回退：structure 接口 ok 时由 structure.hlines（KlineChart 内渲染）
  // 替代，此处返回空避免双重画；接口不可用/旧后端时才走本方案。
  const sysLevels = useMemo<KeyLevel[]>(() => {
    if (!sysSignal || sysStructure) return [];
    const color = SYS_OVERLAY_COLORS[sysDir];
    const abbr = (sysSignal.name_cn || sysSignal.system).slice(0, 4);
    return (sysSignal.key_levels ?? [])
      .filter((lv) => Number.isFinite(Number(lv?.price)) && Number(lv.price) > 0)
      .map((lv) => ({
        label: `${abbr}·${lv.label}`,
        price: Number(lv.price),
        color,
        width: 1,
      }));
  }, [sysSignal, sysStructure, sysDir]);

  // 该系统的 trade_plan → 多空区间图；pz* 显式区间图参数存在时 pz* 优先
  const sysZone = useMemo(() => {
    if (!sysSignal?.trade_plan || zoneParams) return null;
    const plan = sysSignal.trade_plan;
    const side = planSide(plan);
    if (side == null) return null;
    return buildPositionZoneView({
      side,
      entry: plan.entry,
      stopLoss: plan.stop_loss,
      takeProfit: plan.take_profit,
      name: sysSignal.name_cn || sysSignal.system,
    });
  }, [sysSignal, zoneParams]);

  // 各数据源的实际启用条件：简洁/进阶忽略专业模式的细粒度开关
  const isPro = viewMode === "pro";
  const planActive = !isPro || plan;
  const smartActive = !isPro || smart;
  const twelveActive = viewMode === "advanced" || (isPro && twelve);

  // 切周期防抖（150ms）：TF 按钮快速扫过多档时只为停留档发 K 线请求，
  // 中间档位一次请求都不发（UI 高亮仍用 tf 即时响应，无感延迟）
  const [tfSettled, setTfSettled] = useState(tf);
  useEffect(() => {
    if (tfSettled === tf) return;
    const t = window.setTimeout(() => setTfSettled(tf), 150);
    return () => window.clearTimeout(t);
  }, [tf, tfSettled]);

  // 实时窗口轮询（节奏与旧版一致）+ 向左拖懒加载更早历史（end_time 分页，
  // 多页前插合并，切币种/周期整组切换，已加载历史段有内存缓存切回免重拉）
  // ——见 useKlineHistory
  const {
    rows: klineRows,
    loading,
    error,
    loadingOlder,
    loadOlder,
  } = useKlineHistory(
    symbol,
    tfSettled,
    LIMITS[tfSettled],
    tfSettled === "1m" ? 10_000 : 60_000,
  );

  const { candles, volumes } = useMemo(() => {
    if (klineRows.length === 0) {
      return { candles: [] as CandlestickData<Time>[], volumes: [] as HistogramData<Time>[] };
    }
    const c: CandlestickData<Time>[] = [];
    const v: HistogramData<Time>[] = [];
    for (const k of klineRows) {
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
  }, [klineRows]);

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

  // ── 云图（一目均衡表 9/26/52）：K 线 → 五线 + 双色云带 + 人话解读 ──
  // 纯本地计算（幅度 O(n·52)，n≤300 毫秒级），随 K 线轮询自动重算；
  // 切周期/切币种由 candles 变化天然驱动，无需额外请求。
  const ichimokuData = useMemo(() => {
    if (!ichimokuOn || candles.length === 0) return null;
    const bars: IchimokuBar[] = candles.map((c) => ({
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    const r = computeIchimoku(bars);
    if (r.cloud.length === 0) return null; // 窗口不足（需 ≥78 根）
    const toPts = (arr: (number | null)[]) =>
      arr.flatMap((v, i) => (v === null ? [] : [{ time: candles[i].time, value: v }]));
    const tipByTime = new Map<number, string>();
    for (let i = 0; i < candles.length; i++) {
      const tip = ichimokuTipAt(r, i);
      if (tip) tipByTime.set(Number(candles[i].time), tip);
    }
    const overlay: IchimokuOverlay = {
      tenkan: toPts(r.tenkan),
      kijun: toPts(r.kijun),
      chikou: toPts(r.chikou),
      cloud: r.cloud,
      futureStart: r.futureStart,
      displacement: r.params.displacement,
      tipByTime,
    };
    return { overlay, readout: ichimokuReadout(bars, r) };
  }, [ichimokuOn, candles]);

  // ── 诱多/诱空陷阱信号：识别引擎 GET /api/trap-signals；未就绪/失败回退
  // 本地假突破规则识别（毫秒级纯计算，随 K 线轮询自动重算——新信号可被感知）──
  const [trapApiResp, setTrapApiResp] = useState<TrapSignalsResponse | null>(null);
  const [trapApiState, setTrapApiState] = useState<
    "idle" | "loading" | "ok" | "unavailable"
  >("idle");

  useEffect(() => {
    if (!trapOn) {
      setTrapApiResp(null);
      setTrapApiState("idle");
      return;
    }
    let cancelled = false;
    setTrapApiState("loading");
    (async () => {
      try {
        const res = await api.trapSignals(symbol, tf);
        if (cancelled) return;
        // 回声校验：慢返回的旧币种/旧周期响应不得写入当前图（防交易误导）
        if (isStaleEcho(symbol, res?.symbol)) return;
        if (res?.interval && res.interval !== tf) return;
        if (res && res.ok !== false && Array.isArray(res.signals)) {
          setTrapApiResp(res);
          setTrapApiState("ok");
          return;
        }
        setTrapApiResp(null);
        setTrapApiState("unavailable");
      } catch {
        if (!cancelled) {
          setTrapApiResp(null);
          setTrapApiState("unavailable");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [trapOn, symbol, tf]);

  // 数据合成：引擎响应优先；未就绪时本地规则识别（与预测/Delta 同款降级模式）
  const trapData = useMemo<TrapSignalsResponse | null>(() => {
    if (!trapOn || candles.length === 0) return null;
    if (trapApiState === "ok" && trapApiResp) return trapApiResp;
    const bars: TrapBar[] = candles.map((c, i) => ({
      timeSec: Number(c.time),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
      volume: volumes[i]?.value,
    }));
    return mockTrapSignals(symbol, tf, bars);
  }, [trapOn, trapApiState, trapApiResp, candles, volumes, symbol, tf]);

  // 信号 → K 线三角警示标记（窗口裁剪 + 锚定信号 bar 高低点）
  const trapMarks = useMemo<TrapMark[] | null>(() => {
    if (!trapData || candles.length === 0) return null;
    const anchors = candles.map((c) => ({
      timeSec: Number(c.time),
      high: c.high,
      low: c.low,
    }));
    const marks = buildTrapMarks(trapData, anchors);
    return marks.length > 0 ? marks : null;
  }, [trapData, candles]);

  // 点击警示牌 → 原因卡片（浮在图表容器右上角）；切币种/周期/关开关时收起
  const [selectedTrap, setSelectedTrap] = useState<TrapMark | null>(null);
  useEffect(() => {
    setSelectedTrap(null);
  }, [symbol, tf, trapOn]);

  const latestTrap =
    trapData && trapData.signals.length > 0
      ? trapData.signals[trapData.signals.length - 1]
      : null;

  // ── 威科夫阶段引擎（GET /api/wyckoff）：阶段带 + 12 事件标记 ──
  // 刷新联动：以「最新一根 K 线的开盘时间」为依赖锚点——现有 kline 轮询
  // 收到新 bar 时自动重拉一次（后端按同指纹缓存，成本≈0），同一根 bar 内
  // 的轮询刷新不重复请求。不新增任何 setInterval/独立轮询（性能纪律第 6 条）。
  const [wyckoffResp, setWyckoffResp] = useState<WyckoffResponse | null>(null);
  const [wyckoffApiState, setWyckoffApiState] = useState<
    "idle" | "loading" | "ok" | "unavailable"
  >("idle");
  const lastKlineTs = klineRows.length > 0 ? klineRows[klineRows.length - 1].ts : 0;

  useEffect(() => {
    if (!wyckoffOn) {
      setWyckoffResp(null);
      setWyckoffApiState("idle");
      return;
    }
    if (lastKlineTs <= 0) return; // K 线未就位，等下一轮联动
    let cancelled = false;
    setWyckoffApiState((s) => (s === "ok" ? s : "loading"));
    (async () => {
      try {
        const res = await api.wyckoff(symbol, tf);
        if (cancelled) return;
        // 回声校验：慢返回的旧币种/旧周期响应不得写入当前图（防交易误导）
        if (isStaleEcho(symbol, res?.symbol)) return;
        if (res?.interval && res.interval !== tf) return;
        if (res && res.ok !== false) {
          setWyckoffResp(res);
          setWyckoffApiState("ok");
          return;
        }
        setWyckoffResp(null);
        setWyckoffApiState("unavailable");
      } catch {
        if (!cancelled) {
          setWyckoffResp(null);
          setWyckoffApiState("unavailable");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [wyckoffOn, symbol, tf, lastKlineTs]);

  // 二次回声守卫：切币种/周期后、新响应到达前，旧响应不得参与本图渲染
  const wyckoffData = useMemo<WyckoffResponse | null>(() => {
    if (!wyckoffOn || !wyckoffResp) return null;
    if (isStaleEcho(symbol, wyckoffResp.symbol)) return null;
    if (wyckoffResp.interval && wyckoffResp.interval !== tf) return null;
    return wyckoffResp;
  }, [wyckoffOn, wyckoffResp, symbol, tf]);

  // 响应 → 阶段带 + 事件徽章载荷（窗口裁剪 + 吸附锚定 bar，纯函数换算）
  const wyckoffOverlay = useMemo<WyckoffOverlay | null>(() => {
    if (!wyckoffData || candles.length === 0) return null;
    const anchors = candles.map((c) => ({
      timeSec: Number(c.time),
      high: c.high,
      low: c.low,
    }));
    const band = buildWyckoffBand(wyckoffData, anchors);
    const marks = buildWyckoffMarks(wyckoffData, anchors);
    return band || marks.length > 0 ? { band, marks } : null;
  }, [wyckoffData, candles]);

  const latestWyckoffEvent = useMemo(() => {
    const evs = wyckoffData?.events;
    if (!Array.isArray(evs) || evs.length === 0) return null;
    const known = evs.filter((e) => WYCKOFF_EVENT_META[e?.type]);
    return known.length > 0 ? known[known.length - 1] : null;
  }, [wyckoffData]);

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
    // 周期判定用 tfSettled 与 candles 数据口径原子一致（切 TF 防抖窗口内不错位）
    if (!sigSystem || !sigTf || tfSettled !== sigTf) return null;
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
  }, [sigSystem, sigTf, tfSettled, sigTradesResp, candles]);

  // 画线引擎输入沿用「最近固定窗口」口径（与 LIMITS 上限一致）：懒加载前插
  // 的更早历史不进画线/自调计算——避免每页前插触发自调参数重搜（TUNE_KEY
  // 缓存按 bars 数精确匹配，bars 每页都变则页页重搜）与评分计算量随历史
  // 膨胀；引擎设计本来就只看最近窗口。输出的 bar 索引在传入图表前经
  // shiftDrawingIndexes 平移 drawingsIndexOffset 对齐全量数据。
  const recentCandles = useMemo(
    () =>
      candles.length > LIMITS[tfSettled] ? candles.slice(-LIMITS[tfSettled]) : candles,
    [candles, tfSettled],
  );
  const drawingsIndexOffset = candles.length - recentCandles.length;

  // Drawing-engine input arrays, derived once per kline refresh. `dates` is
  // index-aligned (the engine keys outputs by bar index, not by label).
  const baseData = useMemo<BaseData>(() => ({
    dates: recentCandles.map((c) => String(c.time)),
    closes: recentCandles.map((c) => c.close),
    highs: recentCandles.map((c) => c.high),
    lows: recentCandles.map((c) => c.low),
  }), [recentCandles]);

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

    // 键用 tfSettled 与 baseData 数据口径原子一致：防抖窗口内 tf 先行变化时
    // 不得用旧周期数据网格搜索后存进新周期缓存键（既污染缓存又白耗一轮重搜）
    const cached = loadTunedParams(symbol, tfSettled, bars);
    if (cached) return { params: cached, score: 0, tuned: true };

    const warm = loadWarmParams(symbol, tfSettled);
    if (warm) {
      const ev = evaluateParams(baseData, warm);
      if (ev.uplift > 0) {
        saveTunedParams(symbol, tfSettled, warm, ev.score, bars);
        return { params: warm, score: ev.score, tuned: true };
      }
      const res = gridSearchParams(baseData, { seed: warm, strategy });
      saveWarmParams(symbol, tfSettled, res.params);
      saveTunedParams(symbol, tfSettled, res.params, res.score, bars);
      return { params: res.params, score: res.score, tuned: true };
    }

    const res = gridSearchParams(baseData, { strategy });
    saveWarmParams(symbol, tfSettled, res.params);
    saveTunedParams(symbol, tfSettled, res.params, res.score, bars);
    return { params: res.params, score: res.score, tuned: true };
  }, [autoTune, effectiveDraws.size, baseData, symbol, tfSettled]);

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

  // ── 形态分析（楔形/矩形/旗形·三角旗/三角形/头肩/双顶底）：专业模式开关触发，
  // 引擎输出按置信度降序；选中形态经 patternToChartOverlay 并入 drawings /
  // structMarkers 通道（复用现有画线原语），关闭时载荷为空 = 彻底清除 ──
  const patternList = useMemo<DetectedPattern[] | null>(() => {
    if (!isPro || !patternOn || candles.length < 20) return null;
    return detectPatterns(baseData);
  }, [isPro, patternOn, baseData, candles.length]);

  const safePatternIdx =
    patternList && patternList.length > 0 ? Math.min(patternIdx, patternList.length - 1) : 0;
  const activePattern =
    patternList && patternList.length > 0 ? patternList[safePatternIdx] : null;

  const patternOverlay = useMemo(
    () => patternToChartOverlay(activePattern, baseData.dates),
    [activePattern, baseData.dates],
  );

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
  const [liqOn, setLiqOn] = useState(toggleOr(persistedToggles, "liqOn", false));
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

  // ── MACD 指标副图：主图 K 线本地计算（12/26/9），无额外请求 ──
  const [macdOn, setMacdOn] = useState(toggleOr(persistedToggles, "macdOn", false));

  // ── Delta/CVD 订单流副图（「安全带」层）：引擎 GET /api/delta，未就绪时
  // 回退 K 线本地演示推演（角标标注），与预测层同一套降级模式 ──
  const [deltaOn, setDeltaOn] = useState(toggleOr(persistedToggles, "deltaOn", false));

  // ── [R8] FVG 失衡区 + SMC 结构线（BOS/CHoCH）：同一 GET /api/fvg 数据源
  // （60s 后端缓存），两个独立开关共享请求——任一开启才拉取 ──
  const [fvgOn, setFvgOn] = useState(toggleOr(persistedToggles, "fvgOn", false));
  const [bosOn, setBosOn] = useState(toggleOr(persistedToggles, "bosOn", false));
  // [R9] 折溢价区独立开关（默认关）：大面积着色曾把图洗糊，改独立开+轻量画法
  const [pdOn, setPdOn] = useState(toggleOr(persistedToggles, "pdOn", false));
  const smcActive = fvgOn || bosOn || pdOn;
  const { data: fvgResp } = usePolling(
    // [R12] max_zones=30（后端 clamp 上限）：数量不设人为上限，质量过滤把关
    () => (smcActive ? api.fvg(symbol, tfSettled, 30) : Promise.resolve(null)),
    smcActive ? 60_000 : 0,
    [smcActive, symbol, tfSettled],
  );
  const fvgZoneViews = useMemo<FvgZoneView[] | null>(() => {
    if (!fvgOn || !fvgResp?.ok) return null;
    // 回声校验：旧币种/旧周期的慢响应不采用（与其它叠加层同款防串图）
    if (isStaleEcho(symbol, fvgResp.symbol) || (fvgResp.tf != null && fvgResp.tf !== tfSettled)) {
      return null;
    }
    // [R10/R11/R12] 质量过滤（「不合适」的定义）：高回补（≥80%）/完全回补/
    // 过老（>200 根）隐藏；**数量不设上限**（R12 用户明确要求：合适的都展示；
    // primitive 批量绘制，30 个量级无性能压力）。
    // [R11] LuxAlgo 风格画法：部分回补的缺口不截长度、改剔除已回补段——
    // 残余未回补窄带持续延伸到右缘（半回补缺口的残余仍是活跃磁吸/入场区）
    const HIDE_FILL_PCT = 80;
    const HIDE_AGE_BARS = 200;
    const out: FvgZoneView[] = [];
    for (const z of fvgResp.zones ?? []) {
      if (z.mitigated || z.created_ts == null) continue;
      if ((z.fill_pct ?? 0) >= HIDE_FILL_PCT) continue;
      if ((z.age_bars ?? 0) > HIDE_AGE_BARS) continue;
      if (!(Number.isFinite(z.top) && Number.isFinite(z.bottom) && z.top > z.bottom)) continue;
      const bull = z.type === "bullish";
      const fillPct = Math.max(0, Math.min(100, z.fill_pct ?? 0));
      // 残余区间：bullish 从 top 侧被向下回补 → 残余靠 bottom；bearish 镜像
      const height = z.top - z.bottom;
      const eaten = height * (fillPct / 100);
      const remTop = bull ? z.top - eaten : z.top;
      const remBottom = bull ? z.bottom : z.bottom + eaten;
      if (!(remTop - remBottom > 0)) continue;
      out.push({
        type: z.type,
        top: remTop,
        bottom: remBottom,
        timeSec: Math.floor(z.created_ts / 1000),
        fillPct,
        // [R13] 三行紧凑 + 小白措辞（浮层 pre-line 按行渲染）
        tooltip:
          `${bull ? "看涨" : "看跌"} FVG 缺口 · ${fillPct > 0 ? `已被填掉 ${Math.round(fillPct)}%` : "还没被碰过"}\n` +
          `原始缺口 ${z.bottom.toLocaleString()} ~ ${z.top.toLocaleString()}\n` +
          (fillPct > 0
            ? `还没被价格填掉的部分 ${remBottom.toLocaleString()} ~ ${remTop.toLocaleString()}（仍是有效吸引区）\n`
            : "") +
          `${new Date(z.created_ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })} 形成 · ` +
          `价格常${bull ? "回踩" : "反弹"}进这块区域后继续原方向`,
      });
    }
    return out;
  }, [fvgOn, fvgResp, symbol, tfSettled]);

  // [N2→R9] 折价溢价区 → 均衡线+边界线+右缘窄带（独立开关 pdOn；检测失败/旧后端缺字段不渲染）
  const fvgPdView = useMemo<PremiumDiscountView | null>(() => {
    if (!pdOn || !fvgResp?.ok) return null;
    if (isStaleEcho(symbol, fvgResp.symbol) || (fvgResp.tf != null && fvgResp.tf !== tfSettled)) {
      return null;
    }
    const pd = fvgResp.premium_discount;
    if (!pd?.ok || pd.range_high == null || pd.range_low == null || pd.equilibrium == null) {
      return null;
    }
    const zoneCn =
      pd.zone === "premium" ? "溢价区（宜卖不宜追多）"
      : pd.zone === "discount" ? "折价区（宜买不宜杀跌）"
      : "均衡带（无分位优势）";
    return {
      rangeHigh: pd.range_high,
      rangeLow: pd.range_low,
      equilibrium: pd.equilibrium,
      zone: pd.zone ?? "equilibrium",
      posPct: pd.pos_pct ?? 50,
      tooltip:
        `折价/溢价区（近 ${pd.lookback_bars ?? 120} 根 dealing range）\n` +
        `区间 ${pd.range_low.toLocaleString()} ~ ${pd.range_high.toLocaleString()} · 均衡 ${pd.equilibrium.toLocaleString()}\n` +
        `现价分位 ${Math.round(pd.pos_pct ?? 50)}% → ${zoneCn}\n` +
        `SMC 口径：折价区找做多、溢价区找做空，均衡线上下各留 5% 缓冲`,
    };
  }, [pdOn, fvgResp, symbol, tfSettled]);

  // SMC 结构事件 → 线段视图（BOS 实线 / CHoCH 琥珀虚线，被突破 swing 点→突破蜡烛）
  const smcEventViews = useMemo<StructureEventView[] | null>(() => {
    if (!bosOn || !fvgResp?.ok) return null;
    if (isStaleEcho(symbol, fvgResp.symbol) || (fvgResp.tf != null && fvgResp.tf !== tfSettled)) {
      return null;
    }
    const out: StructureEventView[] = [];
    for (const e of fvgResp.structure_events ?? []) {
      if (e.swing_ts == null || e.break_ts == null || !Number.isFinite(e.level)) continue;
      const bull = e.direction === "bullish";
      const isBos = e.kind === "bos";
      out.push({
        kind: e.kind,
        direction: e.direction,
        level: e.level,
        swingTimeSec: Math.floor(e.swing_ts / 1000),
        breakTimeSec: Math.floor(e.break_ts / 1000),
        tooltip:
          `${isBos ? "BOS 结构突破" : "CHoCH 结构转换"}（${bull ? "看涨" : "看跌"}）\n` +
          `收盘${bull ? "上破" : "跌破"} swing ${bull ? "高" : "低"}点 ${e.level.toLocaleString()}\n` +
          (isBos
            ? "趋势延续确认：结构方向上的又一次有效突破（只认收盘，影线扫单不算）"
            : "结构转换警示：首次逆结构方向的收盘突破——原趋势的结构基础被破坏，警惕反转"),
      });
    }
    return out;
  }, [bosOn, fvgResp, symbol, tfSettled]);

  // [R6] 指标开关统一写回：任一开关/画线集合/周期变化即持久化（读-合并-写，
  // 首帧写回初始值幂等；viewMode/ichimoku/trap/wyckoff 沿用各自历史键不迁移）
  useEffect(() => {
    saveChartToggles({
      smart,
      autoTune,
      twelve,
      plan,
      confluenceOn,
      predictOn,
      patternOn,
      liqOn,
      macdOn,
      deltaOn,
      fvgOn,
      bosOn,
      pdOn,
      draws: [...draws],
      tf,
    });
  }, [smart, autoTune, twelve, plan, confluenceOn, predictOn, patternOn, liqOn, macdOn, deltaOn, fvgOn, bosOn, pdOn, draws, tf]);
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

    // 演示推演与引擎 limit=200 口径对齐：只吃最近窗口，懒加载的深历史不进
    // Delta 演示计算（DeltaPane 渲染几千根柱会卡）
    const klines = (): DeltaKline[] => {
      const offset = candles.length - recentCandles.length;
      return recentCandles.map((c, i) => ({
        timeSec: Number(c.time),
        open: c.open,
        close: c.close,
        high: c.high,
        low: c.low,
        volume: volumes[offset + i]?.value,
      }));
    };

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
      {/* 任务 U：未来 24h 高影响财经事件标记条（风险窗口内变红；未配置/无事件不占位） */}
      <EventRibbon />
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

        {/* [C2] 盘上合流仪表：环境合流分 HUD（图内左上角折叠单行，点开看逐条证据） */}
        <button
          onClick={() => setConfluenceOn((v) => !v)}
          title="盘上合流仪表：图内左上角常驻环境合流分（多周期一致/结构突破/流动性扫单/FVG 折溢价四项微勾 + 0-100 分），点开看四组逐条证据与「按当前合流开计划」入口。分数是环境分不是下单信号；数据缺失诚实灰显不硬造"
          className={pillCls(confluenceOn)}
        >
          合流{confluenceOn ? "·开" : "·关"}
        </button>

        {/* 预测层：概率锥 + 路径虚线画在 K 线右侧未来区域 + 图上方研判卡片 */}
        <button
          onClick={() => setPredictOn((v) => !v)}
          title="AI 走势预测：在 K 线右侧未来区域画预测路径（虚线）与目标区间（概率锥），并给出方向概率与研判理由。预测仅供参考，不构成投资建议"
          className={pillCls(predictOn)}
        >
          预测{predictOn ? "·开" : "·关"}
        </button>

        {/* 云图（一目均衡表）：五线 + 双色云带 + 未来 26 根云延伸，自动算自动画 */}
        <button
          onClick={() => setIchimokuOn(!ichimokuOn)}
          title="云图（一目均衡表 9/26/52）：自动计算并画出转换线/基准线/迟行线与红绿双色云带，未来 26 根云提前画好。云上偏多、云下偏空、云中观望；云带就是支撑/压力区，不用自己找点位"
          className={pillCls(ichimokuOn)}
        >
          云图{ichimokuOn ? "·开" : "·关"}
        </button>
        {ichimokuOn && candles.length > 0 && !ichimokuData && (
          <span className="text-xs text-jarvis-yellow" title="一目均衡表需要至少 78 根 K 线（52 周期窗口 + 26 根前移）">
            K 线数不足，云未生成
          </span>
        )}

        {/* 诱多诱空陷阱信号：假突破识别，红▽诱多别追多 / 绿△诱空别追空 */}
        <button
          onClick={() => setTrapOn(!trapOn)}
          title="诱多诱空识别：自动标出「假突破」陷阱——冲破前高又被打回=诱多（红色倒三角，别追多），跌破前低又收回=诱空（绿色正三角，别追空）。点击图上警示牌看逐条理由与操作建议。识别引擎未接入时显示本地规则识别的演示数据"
          className={pillCls(trapOn)}
        >
          诱多诱空{trapOn ? "·开" : "·关"}
        </button>

        {/* 威科夫阶段带/事件标记：吸筹绿带/派发红带 + 12 事件徽章（SC/Spring/SOS/UTAD…） */}
        <button
          onClick={() => setWyckoffOn(!wyckoffOn)}
          title="威科夫阶段引擎：交易区间画成半透明背景带（吸筹=绿 / 派发=红，Phase A→E 颜色渐深），12 个威科夫事件（SC 恐慌抛售、Spring 弹簧、SOS 强势信号、UTAD 派发上冲…）以缩写徽章挂在事件 K 线上，悬停看置信度与订单流佐证。引擎未就绪时显示接口提示"
          className={pillCls(wyckoffOn)}
        >
          威科夫{wyckoffOn ? "·开" : "·关"}
        </button>

        {/* Delta/CVD 副图（「安全带」层）：只有 Delta 与价格背离（吸收证据）才是真反转 */}
        <button
          onClick={() => setDeltaOn((v) => !v)}
          title="Delta/CVD 订单流副图：每根主动买卖差（正绿负红）+ CVD 累计曲线；价格创新低但 CVD 抬高 = 吸收背离（安全带确认信号）。引擎未就绪时显示演示推演"
          className={pillCls(deltaOn)}
        >
          Delta{deltaOn ? "·开" : "·关"}
        </button>

        {/* MACD 指标副图：主图 K 线本地计算，经典 12/26/9 */}
        <button
          onClick={() => setMacdOn((v) => !v)}
          title="MACD 指标副图（12/26/9）：柱体 = DIF−DEA（正绿负红），金线 = DIF 快线，蓝线 = DEA 慢线。DIF 上穿 DEA 为金叉看多、下穿为死叉看空；柱体缩短 = 动能衰减，常先于价格转向。由当前 K 线本地计算，无额外请求"
          className={pillCls(macdOn)}
        >
          MACD{macdOn ? "·开" : "·关"}
        </button>

        {/* [M2 s5] 磁吸位：清算/止损密集区水平线（庄家扫单/插针目标位预判） */}
        <button
          onClick={() => setLiqOn((v) => !v)}
          title="磁吸位叠加：清算簇（多/空爆仓密集触发区）与止损/整数关口聚集区的水平线。价格倾向被吸向流动性密集处——接近强磁吸位时警惕扫单插针。估算模型：VP 入场分布 × 常见杠杆档 + 摆动点/关口，forceOrder 实时校准"
          className={pillCls(liqOn)}
        >
          磁吸位{liqOn ? "·开" : "·关"}
        </button>

        {/* [R8] FVG 失衡区：三根 K 线价格失衡缺口矩形带（ICT/订单流入场观察位） */}
        <button
          onClick={() => setFvgOn((v) => !v)}
          title="FVG 失衡区叠加：三根 K 线留下的价格失衡缺口（绿=看涨缺口常成回踩支撑、红=看跌缺口常成反弹压力），带从形成蜡烛延伸到最新；已完全回补的缺口自动消失，部分回补标注百分比。悬停带内看区间价/回补度/形成时间"
          className={pillCls(fvgOn)}
        >
          FVG{fvgOn ? "·开" : "·关"}
        </button>

        {/* [R8追加] BOS/CHoCH 结构线：SMC 市场结构突破与转换事件 */}
        <button
          onClick={() => setBosOn((v) => !v)}
          title="BOS/CHoCH 结构线：BOS=收盘突破最近 swing 高/低点（趋势延续确认，绿涨红跌实线）；CHoCH=首次逆结构方向突破（结构转换警示，琥珀虚线）。只认收盘突破防扫单假信号，最多展示最近 8 个事件。线段从被突破 swing 点画到突破蜡烛，悬停看解释"
          className={pillCls(bosOn)}
        >
          BOS{bosOn ? "·开" : "·关"}
        </button>

        {/* [R9] 折溢价区：dealing range 分位参考线（轻量画法不洗图，默认关） */}
        <button
          onClick={() => setPdOn((v) => !v)}
          title="折溢价区参考线：近 120 根 swing 极值构成 dealing range，0.5 中点为均衡线——SMC 口径折价区（下半）找做多、溢价区（上半）找做空。轻量画法：均衡点状线+区间边界虚线+右缘窄带着色，不铺满全图。悬停均衡线看现价分位"
          className={pillCls(pdOn)}
        >
          折溢价{pdOn ? "·开" : "·关"}
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
              className="absolute top-full left-0 mt-1 z-50 bg-jarvis-card border border-jarvis-border rounded-lg shadow-lg p-3 w-80 max-h-96 overflow-y-auto"
              onMouseLeave={() => setLegendOpen(false)}
            >
              {composition.legend.length === 0 && !(ichimokuOn && ichimokuData) ? (
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
                  {ichimokuOn && ichimokuData && (
                    <>
                      <p className="text-[10px] text-jarvis-text-secondary pt-1 border-t border-jarvis-border">
                        云图怎么看（一目均衡表）
                      </p>
                      {ICHIMOKU_LEGEND.map((e) => (
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
                    </>
                  )}
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

        {/* 形态分析：楔形/矩形/旗形·三角旗/三角形/头肩/双顶底 识别 + 图上标注 + 解释卡片 */}
        {isPro && (
          <>
            <div className="w-px h-4 bg-jarvis-border" />
            <button
              onClick={() => {
                setPatternOn((v) => !v);
                setPatternIdx(0);
              }}
              title="形态分析：自动识别楔形、矩形、旗形/三角旗、三角形、头肩、双顶底等经典形态，画出上下轨/颈线并标注触点、突破位、量度目标与建议止损，附中文多空解读"
              className={pillCls(patternOn)}
            >
              形态分析{patternOn ? "·开" : "·关"}
            </button>
            {patternOn && activePattern && (
              <span
                className={clsx(
                  "px-2 py-1 rounded-md text-xs font-medium border",
                  activePattern.direction === "bullish"
                    ? "border-jarvis-green/60 text-jarvis-green"
                    : activePattern.direction === "bearish"
                      ? "border-jarvis-red/60 text-jarvis-red"
                      : "border-jarvis-border text-jarvis-text-secondary",
                )}
              >
                {activePattern.direction === "bullish" ? "▲ " : activePattern.direction === "bearish" ? "▼ " : "＝ "}
                {activePattern.nameCn}
              </span>
            )}
            {patternOn && patternList && patternList.length === 0 && (
              <span className="text-xs text-jarvis-text-secondary">未发现明显形态</span>
            )}
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

      {/* 信号结构叠加状态条：来自信号矩阵「结构」跳转，把该系统关键位+计划区间画上图 */}
      {sysSystem && (
        <div className="flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border border-jarvis-blue/40 rounded-lg px-3 py-2">
          <Waypoints size={13} className="text-jarvis-blue shrink-0" />
          <span className="text-jarvis-text font-medium">
            「{sysSignal?.name_cn || sysSystem}」趋势结构叠加
          </span>
          {sysLoading ? (
            <span className="text-jarvis-text-secondary">加载信号结构…</span>
          ) : sysSignal ? (
            <>
              <span
                className={clsx(
                  "px-1.5 py-px rounded text-[10px] font-medium text-white",
                  sysDir === "bullish"
                    ? "bg-jarvis-green"
                    : sysDir === "bearish"
                      ? "bg-jarvis-red"
                      : "bg-jarvis-text-secondary/60",
                )}
              >
                {sysDir === "bullish" ? "看涨" : sysDir === "bearish" ? "看跌" : "中性"}
              </span>
              <span className="text-jarvis-text-secondary font-mono">
                强度 {(sysStrength * 100).toFixed(0)}%
              </span>
              <span className="text-jarvis-text-secondary font-mono">
                关键位 {sysStructure ? (sysStructure.hlines?.length ?? 0) : sysLevels.length} 条
                {sysZone ? " · 含计划区间" : ""}
              </span>
              {sysStructure ? (
                <span
                  className="text-jarvis-text-secondary font-mono"
                  title="结构画线来自 /api/twelve/structure：笔/线段折线、买卖点箭头标注、中枢框区域"
                >
                  已画 {sysStructure.polylines?.length ?? 0} 条结构线 ·{" "}
                  {sysStructure.markers?.length ?? 0} 个标注 ·{" "}
                  {sysStructure.boxes?.length ?? 0} 个区域
                </span>
              ) : sysStructLoading ? (
                <span className="text-jarvis-text-secondary">结构画线加载中…</span>
              ) : (
                <span
                  className="text-jarvis-yellow"
                  title="GET /api/twelve/structure 不可用或返回失败——后端升级后自动出现笔折线/买卖点/中枢框"
                >
                  后端未升级，仅显示关键位水平线
                </span>
              )}
              {sysSignal.reasoning && (
                <span
                  className="text-jarvis-text-secondary truncate max-w-[32rem] cursor-help"
                  title={sysSignal.reasoning}
                >
                  {sysSignal.reasoning}
                </span>
              )}
            </>
          ) : (
            <span className="text-jarvis-yellow">
              {sysTf
                ? `未取到「${sysSystem}」在 ${sysTf} 周期的信号（可能后端未就绪或系统名不识别）`
                : "systf 周期参数不合法，无法叠加结构"}
            </span>
          )}
          <button
            onClick={clearSysOverlay}
            title="清除结构叠加"
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

      {/* 云图状态提示条：价与云位置的人话解读（多绿/空红/震荡灰），悬停看五线组成 */}
      {ichimokuOn && ichimokuData?.readout && (
        <div
          className={clsx(
            "flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border rounded-lg px-3 py-2",
            ICHIMOKU_TONE_CLS[ichimokuData.readout.tone],
          )}
        >
          <Cloudy size={13} className="shrink-0" />
          <span className="font-medium cursor-help" title={ichimokuData.readout.detail}>
            {ichimokuData.readout.text}
          </span>
          <span className="ml-auto text-jarvis-text-secondary/80">
            悬停看解读依据 · 图上悬停 K 线看五线数值
          </span>
        </div>
      )}

      {/* 诱多诱空状态条：窗口内信号计数 + 最新信号摘要 + 数据来源提示 */}
      {trapOn && (
        <div className="flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border border-jarvis-yellow/40 rounded-lg px-3 py-2">
          <AlertTriangle size={13} className="text-jarvis-yellow shrink-0" />
          <span className="text-jarvis-text font-medium">诱多诱空识别</span>
          {trapData ? (
            <>
              <span className="text-jarvis-text-secondary">
                窗口内 {trapMarks?.length ?? 0} 个信号（
                <span className="text-jarvis-red">红▽=诱多别追多</span> /
                <span className="text-jarvis-green"> 绿△=诱空别追空</span>
                ）· 点图上警示牌看原因与建议
              </span>
              {latestTrap && (
                <span className="text-jarvis-text-secondary font-mono">
                  最新：{TRAP_LABELS[latestTrap.type]} {fmtTrapTime(latestTrap.ts)} @{" "}
                  {formatPrice(latestTrap.price)}
                </span>
              )}
              {trapData.mock && (
                <span
                  className="text-jarvis-yellow"
                  title="GET /api/trap-signals 未就绪，当前为本地假突破规则识别的演示数据；识别引擎接入后自动切换真实信号"
                >
                  演示数据
                </span>
              )}
            </>
          ) : trapApiState === "loading" ? (
            <span className="text-jarvis-text-secondary">加载中…</span>
          ) : (
            <span className="text-jarvis-text-secondary">
              K 线数不足（需 ≥25 根），暂无法识别
            </span>
          )}
        </div>
      )}

      {/* 威科夫状态条：阶段（侧/Phase）+ 窗口内事件数 + 最新事件 + 研判提示 */}
      {wyckoffOn && (
        <div className="flex items-center gap-2 flex-wrap text-xs bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
          <Layers size={13} className="text-jarvis-blue shrink-0" />
          <span className="text-jarvis-text font-medium">威科夫阶段</span>
          {wyckoffData ? (
            <>
              {wyckoffData.state && (
                <span
                  className={clsx(
                    "px-1.5 py-px rounded text-[10px] font-medium text-white",
                    wyckoffData.state.side === "acc"
                      ? "bg-jarvis-green"
                      : wyckoffData.state.side === "dist"
                        ? "bg-jarvis-red"
                        : "bg-jarvis-text-secondary/60",
                  )}
                >
                  {WYCKOFF_SIDE_LABELS[wyckoffData.state.side] ?? wyckoffData.state.side}
                  {wyckoffData.state.phase ? ` · Phase ${wyckoffData.state.phase}` : ""}
                </span>
              )}
              {!wyckoffData.range && (
                <span className="text-jarvis-text-secondary">
                  当前为趋势段（无交易区间，不画阶段带）
                </span>
              )}
              <span className="text-jarvis-text-secondary">
                窗口内 {wyckoffOverlay?.marks.length ?? 0} 个事件标记 · 悬停徽章看置信度与证据
              </span>
              {latestWyckoffEvent && (
                <span className="text-jarvis-text-secondary font-mono">
                  最新：{WYCKOFF_EVENT_META[latestWyckoffEvent.type].label}（
                  {WYCKOFF_EVENT_META[latestWyckoffEvent.type].abbr}）@{" "}
                  {formatPrice(latestWyckoffEvent.price)}
                </span>
              )}
              {wyckoffData.verdict_hint && (
                <span
                  className="text-jarvis-text truncate max-w-[28rem] cursor-help"
                  title={wyckoffData.verdict_hint}
                >
                  {wyckoffData.verdict_hint}
                </span>
              )}
              {wyckoffData.stale && (
                <span className="text-jarvis-yellow" title="数据层暂时失败，展示的是后端上次成功的缓存结果">
                  缓存数据
                </span>
              )}
            </>
          ) : wyckoffApiState === "loading" ? (
            <span className="text-jarvis-text-secondary">加载中…</span>
          ) : wyckoffApiState === "unavailable" ? (
            <span
              className="text-jarvis-yellow"
              title="GET /api/wyckoff 不可用，可能后端阶段引擎尚未部署；接入后自动出现阶段带与事件标记"
            >
              阶段引擎接口未就绪
            </span>
          ) : (
            <span className="text-jarvis-text-secondary">等待 K 线数据…</span>
          )}
        </div>
      )}

      <div className="card p-0 overflow-hidden relative">
        {candles.length > 0 ? (
          <>
            <KlineChart
              data={candles}
              volumeData={volumes}
              height={Math.max(400, window.innerHeight - 320)}
              smartLevels={composition.smartLevels}
              drawings={shiftDrawingIndexes(
                // 画线引擎与形态引擎同吃 recentCandles 窗口（索引同口径），
                // 合并后统一平移对齐含懒加载历史的全量数据
                mergePatternDrawings(composition.drawings, patternOverlay.drawings),
                drawingsIndexOffset,
              )}
              keyLevels={(() => {
                const merged = [...composition.keyLevels, ...liqLevels, ...sysLevels];
                return merged.length > 0 ? merged : undefined;
              })()}
              planLines={composition.planLines.length > 0 ? composition.planLines : undefined}
              tradeMarks={sigMarks?.marks}
              prediction={predictOverlay}
              positionZone={positionZone ?? sysZone}
              structure={sysStructure}
              structMarkers={mergePatternMarkers(sysStructure?.markers, patternOverlay.markers)}
              livePrice={liveForChart}
              ichimoku={ichimokuData?.overlay ?? null}
              trapMarks={trapMarks}
              fvgZones={fvgZoneViews}
              fvgPremiumDiscount={fvgPdView}
              smcEvents={smcEventViews}
              onTrapClick={(mark) => setSelectedTrap(mark)}
              wyckoff={wyckoffOverlay}
              datasetKey={`${symbol}|${tfSettled}`}
              onNearLeftEdge={loadOlder}
              loadingOlder={loadingOlder}
            />
            {/* [C2] 合流仪表 HUD：左上角常驻折叠单行（z-10 < TrapReasonCard z-20） */}
            {confluenceOn && <ConfluenceHud symbol={symbol} tf={tf} />}
            {/* 陷阱原因卡片：固定右上角浮层，不遮点击处的 K 线形态 */}
            {selectedTrap && (
              <TrapReasonCard mark={selectedTrap} onClose={() => setSelectedTrap(null)} />
            )}
          </>
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

      {/* 形态分析解释卡片：形态名/方向徽标/置信度/关键点位/中文多空逻辑；
          多形态 tab 切换同时联动图上标注（选中形态才画） */}
      {isPro && patternOn && (
        <PatternExplainCard
          patterns={patternList ?? []}
          activeIndex={safePatternIdx}
          onSelect={setPatternIdx}
        />
      )}

      {/* ── MACD 指标副图（12/26/9）：与画线/Delta 同吃最近窗口，时间轴口径一致 ── */}
      {macdOn && <MacdPane candles={recentCandles} />}

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

      {/* ── 高胜率反转四条件：已收编进合流仪表展开态「微观确认」组（D2 裁决）；
          原面板默认收起保留回退，点开才挂载（收起状态零轮询） ── */}
      {reversalOpen ? (
        <div>
          <button
            onClick={() => setReversalOpen(false)}
            className="mb-1 text-xs text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
          >
            ▾ 收起反转四条件原面板
          </button>
          <ReversalScorePanel symbol={symbol} timeframe={tf} />
        </div>
      ) : (
        <button
          onClick={() => setReversalOpen(true)}
          title="四条件（Delta 背离/过程多分布/三连确认/止损扫单）已并入图上合流仪表展开态；点开回看原独立面板"
          className="text-left text-xs text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
        >
          ▸ 高胜率反转 · 四条件（已并入合流仪表，点开回看原面板）
        </button>
      )}

      {/* ── 主力底牌（威科夫×订单流量价核对）：吸筹/派发裁决 + 突破真伪核验 ── */}
      <SupplyDemandCard symbol={symbol} interval={tf} />

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
