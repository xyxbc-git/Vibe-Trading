import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  BookOpenCheck,
  Fingerprint,
  LayoutGrid,
  List,
  Radio,
  Target,
  Users,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { isStaleEcho } from "@/lib/chartView";
import { calcMACD } from "@/lib/indicators";
import {
  api,
  formatPrice,
  type DepthBucket,
  type DepthOrderbookResponse,
  type FootprintActors,
  type FootprintRow,
  type TapeActor,
  type TapeFingerprint,
  type TapeFlowResponse,
  type TapeFootprintResponse,
  type TapeTrade,
} from "@/api/client";
import KlineChart from "@/components/charts/KlineChart";
import TrapReasonCard from "@/components/cards/TrapReasonCard";
import {
  mockTrapSignals,
  buildTrapMarks,
  TRAP_TOGGLE_KEY,
  type TrapSignalsResponse,
  type TrapBar,
  type TrapMark,
} from "@/lib/trapSignals";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";

/**
 * 盘口透视页（/depth）：
 *   ② 左主区 K 线（复用 KlineChart，不加 overlay）+ 可折叠 MACD(12,26,9) 副图
 *   ③ 右侧 DOM 深度阶梯（REST 快照聚合价格桶，一行一桶：左红卖 / 右绿买）
 *   ④ 下方成交流画像（柱状图视图：买卖额镜像柱 + 净额线；列表视图：主力判定 + 指纹聚合 + 实时成交列表）
 * 币种跟随全局 useSymbol（Header 已有切币器）。
 * 周期强制一致：全页只有顶部一个主周期开关（1m~1d 七档），足迹图周期与
 * 成交流画像窗口始终跟随主周期（足迹图超 30m 回退 30m），不提供独立周期
 * 入口，杜绝上下面板周期不一致的状态。
 */

/* ────────────────────────── 常量与工具 ────────────────────────── */

/** 本页支持的 K 线周期（完整档位；主图/MACD 直接透传交易所标准 interval） */
const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

/** 足迹图支持的周期档（档数×柱数渲染开销大，只开短周期） */
const FP_INTERVALS = ["1m", "5m", "15m", "30m"] as const;
type FootprintInterval = (typeof FP_INTERVALS)[number];

/** 成交流画像窗口（分钟）随主周期的映射：周期放大 → 观察窗口放大。
 * 后端 /tape/flow 对 window_min 有 240min 硬上限（实测超限被钳回 240），
 * 故 4h/1d 档直接映射上限值，标题展示与实际生效保持一致。 */
const TF_WINDOW_MIN: Record<Timeframe, number> = {
  "1m": 15,
  "5m": 30,
  "15m": 60,
  "30m": 120,
  "1h": 240,
  "4h": 240,
  "1d": 240,
};
/** 足迹图只开到 30m（渲染开销）：跟随主周期遇 1h/4h/1d 回退到 30m */
function fpFollow(tf: Timeframe): FootprintInterval {
  return (FP_INTERVALS as readonly string[]).includes(tf)
    ? (tf as FootprintInterval)
    : "30m";
}

/** 成交流区视图（足迹图 | 列表），localStorage 持久（历史存的 "bars" 自动回退默认值） */
type TapeView = "footprint" | "list";
const TAPE_VIEW_KEY = "jarvis.depth.tapeView";
/** MACD 副图开关，localStorage 持久（默认关，零开销） */
const MACD_KEY = "jarvis.depth.macd";

function readStored<T extends string>(key: string, valid: readonly T[], fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    if (v && (valid as readonly string[]).includes(v)) return v as T;
  } catch {
    // localStorage 不可用时用默认值
  }
  return fallback;
}

/** 四类成交主体的展示顺序与配色（散户灰 / 中户蓝 / 机构紫 / 做市商青） */
const ACTOR_ORDER: TapeActor[] = ["retail", "mid", "inst", "maker"];
const ACTOR_STYLE: Record<TapeActor, { bar: string; badge: string }> = {
  retail: {
    bar: "bg-gray-500",
    badge: "bg-gray-500/15 text-gray-400 border-gray-500/30",
  },
  mid: {
    bar: "bg-sky-500",
    badge: "bg-sky-500/15 text-sky-400 border-sky-500/40",
  },
  inst: {
    bar: "bg-purple-500",
    badge: "bg-purple-500/20 text-purple-300 border-purple-500/50",
  },
  maker: {
    bar: "bg-cyan-500",
    badge: "bg-cyan-500/20 text-cyan-300 border-cyan-500/50",
  },
};

/** 主力判定动作 → 语义色（砸盘红 / 拉盘绿 / 吸筹蓝 / 派发橙 / 中性灰） */
function actionColorCls(action: string): string {
  if (action.includes("砸")) return "text-jarvis-red";
  if (action.includes("拉")) return "text-jarvis-green";
  if (action.includes("吸")) return "text-sky-400";
  if (action.includes("派") || action.includes("出货")) return "text-orange-400";
  return "text-jarvis-text-secondary";
}

