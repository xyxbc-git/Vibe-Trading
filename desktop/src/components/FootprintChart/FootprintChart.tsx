import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, BarChart3, BookOpen, History, Maximize2, RefreshCw, X } from "lucide-react";
import type { FootprintBar, PriceLevel, Timeframe } from "@/types/footprint";
import { footprintDataService } from "@/lib/footprint/dataService";
import { BinanceBanError, getFootprintSource, setFootprintSource } from "@/lib/footprint/binanceFeed";
import {
  fetchFootprintRange,
  fetchOhlcRange,
  mergeOlderBars,
  SEED_FOOT_TFS,
} from "@/lib/footprint/backendSeed";
import {
  dayRangeMs,
  fetchHistoryBars,
  fmtLocalDate,
  isHistoryTimeframe,
} from "@/lib/footprint/historyService";
import { useSymbol } from "@/hooks/useSymbol";
import type { BadgeBox, HoverInfo, Layout } from "./renderer";
import {
  AXIS_W,
  COLORS,
  IMBALANCE_RATIO,
  STATS_H,
  STATS_ROWS,
  TIME_H,
  barWOf,
  computeLayout,
  drawFrame,
  estimateTick,
  fmtK,
  fmtPrice,
  hitTest,
} from "./renderer";
import { stepViewport, useViewport } from "./useViewport";
import { buildInsights, type Insight } from "./insight/rules";
import { detectSignals, type FpSignal } from "./insight/signals";
import { fetchSystemConsensus, type SysConsensusLite } from "./insight/systemSignal";
import {
  computeVolumeProfile,
  sessionStartMs,
  type ProfileMode,
  type VolumeProfile,
} from "./profile";
import InsightPanel from "./InsightPanel";
import GuideDrawer from "./GuideDrawer";

const TF_LIST: Timeframe[] = ["1m", "5m", "15m", "30m", "4h", "1d"];
// 契约：time 为毫秒时间戳
const TF_MS: Record<Timeframe, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "30m": 1_800_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
};
const LOOKBACK_BARS = 240;

// ── 实时态左滚历史回补（只走本地后端，零币安 REST）──
/** 每次向前翻页的根数（后端区间上限 1500 / bars limit 500，均富余） */
const BACKFILL_PAGE_BARS = 180;
/** 左滚触发阈值：最左可见柱索引小于该值时预取上一页 */
const BACKFILL_TRIGGER_BARS = 24;
/** 拉取失败后的冷却期（后端离线时避免每帧重试打爆） */
const BACKFILL_FAIL_COOLDOWN_MS = 5_000;

const VP_ON_KEY = "jarvis.fp.profile.on";
const VP_MODE_KEY = "jarvis.fp.profile.mode";

function readVpOn(): boolean {
  try {
    return localStorage.getItem(VP_ON_KEY) !== "off";
  } catch {
    return true;
  }
}
function readVpMode(): ProfileMode {
  try {
    return localStorage.getItem(VP_MODE_KEY) === "session" ? "session" : "visible";
  } catch {
    return "visible";
  }
}

/** 底部统计四行的大白话解释（悬停统计区时展示） */
const STATS_EXPLAIN: { title: string; what: string; how: string }[] = [
  {
    title: "成交量",
    what: "这根柱在这段时间内总共成交了多少（k=千，m=百万）。",
    how: "放量的柱代表多空分歧大、参考意义强；缩量的柱信号价值低，别过度解读。",
  },
  {
    title: "Delta（主动买 − 主动卖）",
    what: "正数（蓝底）= 主动买入更多，买方更急迫；负数（红底）= 主动卖出更多，卖方更急迫。",
    how: "连续多根同向 Delta 说明一方持续占优；Delta 与价格方向相反时（价涨Δ负）要警惕反转。",
  },
  {
    title: "Delta%（净买卖占比）",
    what: "Delta 占总成交量的百分比，剔除「量大但打平」的干扰。",
    how: "±25% 以上算显著失衡（背景色更深）；接近 0 说明多空势均力敌。",
  },
  {
    title: "累计Δ（资金潮水线）",
    what: "把每根柱的 Delta 逐柱累加，反映这段行情里买卖力量的总账。",
    how: "持续上行=买方一直占优。价格创新高但累计Δ不创新高（背离）是经典的顶部预警，反之亦然。",
  },
];

