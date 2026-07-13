import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { clsx } from "clsx";
import {
  History,
  Briefcase,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  ChevronDown,
  FilterX,
  Tag,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, formatPrice } from "@/api/client";
import BehaviorTagDialog from "@/components/cards/BehaviorTagDialog";
import {
  type ClosedTrade,
  type OpenPosition,
  REASON_CN,
  SOURCE_META,
  SYSTEM_CN,
  REGIME_CN,
  resonanceBucket,
  parseSystems,
  fmtTradeTs,
} from "@/lib/tradeMeta";

const PAGE = 50;

/* ─────────────────── 通用小组件 ─────────────────── */

function DirBadge({ direction }: { direction: "long" | "short" }) {
  const short = direction === "short";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded-full whitespace-nowrap",
        short ? "bg-jarvis-red/15 text-jarvis-red" : "bg-jarvis-green/15 text-jarvis-green",
      )}
    >
      {short ? <TrendingDown size={11} /> : <TrendingUp size={11} />}
      {short ? "空" : "多"}
    </span>
  );
}

function SourceBadge({
  source,
  tf,
}: {
  source: string | null;
  tf: string | null;
}) {
  const src = SOURCE_META[String(source ?? "")];
  if (!src) return <span className="text-jarvis-text-secondary">—</span>;
  return (
    <span className={clsx("text-[11px] px-1.5 py-0.5 rounded-full whitespace-nowrap", src.cls)}>
      {src.label}
      {source === "twelve" && tf ? ` ${tf}` : ""}
    </span>
  );
}

/** 依据信号系统中文标签（用户重点关注列，样式加强） */
function SystemTags({ raw }: { raw: string | null }) {
  const systems = parseSystems(raw);
  if (systems.length === 0)
    return <span className="text-jarvis-text-secondary">—</span>;
  return (
    <span className="inline-flex items-center gap-1 flex-wrap">
      {systems.map((s) => (
        <span
          key={s}
          className="text-[11px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 border border-jarvis-blue/30 text-jarvis-blue whitespace-nowrap"
        >
          {SYSTEM_CN[s] ?? s}
        </span>
      ))}
    </span>
  );
}

/** T1.4 复盘标签徽章：有标签显示彩色标签，无标签显示「补标」引导按钮 */
function BehaviorTagBadge({
  tag,
  onClick,
}: {
  tag: string | null | undefined;
  onClick: () => void;
}) {
  if (!tag) {
    return (
      <button
        onClick={onClick}
        title="给这笔交易补复盘标签"
        className="inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded-full border border-dashed border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-yellow hover:border-jarvis-yellow/50 transition-colors whitespace-nowrap"
      >
        <Tag size={10} />
        补标
      </button>
    );
  }
  const plan = tag.includes("按计划");
  const bad = ["恐慌", "追高", "贪婪"].some((w) => tag.includes(w));
  return (
    <button
      onClick={onClick}
      title="点击修改复盘标签"
      className={clsx(
        "inline-flex items-center gap-0.5 text-[11px] px-1.5 py-0.5 rounded-full transition-opacity hover:opacity-80 whitespace-nowrap",
        plan
          ? "bg-jarvis-green/15 text-jarvis-green"
          : bad
            ? "bg-jarvis-red/15 text-jarvis-red"
            : "bg-jarvis-yellow/15 text-jarvis-yellow",
      )}
    >
      <Tag size={10} />
      {tag}
    </button>
  );
}

function PnlText({
  usdt,
  pct,
  size = "sm",
}: {
  usdt: number;
  pct: number | null;
  size?: "sm" | "base";
}) {
  const win = usdt >= 0;
  return (
    <span
      className={clsx(
        "font-mono whitespace-nowrap",
        size === "base" ? "text-sm" : "text-xs",
        win ? "text-jarvis-green" : "text-jarvis-red",
      )}
    >
      {win ? "+" : ""}
      {usdt.toFixed(2)}U
      {pct != null && (
        <span className="text-[10px] ml-1 opacity-70">
          ({win ? "+" : ""}
          {pct.toFixed(2)}%)
        </span>
      )}
    </span>
  );
}