/** 名义额紧凑格式：$1.2K / $3.45M / $1.02B */
function fmtUsd(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

/** 带符号名义额（净额列：正绿负红由外层上色，这里只负责 +/- 前缀） */
function fmtSignedUsd(v: number): string {
  return `${v >= 0 ? "+" : "-"}${fmtUsd(Math.abs(v))}`;
}

/** 币量格式：大数取整、常规 3 位小数、小币种最多 6 位 */
function fmtQty(q: number): string {
  if (q >= 1000) return q.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (q >= 1) return q.toLocaleString("en-US", { maximumFractionDigits: 3 });
  return q.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

/** 毫秒时间戳 → HH:mm:ss（实时成交列表用） */
function timeHms(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** 毫秒时间戳 → HH:mm（指纹活跃时段用） */
function timeHm(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 主体分类徽章：机构紫底 / 做市商青底，散户中户弱化底色 */
function ClsBadge({ cls, clsCn }: { cls: TapeActor; clsCn: string }) {
  return (
    <span
      className={clsx(
        "px-1 py-px rounded border text-[9px] whitespace-nowrap leading-none",
        ACTOR_STYLE[cls].badge,
      )}
    >
      {clsCn}
    </span>
  );
}

/* ────────────────────────── ③ DOM 深度阶梯 ────────────────────────── */

/**
 * 单个价格桶行：一根柱子左卖右买——
 * 左半边红色 bar=卖单挂单量（右端锚定中线向左生长），
 * 右半边绿色 bar=买单挂单量（左端锚定中线向右生长），
 * bar 宽按 usd / 最大桶 usd 归一化；文字层价格居中、名义额贴各自半边。
 */
function BucketRow({
  kind,
  b,
  maxUsd,
}: {
  kind: "ask" | "bid";
  b: DepthBucket;
  maxUsd: number;
}) {
  const pct = Math.min(100, (b.usd / maxUsd) * 100);
  return (
    <div
      className="relative h-[19px]"
      title={`价格 ${formatPrice(b.price)} · 挂单 ${fmtQty(b.qty)} ≈ ${fmtUsd(b.usd)} · 向外累计 ${fmtUsd(b.cum_usd)}`}
    >
      {/* 左半：卖单 bar（红） */}
      <div className="absolute inset-y-[2.5px] right-1/2 w-1/2 flex justify-end">
        {kind === "ask" && (
          <div
            className="h-full rounded-l-sm bg-jarvis-red/25 border-r-2 border-jarvis-red/70"
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      {/* 右半：买单 bar（绿） */}
      <div className="absolute inset-y-[2.5px] left-1/2 w-1/2">
        {kind === "bid" && (
          <div
            className="h-full rounded-r-sm bg-jarvis-green/25 border-l-2 border-jarvis-green/70"
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
      {/* 文字层：卖额靠左 / 价格居中 / 买额靠右 */}
      <div className="absolute inset-0 grid grid-cols-[1fr_auto_1fr] items-center gap-1 px-1 text-[10px] font-mono leading-none">
        <span className="text-left text-jarvis-red/90">
          {kind === "ask" ? fmtUsd(b.usd) : ""}
        </span>
        <span className="text-center text-jarvis-text-secondary">
          {formatPrice(b.price)}
        </span>
        <span className="text-right text-jarvis-green/90">
          {kind === "bid" ? fmtUsd(b.usd) : ""}
        </span>
      </div>
    </div>
  );
}

/**
 * DOM 深度阶梯（纯 div 渲染，非 chart 库）：
 * 垂直价格轴从上到下 = 卖盘价降序（asks 倒序）→ mid 高亮行 → 买盘价降序；
 * 顶部小卡展示 10 档买卖失衡比 / market 标签 / stale 黄色提示。
 */
function DepthLadder({
  depth,
  loading,
  error,
  symbol,
}: {
  depth: DepthOrderbookResponse | null;
  loading: boolean;
  error: string | null;
  symbol: string;
}) {
  const ladderRef = useRef<HTMLDivElement>(null);
  const midRowRef = useRef<HTMLDivElement>(null);
  // 已把 mid 居中过的币种 key：每币种只自动居中一次，不打扰用户后续滚动
  const centeredKeyRef = useRef<string>("");

  // 接口卖盘为价格升序（近 mid → 远），倒序后顶部=最远卖价、紧邻 mid=最优卖价
  const asksDesc = useMemo(() => [...(depth?.asks ?? [])].reverse(), [depth]);
  const bids = depth?.bids ?? []; // 已为价格降序：最优买价紧贴 mid
  // bar 归一化分母：两侧全部桶的最大名义额
  const maxUsd = useMemo(() => {
    const all = [...(depth?.asks ?? []), ...(depth?.bids ?? [])];
    return all.length > 0 ? Math.max(...all.map((b) => b.usd), 1) : 1;
  }, [depth]);

  // 每币种首次拿到快照时把 mid 行滚到可视区中央
  useEffect(() => {
    if (!depth?.ok) return;
    const key = depth.symbol ?? symbol;
    if (centeredKeyRef.current === key) return;
    const box = ladderRef.current;
    const mid = midRowRef.current;
    if (!box || !mid) return;
    box.scrollTop = Math.max(
      0,
      mid.offsetTop - box.clientHeight / 2 + mid.clientHeight / 2,
    );
    centeredKeyRef.current = key;
  }, [depth, symbol]);

  const ratio = depth?.imbalance?.ratio ?? null;
  const ratioCls =
    ratio == null
      ? "text-jarvis-text-secondary"
      : ratio > 1
        ? "text-jarvis-green"
        : ratio < 1
          ? "text-jarvis-red"
          : "text-jarvis-text-secondary";
  const ratioNote =
    ratio == null
      ? "暂无失衡数据"
      : ratio > 1
        ? "下方承接强"
        : ratio < 1
          ? "上方压单重"
          : "买卖均衡";

  return (
    <div className="card p-3 flex flex-col min-w-0">
      {/* 标题行：market 标签 + 桶宽 + stale 提示 */}
      <p className="stat-label mb-2 flex items-center gap-1.5 text-xs flex-wrap">
        <BookOpenCheck size={14} />
        深度阶梯
        {depth?.ok && (
          <span className="text-[10px] px-1.5 py-0.5 rounded border bg-jarvis-blue/10 text-jarvis-blue border-jarvis-blue/40">
            {depth.market === "spot" ? "现货簿" : "合约簿"}
          </span>
        )}
        {depth?.ok && depth.bucket != null && (
          <span className="text-[10px] text-jarvis-text-secondary/70 font-mono">
            桶宽 {depth.bucket}
          </span>
        )}
        {depth?.stale && (
          <span className="text-[10px] px-1.5 py-0.5 rounded border bg-jarvis-yellow/10 text-jarvis-yellow border-jarvis-yellow/40 flex items-center gap-1">
            <AlertTriangle size={9} />
            快照滞后
          </span>
        )}
      </p>

      {/* 顶部小卡：近 10 档买卖失衡比（>1 下方承接强 / <1 上方压单重） */}
      {depth?.ok && depth.imbalance && (
        <div className="flex items-center justify-between rounded-lg bg-jarvis-bg/60 border border-jarvis-border px-2.5 py-1.5 mb-2">
          <div className="text-[10px] text-jarvis-text-secondary leading-tight">
            <p className="mb-0.5">10 档失衡（买 / 卖）</p>
            <p className="font-mono">
              <span className="text-jarvis-green">
                {fmtUsd(depth.imbalance.bid_usd_10)}
              </span>
              {" / "}
              <span className="text-jarvis-red">
                {fmtUsd(depth.imbalance.ask_usd_10)}
              </span>
            </p>
          </div>
          <div className="text-right">
            <p
              className={clsx(
                "text-base font-mono font-semibold leading-none",
                ratioCls,
              )}
            >
              {ratio != null ? ratio.toFixed(2) : "--"}
            </p>
            <p className={clsx("text-[10px] mt-0.5", ratioCls)}>{ratioNote}</p>
          </div>
        </div>
      )}

      {/* 列头 */}
      <div className="grid grid-cols-[1fr_auto_1fr] px-1 pb-1 text-[9px] text-jarvis-text-secondary/70">
        <span className="text-left">← 卖盘挂单额</span>
        <span>价格</span>
        <span className="text-right">买盘挂单额 →</span>
      </div>

      {/* 阶梯主体（滚动容器；offsetTop 相对本容器，供 mid 自动居中） */}
      <div ref={ladderRef} className="relative overflow-y-auto max-h-[480px] pr-0.5">
        {!depth && loading && (
          <div className="flex items-center justify-center py-16">
            <div className="w-5 h-5 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin" />
          </div>
        )}
        {!depth && !loading && error && (
          <p className="text-xs text-jarvis-red py-8 text-center">
            深度接口异常：{error}
          </p>
        )}
        {depth && depth.ok === false && (
          <p className="text-xs text-jarvis-red py-8 text-center">
            深度接口异常：{depth.error ?? "未知错误"}
          </p>
        )}
        {depth?.ok && asksDesc.length === 0 && bids.length === 0 && (
          <p className="text-xs text-jarvis-text-secondary py-8 text-center">
            订单簿为空
          </p>
        )}
        {depth?.ok && (
          <>
            {asksDesc.map((b) => (
              <BucketRow key={`a-${b.price}`} kind="ask" b={b} maxUsd={maxUsd} />
            ))}
            {(asksDesc.length > 0 || bids.length > 0) && (
              <div
                ref={midRowRef}
                className="flex items-center justify-center gap-2 h-[26px] my-0.5 rounded bg-jarvis-blue/10 border-y border-jarvis-blue/30 text-[11px] font-mono"
              >
                <span className="text-jarvis-text font-semibold">
                  {formatPrice(depth.mid)}
                </span>
                <span className="text-jarvis-text-secondary">
                  价差{" "}
                  {depth.spread_pct != null
                    ? `${depth.spread_pct.toFixed(3)}%`
                    : "--"}
                </span>
              </div>
            )}
            {bids.map((b) => (
              <BucketRow key={`b-${b.price}`} kind="bid" b={b} maxUsd={maxUsd} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/* ────────────────────────── ④ 成交流画像三块 ────────────────────────── */

/** (a) 主力判定卡：动作大字 + 非散户占比 + 脉冲警报 + 入场提示 + 四主体结构 */
function VerdictCard({ tape }: { tape: TapeFlowResponse }) {
  const v = tape.verdict;
  const actors = tape.breakdown?.actors;
  return (
    <div className="card p-4 space-y-3">
      <p className="stat-label mb-0 flex items-center gap-1.5 text-xs">
        <Users size={14} />
        主力行为判定
        {v && (
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-border/40 text-jarvis-text-secondary">
            主导·{v.dominant_cn}
          </span>
        )}
      </p>

      {v ? (
        <>
          {/* 动作大字（砸盘红 / 拉盘绿 / 吸筹蓝 / 派发橙 / 中性灰）+ 说明 */}
          <div>
            <p
              className={clsx(
                "text-2xl font-bold leading-none",
                actionColorCls(v.action),
              )}
            >
              {v.action}
            </p>
            {v.note && (
              <p className="text-[11px] text-jarvis-text-secondary mt-1.5 leading-snug">
                {v.note}
              </p>
            )}
          </div>

          {/* 非散户参与占比进度条 */}
          <div>
            <div className="flex items-center justify-between text-[10px] text-jarvis-text-secondary mb-1">
              <span>非散户参与占比</span>
              <span className="font-mono text-jarvis-text">
                {v.non_retail_share_pct.toFixed(1)}%
              </span>
            </div>
            <div className="h-1.5 bg-jarvis-bg rounded-full overflow-hidden">
              <div
                className="h-full bg-jarvis-purple rounded-full transition-all"
                style={{ width: `${Math.min(100, v.non_retail_share_pct)}%` }}
              />
            </div>
          </div>

          {/* 突发脉冲警报（黄色警报框） */}
          {v.burst && (
            <div className="flex items-start gap-1.5 rounded-lg bg-jarvis-yellow/10 border border-jarvis-yellow/40 px-2.5 py-2">
              <AlertTriangle
                size={13}
                className="text-jarvis-yellow flex-shrink-0 mt-0.5"
              />
              <p className="text-[11px] text-jarvis-yellow leading-snug">
                <span className="font-medium">
                  {v.burst.side === "buy" ? "买向脉冲" : "卖向脉冲"}{" "}
                  {fmtUsd(v.burst.usd)}
                </span>
                {" · "}
                {v.burst.note}
              </p>
            </div>
          )}

          {/* 入场时机提示（绿色提示框） */}
          {v.entry_hint && (
            <div className="flex items-start gap-1.5 rounded-lg bg-jarvis-green/10 border border-jarvis-green/40 px-2.5 py-2">
              <Target
                size={13}
                className="text-jarvis-green flex-shrink-0 mt-0.5"
              />
              <p className="text-[11px] text-jarvis-green leading-snug">
                {v.entry_hint}
              </p>
            </div>
          )}
        </>
      ) : (
        <p className="text-xs text-jarvis-text-secondary py-2">
          窗口内数据不足，暂无法判定
        </p>
      )}

      {/* 四主体占比横向堆叠条 + 图例（各自净额正绿负红） */}
      {actors && (
        <div>
          <div className="h-2.5 rounded-full overflow-hidden flex bg-jarvis-bg">
            {ACTOR_ORDER.map((a) => {
              const s = actors[a];
              if (!s || s.pct <= 0) return null;
              return (
                <div
                  key={a}
                  className={ACTOR_STYLE[a].bar}
                  style={{ width: `${s.pct}%` }}
                  title={`${s.actor_cn} ${s.pct.toFixed(1)}% · 净额 ${fmtSignedUsd(s.net_usd)}`}
                />
              );
            })}
          </div>
          <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
            {ACTOR_ORDER.map((a) => {
              const s = actors[a];
              if (!s) return null;
              return (
                <div key={a} className="flex items-center gap-1.5 text-[10px]">
                  <span
                    className={clsx(
                      "w-2 h-2 rounded-sm flex-shrink-0",
                      ACTOR_STYLE[a].bar,
                    )}
                  />
                  <span className="text-jarvis-text-secondary">{s.actor_cn}</span>
                  <span className="font-mono text-jarvis-text">
                    {s.pct.toFixed(1)}%
                  </span>
                  <span
                    className={clsx(
                      "font-mono ml-auto",
                      s.net_usd >= 0 ? "text-jarvis-green" : "text-jarvis-red",
                    )}
                  >
                    {fmtSignedUsd(s.net_usd)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/** (b) 数量指纹聚合表：同一数量反复成交 = 疑似同一主体拆单（唯一标识下了几笔/每笔多少） */
function FingerprintTable({ fps }: { fps: TapeFingerprint[] }) {
  return (
    <div className="card p-4">
      <p className="stat-label mb-2 flex items-center gap-1.5 text-xs">
        <Fingerprint size={14} />
        指纹聚合（拆单识别）
        <span className="text-[10px] text-jarvis-text-secondary/70 font-normal">
          标识 = 单笔数量
        </span>
      </p>
      {fps.length === 0 ? (
        <p className="text-xs text-jarvis-text-secondary py-6 text-center">
          窗口内暂无 ≥2 笔的同数量成交组
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[10px] font-mono">
            <thead>
              <tr className="text-jarvis-text-secondary text-left">
                <th className="font-normal pb-1.5 pr-2">标识</th>
                <th className="font-normal pb-1.5 pr-2">分类</th>
                <th className="font-normal pb-1.5 pr-2 text-right">
                  笔数(买/卖)
                </th>
                <th className="font-normal pb-1.5 pr-2 text-right">单笔均额</th>
                <th className="font-normal pb-1.5 pr-2 text-right">总额</th>
                <th className="font-normal pb-1.5 pr-2 text-right">净额</th>
                <th className="font-normal pb-1.5 text-right">活跃时段</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-jarvis-border/40">
              {fps.map((g) => (
                <tr key={g.fp} className="text-jarvis-text">
                  <td className="py-1.5 pr-2">{g.fp}</td>
                  <td className="py-1.5 pr-2">
                    <ClsBadge cls={g.cls} clsCn={g.cls_cn} />
                  </td>
                  <td
                    className="py-1.5 pr-2 text-right whitespace-nowrap"
                    title={`共 ${g.n} 笔：买 ${g.buy_n} / 卖 ${g.sell_n}`}
                  >
                    {g.n}（<span className="text-jarvis-green">{g.buy_n}</span>/
                    <span className="text-jarvis-red">{g.sell_n}</span>）
                  </td>
                  <td className="py-1.5 pr-2 text-right">{fmtUsd(g.avg_usd)}</td>
                  <td className="py-1.5 pr-2 text-right">
                    {fmtUsd(g.total_usd)}
                  </td>
                  <td
                    className={clsx(
                      "py-1.5 pr-2 text-right",
                      g.net_usd >= 0 ? "text-jarvis-green" : "text-jarvis-red",
                    )}
                  >
                    {fmtSignedUsd(g.net_usd)}
                  </td>
                  <td className="py-1.5 text-right text-jarvis-text-secondary whitespace-nowrap">
                    {timeHm(g.first_ts)}~{timeHm(g.last_ts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** (c) 实时成交列表：最新在上，买绿卖红，机构/做市商彩底徽章，同指纹 ≥3 笔带角标 */
function RecentTradeList({ trades }: { trades: TapeTrade[] }) {
  return (
    <div className="card p-4 flex flex-col">
      <p className="stat-label mb-2 flex items-center gap-1.5 text-xs">
        <Activity size={14} />
        实时成交流
      </p>
      {trades.length === 0 ? (
        <p className="text-xs text-jarvis-text-secondary py-6 text-center">
          窗口内暂无成交记录
        </p>
      ) : (
        <div className="overflow-y-auto max-h-[420px] pr-1 divide-y divide-jarvis-border/30">
          {trades.map((t, i) => (
            <div
              key={`${t.ts_ms}-${i}`}
              className="flex items-center gap-2 py-1 text-[10px] font-mono"
            >
              <span className="text-jarvis-text-secondary flex-shrink-0 w-[52px]">
                {timeHms(t.ts_ms)}
              </span>
              <span
                className={clsx(
                  "flex-shrink-0 w-[14px] text-center font-semibold",
                  t.is_buy ? "text-jarvis-green" : "text-jarvis-red",
                )}
              >
                {t.is_buy ? "买" : "卖"}
              </span>
              <span
                className={clsx(
                  "flex-1 text-right",
                  t.is_buy ? "text-jarvis-green" : "text-jarvis-red",
                )}
              >
                {formatPrice(t.price)}
              </span>
              <span className="flex-1 text-right text-jarvis-text-secondary">
                {fmtQty(t.qty)}
              </span>
              <span
                className={clsx(
                  "flex-1 text-right",
                  t.is_buy ? "text-jarvis-green" : "text-jarvis-red",
                )}
              >
                {fmtUsd(t.usd)}
              </span>
              <span className="flex-shrink-0 flex items-center gap-1">
                <ClsBadge cls={t.cls} clsCn={t.cls_cn} />
                {t.fp_n >= 3 && (
                  <span
                    className="px-1 py-px rounded bg-jarvis-yellow/15 text-jarvis-yellow text-[9px] whitespace-nowrap leading-none"
                    title={`同指纹（单笔数量 ${t.fp}）窗口内已累计 ${t.fp_n} 笔`}
                  >
                    ×{t.fp_n}笔
                  </span>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ────────────────────────── ④ 成交流足迹图（canvas 自绘） ────────────────────────── */

/** ts 归一为 unix 秒：兼容后端可能返回毫秒（>1e12 视为 ms） */
function toSec(ts: number): number {
  return ts > 1e12 ? Math.floor(ts / 1000) : ts;
}

/** 足迹单元格文本用紧凑数字（无 $ 前缀，节省格宽） */
function fmtCompact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

// 足迹图绘制常量（CSS 像素）
const FP_CELL_W = 96; // 每柱列宽
const FP_CELL_H = 16; // 每价格档行高
const FP_HEADER_H = 20; // 顶部时间行高
const FP_AXIS_W = 76; // 右侧价格轴 / 统计标签列宽
const FP_STAT_N = 5; // 底部统计行数（Total/买/卖/Δ/CVD）
const FP_FOOTER_H = FP_STAT_N * FP_CELL_H + 6;
const FP_MAX_ROWS = 600; // 全局价格档数安全上限（异常数据防护）

/** 足迹图布局：全局价格轴（各柱档位跨柱对齐，同一价位同一行） */
interface FpLayout {
  step: number;
  maxPrice: number;
  nRows: number;
  /** 每柱：全局行号 → 档数据 */
  slots: Map<number, FootprintRow>[];
  width: number;
  height: number;
}

/**
 * 足迹图（Footprint，canvas 自绘——档数×柱数多，DOM 渲染会卡）：
 *   每柱一列，列内按全局价格档从高到低排格；每格文本「卖 × 买」（$K 缩写），
 *   背景浓度 = max(买,卖)/该柱最大档，买强蓝系/卖强红系；失衡档黄描边黄字；
 *   收盘价档白描边；列顶 HH:mm；底部对齐 Total/买/卖/Δ/CVD 五行统计；
 *   悬停格 tooltip（价/买/卖/失衡倍数）；横向滚动查旧柱（默认贴最新）。
 */
function FootprintPane({
  resp,
  loading,
  error,
}: {
  resp: TapeFootprintResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // 贴右缘跟随最新柱：用户左滚查旧柱后轮询刷新不再拉回
  const stickRightRef = useRef(true);
  const [tip, setTip] = useState<{ x: number; y: number; lines: string[] } | null>(
    null,
  );

  const bars = useMemo(() => resp?.bars ?? [], [resp]);

  // 全局价格轴布局：桶宽优先接口 bucket，缺失时从相邻档价差推导
  const layout = useMemo<FpLayout | null>(() => {
    if (bars.length === 0) return null;
    let step = resp?.bucket ?? 0;
    if (!(step > 0)) {
      for (const b of bars) {
        for (let i = 1; i < b.rows.length; i++) {
          const d = Math.abs(b.rows[i - 1].price - b.rows[i].price);
          if (d > 0) step = step > 0 ? Math.min(step, d) : d;
        }
        if (step > 0) break;
      }
    }
    if (!(step > 0)) step = 1;
    let maxPrice = -Infinity;
    let minPrice = Infinity;
    for (const b of bars) {
      for (const r of b.rows) {
        if (r.price > maxPrice) maxPrice = r.price;
        if (r.price < minPrice) minPrice = r.price;
      }
      // 降级柱（rows=[] 只有量价汇总，档位明细未覆盖时段）用 OHLC 撑起
      // 价格轴：否则其价格区间落在明细范围外时简化柱画不进画面，甚至
      // 全部柱降级时整个面板误判为「暂无数据」
      if (b.rows.length === 0 && b.high > 0 && b.low > 0) {
        if (b.high > maxPrice) maxPrice = b.high;
        if (b.low < minPrice) minPrice = b.low;
      }
    }
    if (!Number.isFinite(maxPrice) || !Number.isFinite(minPrice)) return null;
    // 超上限的低价档直接裁掉（防异常数据把 canvas 撑爆）
    const nRows = Math.min(
      FP_MAX_ROWS,
      Math.round((maxPrice - minPrice) / step) + 1,
    );
    const slots = bars.map((b) => {
      const m = new Map<number, FootprintRow>();
      for (const r of b.rows) {
        const idx = Math.round((maxPrice - r.price) / step);
        if (idx >= 0 && idx < nRows) m.set(idx, r);
      }
      return m;
    });
    return {
      step,
      maxPrice,
      nRows,
      slots,
      width: bars.length * FP_CELL_W + FP_AXIS_W,
      height: FP_HEADER_H + nRows * FP_CELL_H + FP_FOOTER_H,
    };
  }, [bars, resp?.bucket]);

  // 切币/切周期后恢复贴右缘（跟随最新柱）
  const dataKey = `${resp?.symbol ?? ""}|${resp?.interval ?? ""}`;
  useEffect(() => {
    stickRightRef.current = true;
  }, [dataKey]);

  // 主绘制：数据/布局变化时整幅重画（30 柱 × ≤600 档，canvas 开销可忽略）
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !layout) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(layout.width * dpr);
    canvas.height = Math.round(layout.height * dpr);
    canvas.style.width = `${layout.width}px`;
    canvas.style.height = `${layout.height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, layout.width, layout.height);
    ctx.font = "9px 'SF Mono', Menlo, monospace";
    ctx.textBaseline = "middle";

    const { nRows, slots, step, maxPrice } = layout;
    const gridY = FP_HEADER_H + nRows * FP_CELL_H;

    bars.forEach((b, ci) => {
      const x0 = ci * FP_CELL_W;
      // 浓度归一化分母：该柱最大单档额
      let barMax = 0;
      for (const r of b.rows) barMax = Math.max(barMax, r.buy, r.sell);

      // 列顶时间（最新柱白色高亮）
      ctx.textAlign = "center";
      ctx.fillStyle = ci === bars.length - 1 ? "#e6edf3" : "#8b949e";
      ctx.fillText(timeHm(toSec(b.ts) * 1000), x0 + FP_CELL_W / 2, FP_HEADER_H / 2);

      // 简化柱：rows=[] 但有量价汇总（档位明细未覆盖的历史时段）。原先
      // 整列只剩收盘白框，观感像坏了——改画 high~low 范围的半透明双色带，
      // 左卖红右买蓝按买卖额占比分宽（与单元格「卖 × 买」左右语义一致），
      // 虚线描边 + 中央「简化」字样与正常柱区分；汇总数字看底部统计行。
      const summaryOnly =
        b.rows.length === 0 && (b.total > 0 || b.buy > 0 || b.sell > 0);
      if (summaryOnly && b.high > 0 && b.low > 0) {
        const hiIdx = Math.max(0, Math.min(nRows - 1, Math.round((maxPrice - b.high) / step)));
        const loIdx = Math.max(0, Math.min(nRows - 1, Math.round((maxPrice - b.low) / step)));
        const yTop = FP_HEADER_H + Math.min(hiIdx, loIdx) * FP_CELL_H;
        const hPx = (Math.abs(loIdx - hiIdx) + 1) * FP_CELL_H;
        const sum = b.buy + b.sell;
        const sellW = Math.round((FP_CELL_W - 2) * (sum > 0 ? b.sell / sum : 0.5));
        ctx.fillStyle = "rgba(248, 81, 73, 0.14)";
        ctx.fillRect(x0 + 1, yTop + 0.5, sellW, hPx - 1);
        ctx.fillStyle = "rgba(56, 132, 255, 0.14)";
        ctx.fillRect(x0 + 1 + sellW, yTop + 0.5, FP_CELL_W - 2 - sellW, hPx - 1);
        ctx.strokeStyle = "rgba(139, 148, 158, 0.45)";
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(x0 + 1.5, yTop + 1, FP_CELL_W - 3, hPx - 2);
        ctx.setLineDash([]);
        ctx.textAlign = "center";
        ctx.fillStyle = "#8b949e";
        ctx.fillText("简化 · 无档位明细", x0 + FP_CELL_W / 2, yTop + hPx / 2);
      }

      // 单元格：背景浓度 + 「卖 × 买」双色文本 + 失衡黄描边
      slots[ci].forEach((r, ri) => {
        const y0 = FP_HEADER_H + ri * FP_CELL_H;
        const v = barMax > 0 ? Math.max(r.buy, r.sell) / barMax : 0;
        const alpha = 0.1 + 0.5 * v;
        ctx.fillStyle =
          r.buy >= r.sell
            ? `rgba(56, 132, 255, ${alpha})` // 买强：蓝系
            : `rgba(248, 81, 73, ${alpha})`; // 卖强：红系
        ctx.fillRect(x0 + 1, y0 + 0.5, FP_CELL_W - 2, FP_CELL_H - 1);
        if (r.flag) {
          ctx.strokeStyle = "#d29922";
          ctx.lineWidth = 1;
          ctx.strokeRect(x0 + 1.5, y0 + 1, FP_CELL_W - 3, FP_CELL_H - 2);
        }
        const cx = x0 + FP_CELL_W / 2;
        const cy = y0 + FP_CELL_H / 2;
        ctx.textAlign = "center";
        ctx.fillStyle = "#8b949e";
        ctx.fillText("×", cx, cy);
        ctx.textAlign = "right";
        ctx.fillStyle = r.flag === "sell_imb" ? "#e3b341" : "#f97583";
        ctx.fillText(fmtCompact(r.sell), cx - 5, cy);
        ctx.textAlign = "left";
        ctx.fillStyle = r.flag === "buy_imb" ? "#e3b341" : "#56d364";
        ctx.fillText(fmtCompact(r.buy), cx + 5, cy);
      });

      // 收盘价档白描边（标记该柱收在哪个价位）
      const closeIdx = Math.round((maxPrice - b.close) / step);
      if (closeIdx >= 0 && closeIdx < nRows) {
        const y0 = FP_HEADER_H + closeIdx * FP_CELL_H;
        ctx.strokeStyle = "#e6edf3";
        ctx.lineWidth = 1;
        ctx.strokeRect(x0 + 0.5, y0 + 0.5, FP_CELL_W - 1, FP_CELL_H - 1);
      }

      // 列分隔线
      ctx.strokeStyle = "rgba(48, 54, 61, 0.6)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x0 + FP_CELL_W + 0.5, 0);
      ctx.lineTo(x0 + FP_CELL_W + 0.5, layout.height);
      ctx.stroke();

      // 底部统计五行（与列对齐）：Total/买/卖/Δ/CVD
      const stats: { v: string; c: string }[] = [
        { v: fmtCompact(b.total), c: "#8b949e" },
        { v: fmtCompact(b.buy), c: "#56d364" },
        { v: fmtCompact(b.sell), c: "#f97583" },
        {
          v: `${b.delta >= 0 ? "+" : ""}${fmtCompact(b.delta)}`,
          c: b.delta >= 0 ? "#56d364" : "#f97583",
        },
        { v: fmtCompact(b.cvd), c: "#e6edf3" },
      ];
      ctx.textAlign = "center";
      for (let si = 0; si < stats.length; si++) {
        ctx.fillStyle = stats[si].c;
        ctx.fillText(
          stats[si].v,
          x0 + FP_CELL_W / 2,
          gridY + 4 + si * FP_CELL_H + FP_CELL_H / 2,
        );
      }
    });

    // 右侧轴列：价格标签 + 统计行标签
    const axisX = bars.length * FP_CELL_W;
    ctx.textAlign = "left";
    for (let ri = 0; ri < nRows; ri++) {
      ctx.fillStyle = "#8b949e";
      ctx.fillText(
        formatPrice(maxPrice - ri * step),
        axisX + 6,
        FP_HEADER_H + ri * FP_CELL_H + FP_CELL_H / 2,
      );
    }
    const statLabels: { t: string; c: string }[] = [
      { t: "Total", c: "#8b949e" },
      { t: "买", c: "#56d364" },
      { t: "卖", c: "#f97583" },
      { t: "Δ", c: "#8b949e" },
      { t: "CVD", c: "#e6edf3" },
    ];
    for (let si = 0; si < statLabels.length; si++) {
      ctx.fillStyle = statLabels[si].c;
      ctx.fillText(
        statLabels[si].t,
        axisX + 6,
        gridY + 4 + si * FP_CELL_H + FP_CELL_H / 2,
      );
    }
    // header 下缘 / footer 上缘横向分隔线
    ctx.strokeStyle = "#30363d";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, FP_HEADER_H - 0.5);
    ctx.lineTo(layout.width, FP_HEADER_H - 0.5);
    ctx.moveTo(0, gridY + 1.5);
    ctx.lineTo(layout.width, gridY + 1.5);
    ctx.stroke();
  }, [bars, layout]);

  // 数据更新后：仍贴右缘时自动滚到最新柱
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !layout) return;
    if (stickRightRef.current) el.scrollLeft = el.scrollWidth;
  }, [layout]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickRightRef.current =
      el.scrollWidth - el.clientWidth - el.scrollLeft < 48;
  };

  // 悬停命中：像素 → 列号/行号 → 档数据
  const onMouseMove = (e: ReactMouseEvent<HTMLCanvasElement>) => {
    if (!layout) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const ci = Math.floor(x / FP_CELL_W);
    const ri = Math.floor((y - FP_HEADER_H) / FP_CELL_H);
    if (ci < 0 || ci >= bars.length || ri < 0 || ri >= layout.nRows) {
      setTip(null);
      return;
    }
    const r = layout.slots[ci]?.get(ri);
    if (!r) {
      // 简化柱（rows=[] 仅量价汇总）：在其 high~low 色带范围内悬停给出
      // 降级原因与买卖汇总，消除「图坏了」的误解
      const b = bars[ci];
      if (b && b.rows.length === 0 && (b.total > 0 || b.buy > 0 || b.sell > 0) && b.high > 0) {
        const hiIdx = Math.max(0, Math.min(layout.nRows - 1, Math.round((layout.maxPrice - b.high) / layout.step)));
        const loIdx = Math.max(0, Math.min(layout.nRows - 1, Math.round((layout.maxPrice - b.low) / layout.step)));
        if (ri >= Math.min(hiIdx, loIdx) && ri <= Math.max(hiIdx, loIdx)) {
          setTip({
            x,
            y,
            lines: [
              "简化柱：该时段仅有量价汇总",
              `买 ${fmtUsd(b.buy)} · 卖 ${fmtUsd(b.sell)} · Δ ${b.delta >= 0 ? "+" : ""}${fmtCompact(b.delta)}`,
              "价格档明细自 8/2 晚间开始记录，此前时段按 K 线回补",
            ],
          });
          return;
        }
      }
      setTip(null);
      return;
    }
    const hi = Math.max(r.buy, r.sell);
    const lo = Math.max(Math.min(r.buy, r.sell), 1);
    const flagCn =
      r.flag === "buy_imb"
        ? "（买方失衡档）"
        : r.flag === "sell_imb"
          ? "（卖方失衡档）"
          : "";
    setTip({
      x,
      y,
      lines: [
        `价 ${formatPrice(r.price)}`,
        `买 ${fmtUsd(r.buy)} · 卖 ${fmtUsd(r.sell)}`,
        `失衡 ${(hi / lo).toFixed(1)}×${flagCn}`,
      ],
    });
  };

  // 占位态：接口不可用（404/抛错）/ ok:false / WS 未积累 / 空柱
  const placeholder = !resp
    ? loading
      ? "loading"
      : "后端未升级或 WS 数据积累中（GET /api/tape/footprint 不可用）"
    : resp.ok === false
      ? `接口异常：${resp.error ?? "未知错误"}（后端未升级或 WS 数据积累中）`
      : resp.active === false
        ? "后端未升级或 WS 数据积累中（需开启 ws_enabled）"
        : bars.length === 0
          ? "暂无足迹数据（WS 数据积累中，稍候自动刷新）"
          : null;

  return (
    <div>
      <div className="flex items-center justify-between gap-2 px-1 pb-1 flex-wrap">
        <span className="text-[10px] text-jarvis-text-secondary font-mono truncate">
          每格「卖 × 买」 · 浓度 = 该档额 / 柱内最大档 · 黄框 = 失衡档 · 白框 = 收盘档 ·
          虚线柱 = 仅量价汇总（该时段无档位明细，悬停看说明）
        </span>
        {resp?.source && (
          <span
            className="text-[9px] px-1.5 py-0.5 rounded border bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border whitespace-nowrap"
            title="数据来源"
          >
            {resp.source}
          </span>
        )}
      </div>
      {placeholder ? (
        <div
          className="flex flex-col items-center justify-center text-jarvis-text-secondary"
          style={{ height: 320 }}
          title={error ?? undefined}
        >
          {placeholder === "loading" ? (
            <div className="w-5 h-5 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin" />
          ) : (
            <>
              <LayoutGrid size={20} className="mb-2 text-jarvis-text-secondary/60" />
              <p className="text-xs">{placeholder}</p>
            </>
          )}
        </div>
      ) : (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="overflow-auto max-h-[560px]"
        >
          <div className="relative inline-block">
            <canvas
              ref={canvasRef}
              onMouseMove={onMouseMove}
              onMouseLeave={() => setTip(null)}
            />
            {tip && (
              <div
                className="pointer-events-none absolute z-10 px-2 py-1 rounded border border-jarvis-border bg-jarvis-card/95 text-[10px] text-jarvis-text whitespace-nowrap shadow-lg font-mono"
                style={{
                  left: Math.min(tip.x + 12, (layout?.width ?? 0) - 190),
                  top: tip.y + 12,
                }}
              >
                {tip.lines.map((l) => (
                  <p key={l}>{l}</p>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 主体多空统计卡的四行元信息（名称沿用成交流画像口径） */
const FP_ACTOR_META: { key: TapeActor; label: string }[] = [
  { key: "retail", label: "散户" },
  { key: "mid", label: "中户" },
  { key: "inst", label: "机构大户" },
  { key: "maker", label: "做市商" },
];

/**
 * 主体多空统计卡（谁在做多/做空）：
 *   overall 大字结论 + 四主体「做多占比」水平条（>55% 偏多绿 / <45% 偏空红 / 其间均衡灰）。
 *   口径为 taker 主动方向启发式（主动买≈做多意愿），非真实持仓——脚注声明。
 */
function ActorBiasCard({
  actors,
  disclaimer,
}: {
  actors: FootprintActors;
  disclaimer?: string;
}) {
  const o = actors.overall;
  const overallCls =
    o.delta > 0
      ? "text-jarvis-green"
      : o.delta < 0
        ? "text-jarvis-red"
        : "text-jarvis-text-secondary";
  return (
    <div className="card p-4">
      <p className="stat-label mb-2 flex items-center gap-1.5 text-xs">
        <Users size={14} />
        主体多空结构（谁在做多 / 做空）
      </p>
      <p className={clsx("text-lg font-semibold leading-snug", overallCls)}>
        {o.verdict_cn}
      </p>
      <p className="text-[10px] text-jarvis-text-secondary font-mono mt-0.5 mb-3">
        主动买 {fmtUsd(o.buy)} · 主动卖 {fmtUsd(o.sell)} · Δ {fmtSignedUsd(o.delta)}
      </p>
      <div className="space-y-2">
        {FP_ACTOR_META.map(({ key, label }) => {
          const s = actors[key];
          if (!s) return null;
          const pct = Math.max(0, Math.min(100, s.long_pct));
          const bias = pct > 55 ? "long" : pct < 45 ? "short" : "flat";
          const biasCls =
            bias === "long"
              ? "text-jarvis-green"
              : bias === "short"
                ? "text-jarvis-red"
                : "text-jarvis-text-secondary";
          return (
            <div
              key={key}
              title={`买 ${fmtUsd(s.buy)} / 卖 ${fmtUsd(s.sell)} / 净 ${fmtSignedUsd(s.net)}`}
            >
              <div className="flex items-center justify-between text-[10px] mb-0.5">
                <span className="flex items-center gap-1.5">
                  <span
                    className={clsx("w-2 h-2 rounded-sm", ACTOR_STYLE[key].bar)}
                  />
                  <span className="text-jarvis-text">{label}</span>
                </span>
                <span className={clsx("font-mono", biasCls)}>
                  做多 {pct.toFixed(0)}% · {s.verdict_cn}
                </span>
              </div>
              {/* 做多占比条：绿色填充 = 多方份额，余下红底 = 空方份额，中线 50% 分界 */}
              <div className="relative h-1.5 bg-jarvis-red/25 rounded-full overflow-hidden">
                <div
                  className={clsx(
                    "h-full rounded-full",
                    bias === "long"
                      ? "bg-jarvis-green"
                      : bias === "short"
                        ? "bg-jarvis-red"
                        : "bg-gray-500",
                  )}
                  style={{ width: `${pct}%` }}
                />
                <div className="absolute inset-y-0 left-1/2 w-px bg-jarvis-text/40" />
              </div>
            </div>
          );
        })}
      </div>
      <p className="text-[9px] text-jarvis-text-secondary/60 mt-3 leading-snug">
        {disclaimer ??
          "口径：按 taker 主动方向启发式归类（主动买≈做多意愿、主动卖≈做空/出货意愿），非真实持仓数据，仅供情绪参考。"}
      </p>
    </div>
  );
}

/* ────────────────────────── ② MACD 副图（主图下方可折叠） ────────────────────────── */

/** MACD 线值显示：随价格量级自适应小数位 */
function fmtMacdVal(v: number): string {
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 1 : a >= 1 ? 2 : 4);
}

/**
 * MACD(12,26,9) 副图（lightweight-charts 独立小图，不动 KlineChart.tsx）：
 *   柱 = DIF-DEA（多头绿 / 空头红，与主图量柱同色系）；蓝线 = DIF，橙线 = DEA。
 *   数据复用主图同一份 K 线收盘价本地计算，随主周期切换自动重算，
 *   不发起任何新接口请求。
 */
function MacdPane({
  candles,
  height = 150,
}: {
  candles: CandlestickData<Time>[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const difRef = useRef<ISeriesApi<"Line"> | null>(null);
  const deaRef = useRef<ISeriesApi<"Line"> | null>(null);
  const histRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  const macd = useMemo(
    () => calcMACD(candles.map((c) => c.close)),
    [candles],
  );

  // 初始化图实例（一次；height 变更时重建），暗色样式与页内其它小图一致
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b949e",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "rgba(48, 54, 61, 0.4)" },
        horzLines: { color: "rgba(48, 54, 61, 0.4)" },
      },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: {
        borderColor: "#30363d",
        timeVisible: true,
        secondsVisible: false,
      },
    });
    // 柱在底层，双线叠在柱上方
    const hist = chart.addHistogramSeries({
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const dif = chart.addLineSeries({
      color: "#58a6ff",
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const dea = chart.addLineSeries({
      color: "#d29922",
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chartRef.current = chart;
    histRef.current = hist;
    difRef.current = dif;
    deaRef.current = dea;

    const ro = new ResizeObserver(() =>
      chart.applyOptions({ width: el.clientWidth }),
    );
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      histRef.current = null;
      difRef.current = null;
      deaRef.current = null;
    };
  }, [height]);

  // 数据更新：预热期 null 直接跳过（线从首个有效值开始画）
  useEffect(() => {
    const chart = chartRef.current;
    const dif = difRef.current;
    const dea = deaRef.current;
    const hist = histRef.current;
    if (!chart || !dif || !dea || !hist) return;

    const difData: { time: Time; value: number }[] = [];
    const deaData: { time: Time; value: number }[] = [];
    const histData: { time: Time; value: number; color: string }[] = [];
    for (let i = 0; i < candles.length; i++) {
      const t = candles[i].time;
      const d = macd.dif[i];
      const s = macd.dea[i];
      const h = macd.hist[i];
      if (d !== null) difData.push({ time: t, value: d });
      if (s !== null) deaData.push({ time: t, value: s });
      if (h !== null)
        histData.push({
          time: t,
          value: h,
          color: h >= 0 ? "rgba(63, 185, 80, 0.65)" : "rgba(248, 81, 73, 0.65)",
        });
    }
    hist.setData(histData);
    dif.setData(difData);
    dea.setData(deaData);
    chart.timeScale().fitContent();
  }, [candles, macd]);

  // 图例：最新一根的 DIF / DEA / MACD 柱值
  const lastIdx = (() => {
    for (let i = macd.hist.length - 1; i >= 0; i--) {
      if (macd.hist[i] !== null) return i;
    }
    return -1;
  })();

  return (
    <div className="px-1 pt-1.5">
      <div className="flex items-center gap-3 px-2 pb-1 text-[10px] font-mono flex-wrap">
        <span className="text-jarvis-text-secondary">MACD(12,26,9)</span>
        {lastIdx >= 0 && (
          <>
            <span style={{ color: "#58a6ff" }}>
              DIF {fmtMacdVal(macd.dif[lastIdx]!)}
            </span>
            <span style={{ color: "#d29922" }}>
              DEA {fmtMacdVal(macd.dea[lastIdx]!)}
            </span>
            <span
              className={
                macd.hist[lastIdx]! >= 0 ? "text-jarvis-green" : "text-jarvis-red"
              }
            >
              MACD {fmtMacdVal(macd.hist[lastIdx]!)}
            </span>
          </>
        )}
      </div>
      <div ref={containerRef} />
    </div>
  );
}

/* ────────────────────────── 页面主组件 ────────────────────────── */

export default function DepthView() {
  const { symbol } = useSymbol();
  const [tf, setTf] = useState<Timeframe>("1m");

  // ② K 线取数（仿 Chart.tsx：1m 10s 轮询，其余 60s）
  const {
    data: rawKline,
    loading: klineLoading,
    error: klineError,
  } = usePolling(
    () => api.kline(symbol, tf, 200),
    tf === "1m" ? 10_000 : 60_000,
    [tf, symbol],
  );

  // 后端 rows → lightweight-charts 蜡烛 / 量柱
  const { candles, volumes } = useMemo(() => {
    const rows = (rawKline as Record<string, unknown>)?.rows;
    if (!rawKline || !Array.isArray(rows)) {
      return {
        candles: [] as CandlestickData<Time>[],
        volumes: [] as HistogramData<Time>[],
      };
    }
    const c: CandlestickData<Time>[] = [];
    const v: HistogramData<Time>[] = [];
    for (const k of rows as Record<string, number>[]) {
      const time = (k.ts / 1000) as Time;
      c.push({ time, open: k.o, high: k.h, low: k.l, close: k.c });
      v.push({
        time,
        value: k.v ?? 0,
        color:
          k.c >= k.o ? "rgba(63, 185, 80, 0.3)" : "rgba(248, 81, 73, 0.3)",
      });
    }
    return { candles: c, volumes: v };
  }, [rawKline]);

  // ③ 盘口深度（order-flow phase-1：WS 增量本地订单簿，1s 轮询与后端 1s 缓存
  //    同频；本地簿未就绪时后端自动回退 REST 快照，载荷形状不变）
  const depthPoll = usePolling(
    () => api.orderbookLive(symbol, 24),
    1_000,
    [symbol],
  );

  // ④ 成交流画像（3s 轮询）：窗口跟随主周期（TF_WINDOW_MIN 映射，强制一致），
  // 参数进 deps，切周期即刻重取并丢弃旧窗口的慢响应
  const windowMin = TF_WINDOW_MIN[tf];
  const tapePoll = usePolling(
    () => api.tapeFlow(symbol, windowMin),
    3_000,
    [symbol, windowMin],
  );
  const tape = tapePoll.data;

  // 实时成交列表：统一按时间倒序（最新在上）
  const recentDesc = useMemo(
    () => [...(tape?.recent ?? [])].sort((a, b) => b.ts_ms - a.ts_ms),
    [tape],
  );

  // WS 数据流可用性：未就绪或窗口无成交时整个成交流区显示占位态
  const wsActive =
    tape?.ok === true && tape.ws_ready !== false && tape.active !== false;

  // ④ 成交流区视图（足迹图|列表），localStorage 持久
  const [tapeView, setTapeView] = useState<TapeView>(() =>
    readStored(TAPE_VIEW_KEY, ["footprint", "list"] as const, "footprint"),
  );

  // ② MACD 副图开关（12/26/9，主图下方可折叠），localStorage 持久
  const [showMacd, setShowMacd] = useState(
    () => readStored(MACD_KEY, ["on", "off"] as const, "off") === "on",
  );

  // ② 诱多诱空信号开关：与 K 线页共用 TRAP_TOGGLE_KEY（"1"/"0"），任一页
  // 开关即两页状态一致
  const [trapOn, setTrapOn] = useState(
    () => readStored(TRAP_TOGGLE_KEY, ["1", "0"] as const, "0") === "1",
  );

  useEffect(() => {
    try {
      localStorage.setItem(TAPE_VIEW_KEY, tapeView);
    } catch {
      // 忽略写入失败
    }
  }, [tapeView]);
  useEffect(() => {
    try {
      localStorage.setItem(MACD_KEY, showMacd ? "on" : "off");
    } catch {
      // 忽略写入失败
    }
  }, [showMacd]);
  useEffect(() => {
    try {
      localStorage.setItem(TRAP_TOGGLE_KEY, trapOn ? "1" : "0");
    } catch {
      // 忽略写入失败
    }
  }, [trapOn]);

  // ── 诱多/诱空陷阱信号（复用 K 线页 B2 全链路：识别引擎 API 优先，未就绪
  // 回退本地假突破规则识别；interval 跟随主周期 tf，切周期联动重取）──
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
        // 回声校验：慢返回的旧币种/旧周期响应不得写入当前图
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

  // 数据合成：引擎响应优先；未就绪时本地规则识别（随 K 线轮询自动重算）
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

  // 信号 → 主图三角警示标记（窗口裁剪 + 锚定信号 bar 高低点）
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

  // 点击警示牌 → 原因卡片（浮在主图容器右上角）；切币种/周期/关开关时收起
  const [selectedTrap, setSelectedTrap] = useState<TrapMark | null>(null);
  useEffect(() => {
    setSelectedTrap(null);
  }, [symbol, tf, trapOn]);

  // 足迹图周期强制跟随主周期（1h 回退 30m，无独立入口）+ 取数：
  // 仅足迹图视图激活时请求；1m 档 3s 轮询，其它 10s
  const fpTf: FootprintInterval = fpFollow(tf);
  const fpPoll = usePolling(
    () =>
      tapeView === "footprint"
        ? api.tapeFootprint(symbol, fpTf, 30, 40)
        : Promise.resolve(null),
    fpTf === "1m" ? 3_000 : 10_000,
    [tapeView, symbol, fpTf],
  );
  // 回声校验：快速切币/切周期时，旧币种/旧周期的慢响应直接丢弃
  const footprint = useMemo(() => {
    const d = fpPoll.data;
    if (!d) return null;
    if (isStaleEcho(symbol, d.symbol)) return null;
    if (d.interval && d.interval !== fpTf) return null;
    return d;
  }, [fpPoll.data, symbol, fpTf]);

  return (
    <div className="space-y-4">
      {/* ① 顶部：标题 + 当前币种（切币走 Header 全局切换器）+ 周期切换 */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="page-title mb-0 flex items-center gap-2">
          <BookOpenCheck size={22} />
          盘口透视
          <span className="text-sm text-jarvis-text-secondary font-normal">
            {symbol.replace("USDT", "/USDT")}
          </span>
        </h1>
        <div className="flex items-center gap-2 flex-wrap">
          <div
            className="flex gap-1 bg-jarvis-card border border-jarvis-border rounded-lg p-1"
            title="主周期：K 线 / MACD / 足迹图 / 成交流画像窗口全部跟随此处，全页周期强制一致（足迹图最高 30m，1h/4h/1d 时回退 30m）"
          >
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
          <div className="flex gap-1 bg-jarvis-card border border-jarvis-border rounded-lg p-1">
            <button
              onClick={() => setShowMacd((v) => !v)}
              title="MACD(12,26,9) 副图：DIF/DEA 双线 + 红绿柱，复用主图 K 线数据并随主周期联动，不新增接口请求"
              className={clsx(
                "px-3 py-1 text-sm rounded-md transition-colors",
                showMacd
                  ? "bg-jarvis-blue text-jarvis-accent-fg"
                  : "text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              MACD·{showMacd ? "开" : "关"}
            </button>
            <button
              onClick={() => setTrapOn((v) => !v)}
              title="诱多诱空识别：自动标出「假突破」陷阱——冲破前高又被打回=诱多（红色倒三角，别追多），跌破前低又收回=诱空（绿色正三角，别追空）。点击图上警示牌看逐条理由与操作建议；随主周期联动，与 K 线页开关状态同步。识别引擎未接入时显示本地规则识别的演示数据"
              className={clsx(
                "px-3 py-1 text-sm rounded-md transition-colors",
                trapOn
                  ? "bg-jarvis-blue text-jarvis-accent-fg"
                  : "text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              诱多诱空·{trapOn ? "开" : "关"}
            </button>
            {trapOn && (
              <span
                className={clsx(
                  "flex items-center gap-1 px-2 py-1 text-xs",
                  trapData?.mock ? "text-jarvis-yellow" : "text-jarvis-text-secondary",
                )}
                title={
                  trapData
                    ? `当前窗口识别到 ${trapMarks?.length ?? 0} 个诱多/诱空信号${trapData.mock ? "（演示数据：识别引擎未接入，本地假突破规则识别）" : ""}`
                    : trapApiState === "loading"
                      ? "信号加载中…"
                      : "K 线数不足（需 ≥25 根），暂无法识别"
                }
              >
                <AlertTriangle size={12} />
                {trapData ? (trapMarks?.length ?? 0) : "…"}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* ②③ 主区：左 K 线 + 右深度阶梯（xl 以下阶梯折到主区下方） */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-4">
        <div className="card p-0 overflow-hidden relative">
          {candles.length > 0 ? (
            <>
              <KlineChart
                data={candles}
                volumeData={volumes}
                height={showMacd ? 396 : 520}
                trapMarks={trapMarks}
                onTrapClick={(mark) => setSelectedTrap(mark)}
              />
              {showMacd && (
                <div className="border-t border-jarvis-border">
                  <MacdPane candles={candles} height={124} />
                </div>
              )}
              {/* 陷阱原因卡片：固定右上角浮层，不遮点击处的 K 线形态 */}
              {selectedTrap && (
                <TrapReasonCard
                  mark={selectedTrap}
                  onClose={() => setSelectedTrap(null)}
                />
              )}
            </>
          ) : (
            <div
              className="flex flex-col items-center justify-center text-jarvis-text-secondary"
              style={{ height: 520 }}
            >
              {klineLoading ? (
                <>
                  <div className="w-6 h-6 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin mb-3" />
                  <p className="text-sm">正在获取 K 线数据...</p>
                </>
              ) : klineError ? (
                <p className="text-sm text-jarvis-red">
                  K 线加载失败:{klineError}
                </p>
              ) : (
                <p className="text-sm">暂无 K 线数据</p>
              )}
            </div>
          )}
        </div>
        <DepthLadder
          depth={depthPoll.data}
          loading={depthPoll.loading}
          error={depthPoll.error}
          symbol={symbol}
        />
      </div>

      {/* ④ 成交流区标题栏：WS 状态 + 视图切换（柱状图|列表）+ 柱状图专属控件 */}
      <div className="flex items-center gap-2 flex-wrap">
        <p className="stat-label mb-0 flex items-center gap-1.5">
          <Activity size={14} />
          {tapeView === "footprint"
            ? "成交流足迹图"
            : `成交流画像（${tape?.window_min ?? windowMin}min 窗口）`}
        </p>
        <span
          className={clsx(
            "text-[10px] px-1.5 py-0.5 rounded border font-medium flex items-center gap-1",
            tape?.ws_ready
              ? "bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40"
              : "bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border",
          )}
          title={
            tape?.ws_ready
              ? "WS aggTrade 实时流已连接"
              : "WS 实时流未就绪（连接中或未开启）"
          }
        >
          <Radio size={9} />
          {tape?.ws_ready ? "实时" : "未就绪"}
        </span>
        {tapeView === "list" && wsActive && tape?.breakdown && (
          <span className="text-[10px] text-jarvis-text-secondary font-mono">
            窗口总额 {fmtUsd(tape.breakdown.total_usd)}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2 flex-wrap">
          {/* 周期强制跟随主周期，不设独立周期入口；此处仅保留视图切换 */}
          <span
            className="text-[10px] text-jarvis-text-secondary/70 font-mono"
            title={`足迹图周期与画像窗口跟随顶部主周期（当前 ${tf}${tapeView === "footprint" && fpFollow(tf) !== tf ? `，足迹图回退 ${fpFollow(tf)}` : ""}）`}
          >
            周期跟随主图·{tapeView === "footprint" ? fpTf : `${windowMin}min 窗口`}
          </span>

          {/* 视图切换：足迹图 | 列表（localStorage 持久） */}
          <div className="flex gap-0.5 bg-jarvis-card border border-jarvis-border rounded-lg p-0.5">
            {(
              [
                ["footprint", "足迹图", LayoutGrid],
                ["list", "列表", List],
              ] as const
            ).map(([v, label, Icon]) => (
              <button
                key={v}
                onClick={() => setTapeView(v)}
                className={clsx(
                  "px-2 py-0.5 text-xs rounded-md transition-colors flex items-center gap-1",
                  tapeView === v
                    ? "bg-jarvis-blue text-jarvis-accent-fg"
                    : "text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                <Icon size={11} />
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ④-足迹图视图：主体多空统计卡 + canvas 足迹图 */}
      {tapeView === "footprint" && (
        <>
          {footprint?.ok && footprint.actors && (
            <ActorBiasCard
              actors={footprint.actors}
              disclaimer={footprint.disclaimer}
            />
          )}
          <div className="card p-3">
            <FootprintPane
              resp={footprint}
              loading={fpPoll.loading}
              error={fpPoll.error}
            />
          </div>
        </>
      )}

      {/* ④-列表视图主体：占位态 / 错误态 / 三块布局 */}
      {tapeView === "list" &&
        (!tape ? (
          <div className="card py-10 flex items-center justify-center">
            {tapePoll.loading ? (
              <div className="w-5 h-5 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin" />
            ) : (
              <p className="text-xs text-jarvis-red">
                成交流接口异常：{tapePoll.error ?? "未知错误"}
              </p>
            )}
          </div>
        ) : tape.ok === false ? (
          <div className="card py-10 text-center">
            <p className="text-xs text-jarvis-red">
              成交流接口异常：{tape.error ?? "未知错误"}
            </p>
          </div>
        ) : !wsActive ? (
          <div className="card py-10 text-center">
            <Radio
              size={20}
              className="mx-auto mb-2 text-jarvis-text-secondary/60"
            />
            <p className="text-sm text-jarvis-text-secondary">
              等待 WS 数据流就绪（需后端开启 ws_enabled）
            </p>
            <p className="text-xs text-jarvis-text-secondary/60 mt-1">
              {tape.ws_ready === false
                ? "aggTrade 实时流未连接 · 设置 → system.ws_enabled 开启后自动聚合"
                : "WS 已连接，窗口内暂无成交数据，等待积累"}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 xl:grid-cols-[3fr_4fr_3fr] gap-4">
            <VerdictCard tape={tape} />
            <FingerprintTable fps={tape.fingerprints ?? []} />
            <RecentTradeList trades={recentDesc} />
          </div>
        ))}

      {/* ⑤ 页尾免责声明 */}
      {tape?.disclaimer && (
        <p className="text-[10px] text-jarvis-text-secondary/60 leading-snug">
          {tape.disclaimer}
        </p>
      )}
    </div>
  );
}