export default function FootprintChart() {
  // 跟随顶部导航的全局币种选择器（SymbolProvider）
  const { symbol } = useSymbol();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const barsRef = useRef<FootprintBar[]>([]);
  const tickRef = useRef(1);
  const sizeRef = useRef({ w: 0, h: 0 });
  const layoutRef = useRef<Layout | null>(null);
  const hoverRef = useRef<HoverInfo>(null);
  const dirtyRef = useRef(true);
  const signalsRef = useRef<FpSignal[]>([]);
  const badgeBoxesRef = useRef<BadgeBox[]>([]);

  const [tf, setTf] = useState<Timeframe>("1m");
  const [hover, setHover] = useState<HoverInfo>(null);
  const [mouse, setMouse] = useState({ x: 0, y: 0 });
  const [loading, setLoading] = useState(true);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [guideOpen, setGuideOpen] = useState(false);
  const [activeSignal, setActiveSignal] = useState<{ sig: FpSignal; x: number; y: number } | null>(null);
  // 加载兜底：超时/失败时给出具体原因 + 重试/降级操作，绝不无限「加载中」
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  // 历史回看：null=实时（币安直连滚动窗），'YYYY-MM-DD'=回看该日（走后端
  // tape_footprint_minutes 落库，保留期内任意日期；无实时推送）
  const [histDate, setHistDate] = useState<string | null>(null);

  // 实时态左滚回补：in-flight 防重入 + exhausted 边界停止 + gen 代际失效
  // （切币/切周期/重试后在途响应作废）+ cooldownUntil 失败冷却
  const backfillRef = useRef({ gen: 0, inFlight: false, exhausted: false, cooldownUntil: 0 });
  // 「已到本地数据起点」轻提示（几秒后自动消失）
  const [backfillHint, setBackfillHint] = useState<string | null>(null);
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    },
    [],
  );

  // 后端足迹区间查询只支持 1m/5m/15m/30m：进历史模式时把大周期收敛到 15m
  const enterHistory = useCallback((dateStr: string | null) => {
    setHistDate(dateStr);
    if (dateStr) setTf((cur) => (isHistoryTimeframe(cur) ? cur : "15m"));
  }, []);

  // Volume Profile：开关 + 聚合模式（localStorage 记住），计算结果走 ref 不进渲染
  const [vpOn, setVpOn] = useState(readVpOn);
  const [vpMode, setVpMode] = useState<ProfileMode>(readVpMode);
  const profileRef = useRef<VolumeProfile | null>(null);
  const vpOnRef = useRef(vpOn);
  const vpModeRef = useRef(vpMode);
  const vpRangeRef = useRef({ s: -1, e: -1, lastTime: 0 });
  const vpTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    vpOnRef.current = vpOn;
    try {
      localStorage.setItem(VP_ON_KEY, vpOn ? "on" : "off");
    } catch {
      // localStorage 不可用时忽略
    }
  }, [vpOn]);
  useEffect(() => {
    vpModeRef.current = vpMode;
    try {
      localStorage.setItem(VP_MODE_KEY, vpMode);
    } catch {
      // localStorage 不可用时忽略
    }
  }, [vpMode]);

  const markDirty = useCallback(() => {
    dirtyRef.current = true;
  }, []);

  const getGeom = useCallback(() => {
    const l = layoutRef.current;
    // 可见柱价格极值（轴双击/自动适配按钮用）
    let visLo = Infinity;
    let visHi = -Infinity;
    if (l) {
      const bars = barsRef.current;
      for (let i = l.visStart; i < l.visEnd; i++) {
        const b = bars[i];
        if (!b) continue;
        if (b.low < visLo) visLo = b.low;
        if (b.high > visHi) visHi = b.high;
      }
    }
    return {
      chartW: l?.chartW ?? Math.max(40, sizeRef.current.w - AXIS_W),
      maxScroll: l?.maxScroll ?? 0,
      centerPriceEff: l?.centerPrice ?? 0,
      plotH: l?.plotH ?? Math.max(40, sizeRef.current.h - TIME_H - STATS_H),
      tick: l?.tick ?? tickRef.current,
      visLo,
      visHi,
    };
  }, []);

  const { vpRef, bindTarget, resetFollow, following, syncFollow, autoFit } = useViewport(getGeom);

  // DEV-only 调试钩子：headless e2e 直接读视口物理状态断言（生产构建 tree-shake 掉）
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const w = window as unknown as { __fpDebug?: unknown };
    w.__fpDebug = { vpRef, barsRef, signalsRef, badgeBoxesRef, layoutRef, markDirty, profileRef };
    return () => {
      delete w.__fpDebug;
    };
  }, [vpRef, markDirty]);

  // 交互期间画面持续变化：wheel/pointer 事件直接改 ref，这里只负责置脏
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const mark = () => markDirty();
    wrap.addEventListener("wheel", mark, { passive: true });
    wrap.addEventListener("pointerdown", mark);
    wrap.addEventListener("pointermove", mark);
    return () => {
      wrap.removeEventListener("wheel", mark);
      wrap.removeEventListener("pointerdown", mark);
      wrap.removeEventListener("pointermove", mark);
    };
  }, [markDirty]);

  // 容器尺寸监听 + DPR 适配
  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ro = new ResizeObserver(() => {
      const r = wrap.getBoundingClientRect();
      const w = Math.max(1, Math.floor(r.width));
      const h = Math.max(1, Math.floor(r.height));
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      sizeRef.current = { w, h };
      markDirty();
    });
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [markDirty]);

  // 12 系统共识（解读面板「信号共振/分歧」条目）：接口不可用时保持 null 静默隐藏
  const sysConsRef = useRef<SysConsensusLite | null>(null);

  // 数据变化 → 重算信号 + 解读（节流：每柱一次而非每笔成交一次）
  const lastAnalyzedRef = useRef(0);
  const refreshAnalysis = useCallback(
    (force: boolean) => {
      const bars = barsRef.current;
      const last = bars[bars.length - 1];
      if (!last) return;
      if (!force && last.time === lastAnalyzedRef.current) return;
      lastAnalyzedRef.current = last.time;
      signalsRef.current = detectSignals(bars);
      setInsights(
        buildInsights(bars, tickRef.current, 10, sysConsRef.current, profileRef.current),
      );
      setActiveSignal(null);
    },
    [],
  );

  // 实时态左滚接近已载数据左边界：从本地后端按时间区间向前分页拉取并拼接
  // （与冷启动 seed 同一套 USD→币量换算；严禁触发币安 REST）。
  // 由 rAF 主循环在布局后调用，防重入/冷却/边界判定全在此内部收口。
  const requestOlderBars = useCallback(() => {
    const st = backfillRef.current;
    if (histDate || st.inFlight || st.exhausted) return;
    // 后端落库只对应真实源；mock 源的历史与后端数据无法拼接
    if (getFootprintSource() !== "real") return;
    if (Date.now() < st.cooldownUntil) return;
    const first = barsRef.current[0];
    if (!first) return;
    const gen = st.gen;
    st.inFlight = true;
    void (async () => {
      try {
        // 区间右端取首柱起点前 1ms：闭区间端点不会重复拉到首柱所在桶
        const toMs = first.time - 1;
        const fromMs = first.time - BACKFILL_PAGE_BARS * TF_MS[tf];
        const older = (SEED_FOOT_TFS as readonly string[]).includes(tf)
          ? await fetchFootprintRange(symbol, tf, fromMs, toMs)
          : await fetchOhlcRange(symbol, tf, fromMs, toMs);
        if (backfillRef.current.gen !== gen) return; // 切币/切周期后过期响应作废
        const { bars: merged, prepended } = mergeOlderBars(barsRef.current, older);
        if (prepended === 0) {
          // 区间内无更早数据：到达本地落库起点，优雅停止并轻提示
          st.exhausted = true;
          setBackfillHint("已到本地数据起点");
          if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
          hintTimerRef.current = setTimeout(() => setBackfillHint(null), 4_000);
          return;
        }
        barsRef.current = merged;
        // 前插使柱索引整体右移 prepended：补偿 scrollX 保持画面静止不跳
        // （follow 态 scrollX 由 maxScroll 派生，无需补偿）
        const vp = vpRef.current;
        if (!vp.follow) vp.scrollX += prepended * barWOf(vp.zoomX);
        tickRef.current = estimateTick(merged);
        refreshAnalysis(true);
        markDirty();
      } catch (e) {
        console.warn("[Footprint] 左滚历史回补失败，冷却后重试", e);
        backfillRef.current.cooldownUntil = Date.now() + BACKFILL_FAIL_COOLDOWN_MS;
      } finally {
        if (backfillRef.current.gen === gen) backfillRef.current.inFlight = false;
      }
    })();
  }, [symbol, tf, histDate, vpRef, markDirty, refreshAnalysis]);

  // Volume Profile 重算（200ms debounce）：visible 模式跟视口、session 模式跟今日全量。
  // 聚合是 O(可见柱×价位) 的纯计算且节流执行，拖拽/缩放期间不影响 60fps 主循环
  const scheduleProfileRecompute = useCallback(() => {
    if (vpTimerRef.current) return;
    vpTimerRef.current = setTimeout(() => {
      vpTimerRef.current = null;
      const bars = barsRef.current;
      const l = layoutRef.current;
      if (!vpOnRef.current || !l || bars.length === 0) {
        profileRef.current = null;
        markDirty();
        return;
      }
      let s: number;
      let e: number;
      if (vpModeRef.current === "session") {
        const t0 = sessionStartMs();
        s = bars.findIndex((b) => b.time >= t0);
        // 全部柱都早于今日 0 点（历史回看）：退化为全区间分布而非只取末柱
        if (s < 0) s = 0;
        e = bars.length;
      } else {
        s = l.visStart;
        e = l.visEnd;
      }
      profileRef.current = computeVolumeProfile(bars, s, e, tickRef.current);
      refreshAnalysis(true);
      markDirty();
    }, 200);
  }, [markDirty, refreshAnalysis]);

  // 开关/模式变化：开→立即重算，关→清空；卸载清 debounce 定时器
  useEffect(() => {
    if (vpOn) {
      vpRangeRef.current = { s: -1, e: -1, lastTime: 0 };
      scheduleProfileRecompute();
    } else {
      profileRef.current = null;
      refreshAnalysis(true);
      markDirty();
    }
  }, [vpOn, vpMode, symbol, tf, scheduleProfileRecompute, refreshAnalysis, markDirty]);
  useEffect(
    () => () => {
      if (vpTimerRef.current) {
        clearTimeout(vpTimerRef.current);
        // 必须复位，否则 StrictMode 卸载→重挂载后 schedule 永远被「已排队」挡住
        vpTimerRef.current = null;
      }
    },
    [],
  );

  // 共识拉取：切币立即拉一次 + 每 60s 刷新；结果落 ref，随下一根柱并入解读
  useEffect(() => {
    let disposed = false;
    sysConsRef.current = null;
    const pull = async () => {
      const c = await fetchSystemConsensus(symbol);
      if (disposed) return;
      const changed =
        (c?.direction ?? null) !== (sysConsRef.current?.direction ?? null) ||
        (c?.confidence ?? -1) !== (sysConsRef.current?.confidence ?? -1);
      sysConsRef.current = c;
      if (changed) refreshAnalysis(true);
    };
    void pull();
    const timer = setInterval(() => void pull(), 60_000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [symbol, refreshAnalysis]);

  // 切币种：价格量级完全不同（BTC 6.4 万 vs DOGE 0.12），必须重置视口，
  // 否则物化过的 centerPrice/scrollX 会让画面落在不存在的价位区域
  const prevSymbolRef = useRef(symbol);
  useEffect(() => {
    if (prevSymbolRef.current !== symbol) {
      prevSymbolRef.current = symbol;
      resetFollow();
    }
  }, [symbol, resetFollow]);

  // 数据接入：历史 getBars + 实时 subscribe（同 time 更新末柱，新 time 追加）
  useEffect(() => {
    let disposed = false;
    barsRef.current = [];
    signalsRef.current = [];
    lastAnalyzedRef.current = 0;
    setInsights([]);
    setLoading(true);
    setLoadError(null);
    setHover(null);
    hoverRef.current = null;
    setActiveSignal(null);
    // 左滚回补状态复位：gen+1 使在途响应作废，边界/冷却重新判定
    backfillRef.current = {
      gen: backfillRef.current.gen + 1,
      inFlight: false,
      exhausted: false,
      cooldownUntil: 0,
    };
    if (hintTimerRef.current) {
      clearTimeout(hintTimerRef.current);
      hintTimerRef.current = null;
    }
    setBackfillHint(null);
    markDirty();

    // ── 历史回看模式：后端落库区间查询，一次性加载、无实时订阅 ──
    if (histDate) {
      const tfEff = isHistoryTimeframe(tf) ? tf : "15m";
      (async () => {
        try {
          const { fromMs, toMs } = dayRangeMs(histDate);
          const bars = await fetchHistoryBars(symbol, tfEff, fromMs, toMs);
          if (disposed) return;
          barsRef.current = bars;
          tickRef.current = estimateTick(bars);
          if (bars.length === 0) {
            setLoadError(
              `${histDate} 后端库内暂无 ${symbol} 足迹数据——` +
                "历史数据仅在 jarvis 后端（WS 流）运行期间积累，足迹档位从本次升级后开始落库；" +
                "可换一个日期，或回到实时模式。",
            );
          }
          resetFollow();
        } catch (e) {
          if (!disposed) {
            const msg = e instanceof Error ? e.message : String(e);
            setLoadError(`历史足迹加载失败（${msg}）——请确认 jarvis 后端服务在运行。`);
          }
        } finally {
          if (!disposed) {
            setLoading(false);
            refreshAnalysis(true);
            markDirty();
          }
        }
      })();
      return () => {
        disposed = true;
      };
    }

    // 兜底计时：10s 仍无一根柱 → 弹出可操作的错误提示（后台加载继续，
    // 数据一旦到达提示自动消除；真实源冷启动正常也可能 >10s，故措辞为「缓慢/失败」）
    const failTimer = setTimeout(() => {
      if (!disposed && barsRef.current.length === 0) {
        setLoadError(
          getFootprintSource() === "real"
            ? "币安行情连接缓慢或失败——请检查网络/代理（fapi.binance.com 需可达），或先切换到模拟行情。"
            : "行情数据加载缓慢——模拟源初始化异常，可尝试重试。",
        );
      }
    }, 10_000);

    const applyLive = (bar: FootprintBar) => {
      if (disposed || bar.timeframe !== tf || bar.symbol !== symbol) return;
      const bars = barsRef.current;
      const last = bars[bars.length - 1];
      let isNewBar = false;
      if (last && bar.time === last.time) {
        bars[bars.length - 1] = bar;
      } else if (!last || bar.time > last.time) {
        bars.push(bar);
        isNewBar = true;
        // 只在跟随最新时裁剪左端：浏览历史（含左滚回补的前插数据）期间裁剪
        // 会让柱索引左移导致画面跳动、回补数据被丢；follow 态 scrollX 由
        // maxScroll 派生，裁剪无感知
        if (vpRef.current.follow && bars.length > 1500) bars.splice(0, bars.length - 1000);
      } else {
        // 乱序旧柱：定位替换（如聚合器补发上一根收尾快照）
        const i = bars.findIndex((b) => b.time === bar.time);
        if (i >= 0) bars[i] = bar;
        else return;
      }
      setLoadError(null); // 有数据抵达即撤下错误提示
      tickRef.current = estimateTick(bars);
      refreshAnalysis(isNewBar);
      markDirty();
    };

    // 先订阅再拉历史，避免两步之间漏推送；历史落地时与已收实时柱按 time 去重合并
    const pending: FootprintBar[] = [];
    let historyReady = false;
    const unsub = footprintDataService.subscribe(symbol, tf, (bar) => {
      if (historyReady) applyLive(bar);
      else pending.push(bar);
    });

    (async () => {
      const now = Date.now();
      try {
        const bars = await footprintDataService.getBars(
          symbol,
          tf,
          now - LOOKBACK_BARS * TF_MS[tf],
          now,
        );
        if (disposed) return;
        barsRef.current = [...bars].sort((a, b) => a.time - b.time);
        tickRef.current = estimateTick(barsRef.current);
        if (barsRef.current.length > 0) setLoadError(null);
      } catch (e) {
        console.error("[Footprint] getBars 失败", e);
        if (!disposed && barsRef.current.length === 0) {
          const msg = e instanceof Error ? e.message : String(e);
          setLoadError(
            e instanceof BinanceBanError
              ? `${msg}。解除前请勿反复重试（会延长封禁），可先切换到模拟行情。`
              : getFootprintSource() === "real"
                ? `币安行情拉取失败（${msg}）——请检查网络/代理，或切换到模拟行情。`
                : `行情数据拉取失败（${msg}）。`,
          );
        }
      }
      historyReady = true;
      setLoading(false);
      for (const bar of pending) applyLive(bar);
      pending.length = 0;
      refreshAnalysis(true);
      markDirty();
    })();

    return () => {
      disposed = true;
      clearTimeout(failTimer);
      unsub();
    };
  }, [symbol, tf, histDate, reloadKey, markDirty, refreshAnalysis, resetFollow, vpRef]);

  // rAF 主循环：物理推进（惯性/缩放插值）+ 脏帧重绘，全程零 React 渲染
  useEffect(() => {
    let raf = 0;
    let prevT = performance.now();
    const loop = (t: number) => {
      raf = requestAnimationFrame(loop);
      const dt = Math.min(0.05, (t - prevT) / 1000);
      prevT = t;

      const physicsChanged = stepViewport(vpRef.current, getGeom(), dt, syncFollow);
      if (physicsChanged) dirtyRef.current = true;
      if (!dirtyRef.current) return;

      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      if (!canvas || !ctx) return;
      dirtyRef.current = false;
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const l = computeLayout(
        vpRef.current,
        barsRef.current,
        sizeRef.current.w,
        sizeRef.current.h,
        tickRef.current,
      );
      layoutRef.current = l;

      // 左滚接近已载数据左边界 → 按需向前分页回补（防重入/边界/冷却在内部收口）
      if (l.visStart <= BACKFILL_TRIGGER_BARS && barsRef.current.length > 0) {
        requestOlderBars();
      }

      // 可见范围或末柱变化 → 调度 VP 重算（debounce 内合并，绘制用旧缓存不卡帧）
      if (vpOnRef.current) {
        const r = vpRangeRef.current;
        const lastT = barsRef.current[barsRef.current.length - 1]?.time ?? 0;
        if (l.visStart !== r.s || l.visEnd !== r.e || lastT !== r.lastTime) {
          r.s = l.visStart;
          r.e = l.visEnd;
          r.lastTime = lastT;
          scheduleProfileRecompute();
        }
      }

      badgeBoxesRef.current = drawFrame(
        ctx,
        l,
        barsRef.current,
        hoverRef.current,
        signalsRef.current,
        vpOnRef.current ? profileRef.current : null,
      );
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [vpRef, getGeom, syncFollow, scheduleProfileRecompute, requestOlderBars]);

  const onMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      setMouse({ x, y });
      // 拖拽/惯性期间不做 hover 命中（省计算且避免 tooltip 闪烁）
      const vp = vpRef.current;
      if (vp.dragging) return;
      const l = layoutRef.current;
      const cell = l ? hitTest(l, barsRef.current, x, y) : null;
      hoverRef.current = cell;
      setHover(cell);
      markDirty();
    },
    [markDirty, vpRef],
  );

  const onMouseLeave = useCallback(() => {
    hoverRef.current = null;
    setHover(null);
    markDirty();
  }, [markDirty]);

  // 点击：优先命中信号徽标。
  // 绑在容器 div（而非 canvas）上：拖拽的 setPointerCapture 会把 click 的
  // target 重定向到捕获元素（容器），canvas 上的 onClick 在捕获后收不到。
  const clickStartRef = useRef<{ x: number; y: number } | null>(null);
  const onPointerDownCapturePos = useCallback((e: React.PointerEvent) => {
    clickStartRef.current = { x: e.clientX, y: e.clientY };
  }, []);
  const onClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    // 拖拽松手也会派发 click：位移超过阈值不算点击
    const s = clickStartRef.current;
    if (s && Math.hypot(e.clientX - s.x, e.clientY - s.y) > 6) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    for (const b of badgeBoxesRef.current) {
      if ((x - b.x) ** 2 + (y - b.y) ** 2 <= b.r ** 2) {
        setActiveSignal({ sig: b.signal, x: b.x, y: b.y });
        return;
      }
    }
    setActiveSignal(null);
  }, []);

  const bars = barsRef.current;
  const hoverBar: FootprintBar | undefined =
    hover ? bars[hover.barIndex] : undefined;
  const tick = tickRef.current;

  // ---------- 悬停解释浮层（大白话版） ----------
  let tooltip: React.ReactNode = null;
  const tooltipStyle = (w: number, h: number): React.CSSProperties => ({
    left: Math.min(mouse.x + 14, Math.max(0, sizeRef.current.w - w)),
    top: Math.max(4, Math.min(mouse.y + 14, sizeRef.current.h - h)),
    background: "rgba(7,22,34,0.96)",
    borderColor: COLORS.border,
    color: COLORS.text,
    width: w,
  });

  if (hover?.kind === "cell" && hover.level && hoverBar) {
    const lv: PriceLevel = hover.level;
    const cellDelta = lv.askVol - lv.bidVol;
    const buyStrong = lv.askVol >= lv.bidVol;
    const askImb = lv.askVol >= IMBALANCE_RATIO * Math.max(1, lv.bidVol);
    const bidImb = lv.bidVol >= IMBALANCE_RATIO * Math.max(1, lv.askVol);
    const ratio = buyStrong
      ? lv.askVol / Math.max(1, lv.bidVol)
      : lv.bidVol / Math.max(1, lv.askVol);
    // 分布节点提示：该价位命中 HVN（磁吸）/LVN（真空）时附一句
    const vp = vpOn ? profileRef.current : null;
    const nearTick = (arr: number[]) => arr.some((p) => Math.abs(p - lv.price) < tick / 2);
    const nodeHint = vp
      ? nearTick(vp.hvns)
        ? "此价位是分布图上的高量节点（HVN）——历史换手密集，价格常被吸回或在此停留。"
        : nearTick(vp.lvns)
          ? "此价位是分布图上的低量真空区（LVN）——缺少历史换手，价格常快速穿越，不宜指望它挡住行情。"
          : null
      : null;
    tooltip = (
      <div
        className="pointer-events-none absolute z-10 rounded-lg border px-3 py-2.5 text-[11px] leading-5 shadow-xl"
        style={tooltipStyle(240, nodeHint ? 214 : 170)}
      >
        <div className="font-mono text-slate-400">
          价位 {fmtPrice(lv.price, tick)}
          {hover.isPoc && <span style={{ color: COLORS.poc }}> · POC 主战场</span>}
        </div>
        <div className="font-mono">
          <span style={{ color: COLORS.downText }}>左·主动卖 {fmtK(lv.bidVol)}</span>
          <span className="mx-1.5 text-slate-500">×</span>
          <span style={{ color: COLORS.upText }}>右·主动买 {fmtK(lv.askVol)}</span>
        </div>
        <p className="mt-1 text-slate-300">
          {buyStrong
            ? `这个价位买方吃单是卖方的 ${ratio.toFixed(1)} 倍——买方更急迫，${askImb ? "已达失衡级别（绿框），买方碾压，短期看涨信号。" : "略占上风。"}`
            : `这个价位卖方出货是买方的 ${ratio.toFixed(1)} 倍——卖方更急迫，${bidImb ? "已达失衡级别（绿框），卖方碾压，短期看跌信号。" : "略占上风。"}`}
        </p>
        <p className="mt-0.5 text-slate-500">
          格Δ {fmtK(cellDelta)} · 所在柱 Vol {fmtK(hoverBar.totalVol)} / Δ{" "}
          {fmtK(hoverBar.delta)}
          {hover.isPoc && " · 黄框价位后续常成支撑/压力"}
        </p>
        {nodeHint && (
          <p className="mt-0.5" style={{ color: "#93c5fd" }}>
            {nodeHint}
          </p>
        )}
      </div>
    );
  } else if (hover?.kind === "stats" && hoverBar) {
    const ex = STATS_EXPLAIN[hover.row];
    const vals = [
      fmtK(hoverBar.totalVol),
      fmtK(hoverBar.delta),
      `${hoverBar.totalVol > 0 ? ((hoverBar.delta / hoverBar.totalVol) * 100).toFixed(0) : 0}%`,
      fmtK(hoverBar.cumDelta),
    ];
    tooltip = (
      <div
        className="pointer-events-none absolute z-10 rounded-lg border px-3 py-2.5 text-[11px] leading-5 shadow-xl"
        style={tooltipStyle(250, 150)}
      >
        <div className="font-medium" style={{ color: COLORS.text }}>
          {ex.title}：<span className="font-mono">{vals[hover.row]}</span>
        </div>
        <p className="mt-1 text-slate-300">{ex.what}</p>
        <p className="mt-0.5 text-slate-500">怎么用：{ex.how}</p>
      </div>
    );
  }

  // ---------- 信号徽标点击弹层 ----------
  const signalPopover = activeSignal && (
    <div
      className="absolute z-20 w-[260px] rounded-lg border p-3 text-[11px] leading-5 shadow-2xl"
      style={{
        left: Math.min(activeSignal.x + 12, Math.max(0, sizeRef.current.w - 280)),
        top: Math.max(8, Math.min(activeSignal.y - 20, sizeRef.current.h - 220)),
        background: "rgba(7,22,34,0.97)",
        borderColor:
          activeSignal.sig.type === "sweep"
            ? COLORS.sweep
            : activeSignal.sig.type === "divergence"
              ? COLORS.divergence
              : activeSignal.sig.side === "buy"
                ? COLORS.up
                : COLORS.down,
      }}
    >
      <div className="flex items-center">
        <span className="font-semibold" style={{ color: COLORS.text }}>
          {activeSignal.sig.title}
        </span>
        <button
          onClick={() => setActiveSignal(null)}
          className="ml-auto rounded p-0.5 hover:bg-white/10"
          style={{ color: COLORS.dim }}
          aria-label="关闭"
        >
          <X size={12} />
        </button>
      </div>
      <p className="mt-1.5" style={{ color: COLORS.text }}>
        {activeSignal.sig.what}
      </p>
      <p className="mt-1.5 text-slate-400">
        <span className="text-slate-300">通常意味着：</span>
        {activeSignal.sig.meaning}
      </p>
      <p
        className="mt-1.5 rounded border-l-2 pl-2"
        style={{ borderColor: COLORS.poc, color: "#fde047" }}
      >
        风险：{activeSignal.sig.risk}
      </p>
    </div>
  );

  return (
    <div
      className="relative flex h-full min-h-0 flex-col overflow-hidden rounded-xl border"
      style={{ background: COLORS.bg, borderColor: COLORS.border }}
    >
      {/* 工具条 */}
      <div
        className="flex shrink-0 items-center gap-3 border-b px-3 py-2"
        style={{ borderColor: COLORS.border }}
      >
        <span className="text-sm font-semibold" style={{ color: COLORS.text }}>
          订单流足迹
        </span>
        <span
          className="rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold"
          style={{ background: "rgba(37,99,235,0.15)", color: "#93c5fd" }}
          title="跟随顶部导航的币种选择器"
        >
          {symbol.replace(/USDT$/, "/USDT")}
        </span>
        <div
          className="flex overflow-hidden rounded-md border"
          style={{ borderColor: COLORS.border }}
        >
          {TF_LIST.map((t) => {
            const histBlocked = histDate !== null && !isHistoryTimeframe(t);
            return (
              <button
                key={t}
                onClick={() => {
                  if (!histBlocked) setTf(t);
                }}
                className="px-2.5 py-1 font-mono text-[11px] transition-colors"
                title={histBlocked ? "历史回看仅支持 1m/5m/15m/30m（后端落库粒度）" : undefined}
                style={
                  t === tf
                    ? { background: "rgba(37,99,235,0.25)", color: "#bfdbfe" }
                    : histBlocked
                      ? { color: COLORS.dim, opacity: 0.35, cursor: "not-allowed" }
                      : { color: COLORS.dim }
                }
              >
                {t}
              </button>
            );
          })}
        </div>

        {/* 日期回看：实时（币安直连）/ 今日 / 昨日 / 自定义日期（后端落库历史） */}
        <div
          className="flex items-center overflow-hidden rounded-md border"
          style={{ borderColor: histDate ? "rgba(96,165,250,0.5)" : COLORS.border }}
        >
          {(
            [
              { label: "实时", date: null },
              { label: "今日", date: fmtLocalDate(new Date()) },
              { label: "昨日", date: fmtLocalDate(new Date(Date.now() - 86_400_000)) },
            ] as const
          ).map((opt) => (
            <button
              key={opt.label}
              onClick={() => enterHistory(opt.date)}
              className="px-2 py-1 text-[11px] transition-colors"
              title={
                opt.date === null
                  ? "币安实时行情（滚动窗口）"
                  : "从 jarvis 后端落库加载该日足迹（保留期内可回看）"
              }
              style={
                histDate === opt.date
                  ? { background: "rgba(37,99,235,0.25)", color: "#bfdbfe" }
                  : { color: COLORS.dim }
              }
            >
              {opt.label}
            </button>
          ))}
          <input
            type="date"
            value={histDate ?? ""}
            max={fmtLocalDate(new Date())}
            onChange={(e) => enterHistory(e.target.value || null)}
            className="border-l bg-transparent px-1.5 py-1 text-[11px] outline-none [color-scheme:dark]"
            style={{ borderColor: COLORS.border, color: histDate ? "#bfdbfe" : COLORS.dim }}
            title="回看任意历史日期（后端保留期内）"
          />
        </div>
        {histDate && (
          <span
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold"
            style={{ background: "rgba(234,179,8,0.14)", color: "#fde047" }}
            title="历史回看走 jarvis 后端落库（金额单位 USD 名义额）；无实时推送，切「实时」恢复"
          >
            <History size={11} />
            历史回看 {histDate}
          </span>
        )}

        <div className="hidden items-center gap-3 text-[10px] lg:flex" style={{ color: COLORS.dim }}>
          <span title="格子右列，蓝底越深买方越强">
            <i className="mr-1 inline-block h-2 w-2 rounded-[2px]" style={{ background: "rgba(37,99,235,0.7)" }} />
            主动买
          </span>
          <span title="格子左列，红底越深卖方越强">
            <i className="mr-1 inline-block h-2 w-2 rounded-[2px]" style={{ background: "rgba(239,68,68,0.7)" }} />
            主动卖
          </span>
          <span title={`一侧吃单 ≥ 对侧 ${IMBALANCE_RATIO} 倍，单方面碾压`}>
            <i className="mr-1 inline-block h-2 w-2 rounded-[2px] border" style={{ borderColor: COLORS.imbalance }} />
            失衡≥{IMBALANCE_RATIO}x
          </span>
          <span title="本柱成交量最大的价位，常成支撑/压力">
            <i className="mr-1 inline-block h-2 w-2 rounded-[2px] border" style={{ borderColor: COLORS.poc }} />
            POC
          </span>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setVpOn(!vpOn)}
            className="flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors hover:bg-white/5"
            style={
              vpOn
                ? { borderColor: "rgba(96,165,250,0.5)", background: "rgba(37,99,235,0.18)", color: "#bfdbfe" }
                : { borderColor: COLORS.border, color: COLORS.dim }
            }
            title="成交量分布（钟形曲线）：横向条=该价位累计成交量，黄线=POC，蓝带=价值区 70%"
          >
            <BarChart3 size={12} />
            分布
          </button>
          {vpOn && (
            <div
              className="flex overflow-hidden rounded-md border"
              style={{ borderColor: COLORS.border }}
            >
              {(["visible", "session"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setVpMode(m)}
                  className="px-2 py-1 text-[10px] transition-colors"
                  style={
                    m === vpMode
                      ? { background: "rgba(37,99,235,0.25)", color: "#bfdbfe" }
                      : { color: COLORS.dim }
                  }
                  title={m === "visible" ? "聚合当前可见范围的柱" : "聚合今日 0 点至今全部柱"}
                >
                  {m === "visible" ? "可见范围" : "今日"}
                </button>
              ))}
            </div>
          )}
          <button
            onClick={() => {
              autoFit();
              markDirty();
            }}
            className="flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors hover:bg-white/5"
            style={{ borderColor: COLORS.border, color: COLORS.text }}
            title="纵向自动装下可见柱的价格范围（也可双击价格轴/时间轴触发）"
          >
            <Maximize2 size={12} />
            自动适配
          </button>
          <button
            onClick={() => setGuideOpen(true)}
            className="flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors hover:bg-white/5"
            style={{ borderColor: COLORS.border, color: COLORS.text }}
          >
            <BookOpen size={12} />
            读图指南
          </button>
          {!following && (
            <button
              onClick={() => {
                resetFollow();
                markDirty();
              }}
              className="rounded border px-2 py-1 text-[11px] transition-colors hover:bg-white/5"
              style={{ borderColor: COLORS.border, color: COLORS.text }}
            >
              回到最新 →
            </button>
          )}
        </div>
      </div>

      {/* 主体：图表 + 解读面板 */}
      <div className="flex min-h-0 flex-1">
        <div
          ref={(node) => {
            wrapRef.current = node;
            bindTarget(node);
          }}
          className="relative min-h-0 min-w-0 flex-1 cursor-crosshair touch-none"
          onPointerDown={onPointerDownCapturePos}
          onClick={onClick}
        >
          <canvas
            ref={canvasRef}
            className="block"
            onMouseMove={onMouseMove}
            onMouseLeave={onMouseLeave}
          />
          {tooltip}
          {signalPopover}
          {backfillHint && (
            <div
              className="pointer-events-none absolute bottom-14 left-2 z-10 rounded border px-2 py-1 text-[11px] shadow-lg"
              style={{
                background: "rgba(7,22,34,0.92)",
                borderColor: COLORS.border,
                color: COLORS.dim,
              }}
            >
              {backfillHint}
            </div>
          )}
          {loading && !loadError && (
            <div
              className="absolute inset-0 flex items-center justify-center text-xs"
              style={{ color: COLORS.dim, background: "rgba(11,30,45,0.6)" }}
            >
              {histDate ? `加载 ${histDate} 历史足迹…` : "加载足迹数据…"}
            </div>
          )}
          {loadError && (
            <div
              className="absolute inset-0 z-10 flex items-center justify-center"
              style={{ background: "rgba(11,30,45,0.72)" }}
            >
              <div
                className="w-[380px] max-w-[90%] rounded-xl border p-4 shadow-2xl"
                style={{ background: "#0a1b29", borderColor: "rgba(234,179,8,0.45)" }}
              >
                <div className="flex items-center gap-2">
                  <AlertTriangle size={16} style={{ color: "#fde047" }} />
                  <span className="text-sm font-semibold" style={{ color: COLORS.text }}>
                    行情数据加载异常
                  </span>
                </div>
                <p className="mt-2 text-xs leading-5" style={{ color: COLORS.dim }}>
                  {loadError}
                </p>
                <div className="mt-3 flex gap-2">
                  <button
                    onClick={() => {
                      setLoadError(null);
                      setReloadKey((k) => k + 1);
                    }}
                    className="flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs transition-colors hover:bg-white/5"
                    style={{ borderColor: COLORS.border, color: COLORS.text }}
                  >
                    <RefreshCw size={12} />
                    重试
                  </button>
                  {histDate && (
                    <button
                      onClick={() => enterHistory(null)}
                      className="flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs transition-colors hover:bg-white/5"
                      style={{ borderColor: COLORS.border, color: COLORS.text }}
                    >
                      回到实时
                    </button>
                  )}
                  {!histDate && getFootprintSource() === "real" && (
                    <button
                      onClick={() => {
                        setFootprintSource("mock");
                        location.reload(); // 数据服务单例按源开关择一，需刷新重建
                      }}
                      className="flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs transition-colors hover:bg-white/5"
                      style={{
                        borderColor: "rgba(96,165,250,0.5)",
                        background: "rgba(37,99,235,0.15)",
                        color: "#93c5fd",
                      }}
                    >
                      切换到模拟行情（降级）
                    </button>
                  )}
                </div>
                <p className="mt-2 text-[10px] leading-4" style={{ color: COLORS.dim }}>
                  后台仍在尝试连接，数据一旦到达此提示会自动消失。模拟行情仅用于界面体验，非真实成交。
                </p>
              </div>
            </div>
          )}
        </div>
        <InsightPanel insights={insights} />
      </div>

      <div
        className="flex shrink-0 items-center justify-between border-t px-3 py-1 text-[10px]"
        style={{ borderColor: COLORS.border, color: COLORS.dim }}
      >
        <span>
          拖拽平移（松手惯性滑行）· 滚轮平滑缩放 · Shift+滚轮横移 · 双击回到最新 ·
          悬停任意格子/统计行看白话解释
        </span>
        <span>徽标：⚡扫盘 ≣堆积 ◈背离（点击看含义）</span>
      </div>
      {guideOpen && <GuideDrawer onClose={() => setGuideOpen(false)} />}
    </div>
  );
}