const thCls = "pb-2 pr-3 font-medium whitespace-nowrap";
const tdCls = "py-2 pr-3 align-middle";
const selectCls =
  "bg-jarvis-bg border border-jarvis-border rounded-md px-2 py-1.5 text-xs text-jarvis-text focus:outline-none focus:border-jarvis-blue cursor-pointer";

/* ─────────────────── 页面 ─────────────────── */

export default function TradeRecords() {
  // 持仓中（后端已补 current_price / pnl_pct / pnl_usdt），15s 追浮动盈亏
  const { data: openData } = usePolling(
    () => api.get<OpenPosition[]>("/positions?status=open"),
    15_000,
  );
  // 已平仓完整历史（后端默认排除 replay 回放样本），30s 轮询追新
  const {
    data: closedData,
    loading,
    error,
    refetch,
  } = usePolling(
    () => api.get<ClosedTrade[]>("/positions?status=closed&limit=1000"),
    30_000,
  );

  const [fSymbol, setFSymbol] = useState("all");
  const [fDir, setFDir] = useState<"all" | "long" | "short">("all");
  const [fSource, setFSource] = useState("all");
  const [fSystem, setFSystem] = useState("all");
  const [showCount, setShowCount] = useState(PAGE);
  // T1.4 补标/改标弹窗（自动平仓的单事后在这里补）
  const [tagDialog, setTagDialog] = useState<{
    positionId: number;
    title: string;
    currentTag: string | null;
  } | null>(null);

  const open = useMemo(
    () => (openData ?? []).filter((p) => p.signal_source !== "replay"),
    [openData],
  );
  const closedAll = useMemo(
    () =>
      (closedData ?? [])
        // 后端已默认排除回放样本，前端双保险
        .filter((t) => t.signal_source !== "replay")
        .sort((a, b) => Number(b.closed_ts ?? 0) - Number(a.closed_ts ?? 0)),
    [closedData],
  );

  const symbols = useMemo(
    () =>
      [...new Set([...closedAll, ...open].map((t) => t.symbol))].sort(),
    [closedAll, open],
  );

  const filtered = useMemo(
    () =>
      closedAll.filter((t) => {
        if (fSymbol !== "all" && t.symbol !== fSymbol) return false;
        if (fDir !== "all" && t.direction !== fDir) return false;
        if (fSource !== "all" && String(t.signal_source ?? "") !== fSource)
          return false;
        if (fSystem !== "all" && !parseSystems(t.signal_systems).includes(fSystem))
          return false;
        return true;
      }),
    [closedAll, fSymbol, fDir, fSource, fSystem],
  );

  // 筛选条件变化时回到第一页
  useEffect(() => {
    setShowCount(PAGE);
  }, [fSymbol, fDir, fSource, fSystem]);

  const visible = filtered.slice(0, showCount);
  const hasMore = filtered.length > showCount;

  // 无限滚动：底部哨兵进入视口自动加载下一页
  const sentinelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore) return;
    const ob = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) setShowCount((c) => c + PAGE);
      },
      { rootMargin: "240px" },
    );
    ob.observe(el);
    return () => ob.disconnect();
  }, [hasMore]);

  const stats = useMemo(() => {
    const total = filtered.length;
    const wins = filtered.filter((t) => Number(t.realized_pnl_usdt ?? 0) >= 0).length;
    const pnlSum = filtered.reduce((s, t) => s + Number(t.realized_pnl_usdt ?? 0), 0);
    return { total, winRate: total > 0 ? (wins / total) * 100 : null, pnlSum };
  }, [filtered]);

  const floatSum = open.reduce((s, p) => s + Number(p.pnl_usdt ?? 0), 0);
  const filterActive =
    fSymbol !== "all" || fDir !== "all" || fSource !== "all" || fSystem !== "all";
  const resetFilters = () => {
    setFSymbol("all");
    setFDir("all");
    setFSource("all");
    setFSystem("all");
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <History size={22} />
          交易记录
        </h1>
        <div className="flex items-center gap-3 text-xs text-jarvis-text-secondary">
          <span>30s 自动刷新 · 记录持久保存</span>
          <button
            onClick={refetch}
            className="flex items-center gap-1 px-2 py-1 rounded-md border border-jarvis-border hover:text-jarvis-text hover:border-jarvis-blue transition-colors"
            title="立即刷新"
          >
            <RefreshCw size={12} />
            刷新
          </button>
        </div>
      </div>

      {/* ── 持仓中（置顶，带浮动盈亏） ── */}
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <p className="stat-label flex items-center gap-2 mb-0">
            <Briefcase size={14} />
            持仓中（{open.length}）
          </p>
          {open.length > 0 && (
            <span className="text-xs text-jarvis-text-secondary">
              浮动盈亏合计 <PnlText usdt={floatSum} pct={null} size="base" />
            </span>
          )}
        </div>
        {open.length === 0 ? (
          <p className="text-sm text-jarvis-text-secondary py-3">
            暂无持仓 · 12 系统共识达标后会自动开仓，成交后即出现在这里
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                  <th className={thCls}>开仓时间</th>
                  <th className={thCls}>币种</th>
                  <th className={thCls}>方向</th>
                  <th className={thCls}>开仓价 → 现价</th>
                  <th className={clsx(thCls, "text-right")}>数量</th>
                  <th className={thCls}>止损 / 止盈</th>
                  <th className={clsx(thCls, "text-right")}>浮动盈亏</th>
                  <th className={thCls}>来源</th>
                  <th className={thCls}>依据信号系统</th>
                  <th className={thCls}>共振</th>
                  <th className={clsx(thCls, "pr-0")}>市场状态</th>
                </tr>
              </thead>
              <tbody>
                {open.map((p) => {
                  const systems = parseSystems(p.signal_systems);
                  return (
                    <tr
                      key={p.id}
                      className="border-b border-jarvis-border/40 last:border-0 hover:bg-white/[0.03]"
                    >
                      <td className={clsx(tdCls, "font-mono text-jarvis-text-secondary whitespace-nowrap")}>
                        {fmtTradeTs(p.opened_ts, true)}
                      </td>
                      <td className={clsx(tdCls, "text-jarvis-text font-medium whitespace-nowrap")}>
                        {p.symbol}
                      </td>
                      <td className={tdCls}>
                        <DirBadge direction={p.direction} />
                      </td>
                      <td className={clsx(tdCls, "font-mono text-jarvis-text whitespace-nowrap")}>
                        {formatPrice(p.entry_price)} →{" "}
                        {p.current_price != null ? formatPrice(p.current_price) : "…"}
                      </td>
                      <td className={clsx(tdCls, "font-mono text-jarvis-text text-right whitespace-nowrap")}>
                        {Number(p.qty ?? 0)}
                      </td>
                      <td className={clsx(tdCls, "font-mono text-jarvis-text-secondary whitespace-nowrap")}>
                        {p.stop_loss ? formatPrice(p.stop_loss) : "—"} /{" "}
                        {p.take_profit ? formatPrice(p.take_profit) : "—"}
                      </td>
                      <td className={clsx(tdCls, "text-right")}>
                        {p.pnl_usdt != null ? (
                          <PnlText
                            usdt={Number(p.pnl_usdt)}
                            pct={p.pnl_pct != null ? Number(p.pnl_pct) : null}
                          />
                        ) : (
                          <span className="text-jarvis-text-secondary">取价中…</span>
                        )}
                      </td>
                      <td className={tdCls}>
                        <SourceBadge source={p.signal_source} tf={p.signal_tf} />
                      </td>
                      <td className={tdCls}>
                        <SystemTags raw={p.signal_systems} />
                      </td>
                      <td className={clsx(tdCls, "whitespace-nowrap")}>
                        {p.signal_source === "twelve" && systems.length > 0 ? (
                          <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-jarvis-blue/10 text-jarvis-blue whitespace-nowrap">
                            {resonanceBucket(systems.length)}
                          </span>
                        ) : (
                          <span className="text-jarvis-text-secondary">—</span>
                        )}
                      </td>
                      <td className={clsx(tdCls, "pr-0 whitespace-nowrap")}>
                        {p.signal_source === "twelve" ? (
                          <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-jarvis-border/40 text-jarvis-text-secondary whitespace-nowrap">
                            {REGIME_CN[String(p.signal_regime ?? "")] ?? "未知"}
                          </span>
                        ) : (
                          <span className="text-jarvis-text-secondary">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── 筛选栏 + 统计 ── */}
      <div className="card">
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={fSymbol}
            onChange={(e) => setFSymbol(e.target.value)}
            className={selectCls}
            title="按币种筛选"
          >
            <option value="all">全部币种</option>
            {symbols.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>

          <div className="flex rounded-md border border-jarvis-border overflow-hidden">
            {(
              [
                { v: "all", label: "全部" },
                { v: "long", label: "多" },
                { v: "short", label: "空" },
              ] as const
            ).map((d, i) => (
              <button
                key={d.v}
                onClick={() => setFDir(d.v)}
                className={clsx(
                  "px-2.5 py-1.5 text-xs transition-colors",
                  i > 0 && "border-l border-jarvis-border",
                  fDir === d.v
                    ? d.v === "short"
                      ? "bg-jarvis-red/15 text-jarvis-red"
                      : d.v === "long"
                        ? "bg-jarvis-green/15 text-jarvis-green"
                        : "bg-jarvis-blue/20 text-jarvis-blue"
                    : "bg-jarvis-bg text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                {d.label}
              </button>
            ))}
          </div>

          <select
            value={fSource}
            onChange={(e) => setFSource(e.target.value)}
            className={selectCls}
            title="按下单来源筛选"
          >
            <option value="all">全部来源</option>
            {Object.entries(SOURCE_META).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </select>

          <select
            value={fSystem}
            onChange={(e) => setFSystem(e.target.value)}
            className={selectCls}
            title="按依据信号系统筛选"
          >
            <option value="all">全部信号系统</option>
            {Object.entries(SYSTEM_CN).map(([k, cn]) => (
              <option key={k} value={k}>
                {cn}
              </option>
            ))}
          </select>

          {filterActive && (
            <button
              onClick={resetFilters}
              className="flex items-center gap-1 px-2 py-1.5 text-xs text-jarvis-text-secondary hover:text-jarvis-text rounded-md border border-jarvis-border transition-colors"
            >
              <FilterX size={12} />
              清除筛选
            </button>
          )}

          <span className="ml-auto text-xs text-jarvis-text-secondary whitespace-nowrap">
            {filterActive ? `筛出 ${stats.total} / ${closedAll.length} 笔` : `共 ${stats.total} 笔`}
            {stats.winRate != null && (
              <>
                {" · 胜率 "}
                <span className="text-jarvis-text font-mono">
                  {stats.winRate.toFixed(1)}%
                </span>
              </>
            )}
            {" · 累计 "}
            <PnlText usdt={stats.pnlSum} pct={null} />
          </span>
        </div>
      </div>

      {/* ── 已平仓全宽表格 ── */}
      <div className="card">
        {error && !closedData ? (
          <p className="text-sm text-jarvis-text-secondary py-10 text-center">
            记录加载失败：{error}
          </p>
        ) : loading && !closedData ? (
          <div className="space-y-2 animate-pulse py-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-9 rounded-lg bg-jarvis-border/30" />
            ))}
          </div>
        ) : closedAll.length === 0 ? (
          <div className="py-14 text-center space-y-2">
            <History size={28} className="mx-auto text-jarvis-text-secondary/50" />
            <p className="text-sm text-jarvis-text">还没有已平仓的交易记录</p>
            <p className="text-xs text-jarvis-text-secondary max-w-md mx-auto leading-relaxed">
              12 系统自动跟盘每 15 分钟跑一轮，共识达标才下单，出手频率不高属正常；
              也可以去驾驶舱点「一键回放预积累」，先看看 12 套系统的历史胜率。
            </p>
            <Link
              to="/"
              className="inline-block text-xs text-jarvis-blue hover:underline"
            >
              去驾驶舱看看 →
            </Link>
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-14 text-center space-y-2">
            <FilterX size={24} className="mx-auto text-jarvis-text-secondary/50" />
            <p className="text-sm text-jarvis-text-secondary">当前筛选条件下没有匹配的记录</p>
            <button
              onClick={resetFilters}
              className="text-xs text-jarvis-blue hover:underline"
            >
              清除筛选条件
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                  <th className={thCls}>平仓时间</th>
                  <th className={thCls}>币种</th>
                  <th className={thCls}>方向</th>
                  <th className={thCls}>开仓价 → 平仓价</th>
                  <th className={clsx(thCls, "text-right")}>数量</th>
                  <th className={clsx(thCls, "text-right")}>盈亏</th>
                  <th className={thCls}>平仓原因</th>
                  <th className={thCls}>复盘标签</th>
                  <th className={thCls}>来源</th>
                  <th className={thCls}>依据信号系统</th>
                  <th className={thCls}>共振</th>
                  <th className={clsx(thCls, "pr-0")}>市场状态</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((t) => {
                  const systems = parseSystems(t.signal_systems);
                  return (
                    <tr
                      key={t.id}
                      className="border-b border-jarvis-border/40 last:border-0 hover:bg-white/[0.03]"
                    >
                      <td className={clsx(tdCls, "font-mono text-jarvis-text-secondary whitespace-nowrap")}>
                        {fmtTradeTs(t.closed_ts, true)}
                      </td>
                      <td className={clsx(tdCls, "text-jarvis-text font-medium whitespace-nowrap")}>
                        {t.symbol}
                      </td>
                      <td className={tdCls}>
                        <DirBadge direction={t.direction} />
                      </td>
                      <td className={clsx(tdCls, "font-mono text-jarvis-text whitespace-nowrap")}>
                        {formatPrice(t.entry_price)} → {formatPrice(t.exit_price)}
                      </td>
                      <td className={clsx(tdCls, "font-mono text-jarvis-text text-right whitespace-nowrap")}>
                        {Number(t.qty ?? 0)}
                      </td>
                      <td className={clsx(tdCls, "text-right")}>
                        <PnlText
                          usdt={Number(t.realized_pnl_usdt ?? 0)}
                          pct={Number(t.realized_pnl_pct ?? 0)}
                        />
                      </td>
                      <td className={clsx(tdCls, "text-jarvis-text-secondary whitespace-nowrap")}>
                        {REASON_CN[String(t.exit_reason ?? "")] ??
                          String(t.exit_reason ?? "—")}
                      </td>
                      <td className={clsx(tdCls, "whitespace-nowrap")}>
                        <BehaviorTagBadge
                          tag={t.behavior_tag}
                          onClick={() =>
                            setTagDialog({
                              positionId: t.id,
                              title: `${t.symbol} ${t.direction === "short" ? "空单" : "多单"} #${t.id} · ${
                                Number(t.realized_pnl_pct ?? 0) >= 0 ? "+" : ""
                              }${Number(t.realized_pnl_pct ?? 0).toFixed(2)}%`,
                              currentTag: t.behavior_tag ?? null,
                            })
                          }
                        />
                      </td>
                      <td className={tdCls}>
                        <SourceBadge source={t.signal_source} tf={t.signal_tf} />
                      </td>
                      <td className={clsx(tdCls, "min-w-40")}>
                        <SystemTags raw={t.signal_systems} />
                      </td>
                      <td className={clsx(tdCls, "whitespace-nowrap")}>
                        {t.signal_source === "twelve" && systems.length > 0 ? (
                          <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-jarvis-blue/10 text-jarvis-blue whitespace-nowrap">
                            {resonanceBucket(systems.length)}
                          </span>
                        ) : (
                          <span className="text-jarvis-text-secondary">—</span>
                        )}
                      </td>
                      <td className={clsx(tdCls, "pr-0 whitespace-nowrap")}>
                        {t.signal_source === "twelve" ? (
                          <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-jarvis-border/40 text-jarvis-text-secondary whitespace-nowrap">
                            {REGIME_CN[String(t.signal_regime ?? "")] ?? "未知"}
                          </span>
                        ) : (
                          <span className="text-jarvis-text-secondary">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {hasMore && (
              <div ref={sentinelRef} className="pt-1">
                <button
                  onClick={() => setShowCount((c) => c + PAGE)}
                  className="w-full flex items-center justify-center gap-1 py-2.5 text-xs text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
                >
                  <ChevronDown size={13} />
                  加载更早记录（剩余 {filtered.length - showCount} 笔）
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {tagDialog && (
        <BehaviorTagDialog
          positionId={tagDialog.positionId}
          title={tagDialog.title}
          currentTag={tagDialog.currentTag}
          onClose={() => setTagDialog(null)}
          onSaved={() => refetch()}
        />
      )}
    </div>
  );
}
