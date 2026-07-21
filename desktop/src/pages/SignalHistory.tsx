import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { clsx } from "clsx";
import {
  History,
  Filter,
  FilterX,
  Trash2,
  GitCommitHorizontal,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  RefreshCw,
} from "lucide-react";
import {
  api,
  formatPrice,
  type SignalChangeRow,
  type SignalDirection,
  type KeyLevel,
} from "@/api/client";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";

/** 每页条数档位（服务端 limit/offset 分页） */
const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
const DEFAULT_PAGE_SIZE = 50;
/** 每页条数偏好持久化键 */
const PAGE_SIZE_KEY = "jarvis.signalHistory.pageSize";

function loadPageSize(): number {
  try {
    const v = parseInt(localStorage.getItem(PAGE_SIZE_KEY) ?? "", 10);
    return (PAGE_SIZE_OPTIONS as readonly number[]).includes(v)
      ? v
      : DEFAULT_PAGE_SIZE;
  } catch {
    return DEFAULT_PAGE_SIZE;
  }
}

function savePageSize(n: number): void {
  try {
    localStorage.setItem(PAGE_SIZE_KEY, String(n));
  } catch {
    /* storage 不可用（隐私模式等）——偏好记忆静默降级 */
  }
}

/** 周期筛选项（与后端 TwelveTf 对齐） */
const TF_OPTIONS = ["5m", "15m", "30m", "1h", "4h", "1d"] as const;

/** 12 信号系统筛选项（表格行展示优先用后端下发的 name_cn） */
const SYSTEM_OPTIONS: { value: string; label: string }[] = [
  { value: "turtle", label: "海龟" },
  { value: "dow", label: "道氏" },
  { value: "elliott", label: "波浪" },
  { value: "volatility", label: "波动率" },
  { value: "gann", label: "江恩" },
  { value: "chanlun", label: "缠论" },
  { value: "rule123", label: "123法则" },
  { value: "gap", label: "跳空" },
  { value: "martingale", label: "马丁" },
  { value: "oscillator", label: "摆动" },
  { value: "triple_rsi", label: "三重RSI" },
  { value: "arbitrage", label: "套利" },
];

const SYSTEM_LABEL: Record<string, string> = Object.fromEntries(
  SYSTEM_OPTIONS.map((o) => [o.value, o.label]),
);

/** 方向 → 中文徽章配色（涨绿跌红中性灰，与全站语义色一致） */
const DIR_META: Record<
  SignalDirection,
  { label: string; badge: string; dot: string }
> = {
  bullish: {
    label: "看涨",
    badge: "bg-jarvis-green/15 text-jarvis-green",
    dot: "bg-jarvis-green",
  },
  bearish: {
    label: "看跌",
    badge: "bg-jarvis-red/15 text-jarvis-red",
    dot: "bg-jarvis-red",
  },
  neutral: {
    label: "中性",
    badge: "bg-jarvis-border/40 text-jarvis-text-secondary",
    dot: "bg-jarvis-text-secondary",
  },
};

/** 变更类型 → 中文徽章（direction/strength/plan/levels） */
const KIND_META: Record<string, { label: string; cls: string }> = {
  direction: {
    label: "方向",
    cls: "bg-jarvis-blue/10 border-jarvis-blue/30 text-jarvis-blue",
  },
  strength: {
    label: "强度",
    cls: "bg-jarvis-yellow/10 border-jarvis-yellow/30 text-jarvis-yellow",
  },
  plan: {
    label: "计划",
    cls: "bg-jarvis-purple/10 border-jarvis-purple/30 text-jarvis-purple",
  },
  levels: {
    label: "关键位",
    cls: "bg-jarvis-green/10 border-jarvis-green/30 text-jarvis-green",
  },
};

/** 后端快照 JSON（prev_json / new_json 的公共形状） */
type Snapshot = SignalChangeRow["prev_json"] | SignalChangeRow["new_json"];

/** 两侧快照逐字段对比结果（true = 有变化，需在「变更后」侧高亮） */
interface ChangedFlags {
  direction: boolean;
  strength: boolean;
  entry: boolean;
  stop: boolean;
  tp: boolean;
  levels: boolean;
}

/* ─────────────────── 工具函数 ─────────────────── */

/** 任意字符串安全映射到方向元数据（后端快照里 direction 是宽松 string） */
function dirMeta(d: string | null | undefined) {
  if (d === "bullish" || d === "bearish" || d === "neutral") return DIR_META[d];
  return null;
}

/** unix 秒 → 本地时间 MM-DD HH:mm:ss */
function fmtTs(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** datetime-local 输入值（本地时区）→ unix 秒；空/非法返回 undefined */
function localInputToUnix(v: string): number | undefined {
  if (!v) return undefined;
  const ms = new Date(v).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : undefined;
}

/** 强度 0~1 → 百分比文本（与 SignalBoard 同口径 clamp 后取整） */
function fmtStrength(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(Number(s))) return "—";
  const v = Math.max(0, Math.min(1, Number(s)));
  return `${Math.round(v * 100)}%`;
}

/* ─────────────────── 通用小组件 ─────────────────── */

/** 方向徽章；direction 为空（首条记录无 prev）时显示占位横线 */
function DirBadge({ d }: { d: string | null | undefined }) {
  const meta = dirMeta(d);
  if (!meta) return <span className="text-jarvis-text-secondary">—</span>;
  return (
    <span
      className={clsx(
        "inline-flex items-center text-[11px] px-1.5 py-0.5 rounded-full whitespace-nowrap",
        meta.badge,
      )}
    >
      {meta.label}
    </span>
  );
}

/** 变更类型徽章组 */
function KindBadges({ kinds }: { kinds: string[] | null }) {
  const list = kinds ?? [];
  if (list.length === 0)
    return <span className="text-jarvis-text-secondary">—</span>;
  return (
    <span className="inline-flex items-center gap-1 flex-wrap">
      {list.map((k) => {
        const meta = KIND_META[k] ?? {
          label: k,
          cls: "bg-jarvis-border/40 border-jarvis-border text-jarvis-text-secondary",
        };
        return (
          <span
            key={k}
            className={clsx(
              "text-[11px] px-1.5 py-0.5 rounded border whitespace-nowrap",
              meta.cls,
            )}
          >
            {meta.label}
          </span>
        );
      })}
    </span>
  );
}

/** 对比卡片里的单行字段（hl=true 时新值高亮为主题蓝） */
function DetailItem({
  label,
  value,
  hl,
}: {
  label: string;
  value: React.ReactNode;
  hl?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3 text-xs">
      <span className="text-jarvis-text-secondary shrink-0">{label}</span>
      <span
        className={clsx(
          "text-right",
          hl ? "text-jarvis-blue font-medium" : "text-jarvis-text",
        )}
      >
        {value}
      </span>
    </div>
  );
}

/** 关键位列表（对比卡片内） */
function LevelList({ levels, hl }: { levels: KeyLevel[]; hl?: boolean }) {
  if (levels.length === 0)
    return <span className="text-jarvis-text-secondary text-xs">—</span>;
  return (
    <div className={clsx("space-y-0.5", hl && "text-jarvis-blue")}>
      {levels.map((l, i) => (
        <div key={`${l.label}-${i}`} className="flex justify-between gap-3 text-xs">
          <span className={hl ? "text-jarvis-blue/80" : "text-jarvis-text-secondary"}>
            {l.label}
          </span>
          <span className={clsx("font-mono", hl ? "font-medium" : "text-jarvis-text")}>
            {formatPrice(l.price)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** prev/new 参数快照卡片；changed 仅传给「变更后」侧用于逐项高亮 */
function SnapshotCard({
  title,
  json,
  changed,
}: {
  title: string;
  json: Snapshot;
  changed?: ChangedFlags;
}) {
  if (!json) {
    return (
      <div className="rounded-lg border border-jarvis-border/60 bg-jarvis-bg/40 p-3">
        <p className="text-xs text-jarvis-text-secondary mb-2">{title}</p>
        <p className="text-xs text-jarvis-text-secondary py-3 text-center">
          首次记录，无历史快照
        </p>
      </div>
    );
  }
  const plan = json.trade_plan ?? null;
  const levels = json.key_levels ?? [];
  return (
    <div className="rounded-lg border border-jarvis-border/60 bg-jarvis-bg/40 p-3 space-y-1.5">
      <p className="text-xs text-jarvis-text-secondary mb-2">{title}</p>
      <DetailItem
        label="方向"
        value={<DirBadge d={json.direction} />}
        hl={changed?.direction}
      />
      <DetailItem
        label="强度"
        value={<span className="font-mono">{fmtStrength(json.strength)}</span>}
        hl={changed?.strength}
      />
      {plan ? (
        <>
          <DetailItem
            label="入场价"
            value={<span className="font-mono">{formatPrice(plan.entry)}</span>}
            hl={changed?.entry}
          />
          <DetailItem
            label="止损价"
            value={<span className="font-mono">{formatPrice(plan.stop_loss)}</span>}
            hl={changed?.stop}
          />
          <DetailItem
            label="止盈价"
            value={<span className="font-mono">{formatPrice(plan.take_profit)}</span>}
            hl={changed?.tp}
          />
        </>
      ) : (
        <DetailItem
          label="交易计划"
          value="无计划"
          hl={changed ? changed.entry || changed.stop || changed.tp : false}
        />
      )}
      <div className="pt-1 border-t border-jarvis-border/40">
        <p
          className={clsx(
            "text-xs mb-1",
            changed?.levels ? "text-jarvis-blue" : "text-jarvis-text-secondary",
          )}
        >
          关键位
        </p>
        <LevelList levels={levels} hl={changed?.levels} />
      </div>
    </div>
  );
}

/** 行展开详情：prev_json vs new_json 左右对比，变化项在右侧高亮 */
function ExpandedDetail({ row }: { row: SignalChangeRow }) {
  const prev = row.prev_json;
  const next = row.new_json;
  // 逐字段对比：方向/强度/计划三价/关键位（列表整体比对）
  const changed: ChangedFlags = {
    direction: (prev?.direction ?? null) !== (next?.direction ?? null),
    strength: (prev?.strength ?? null) !== (next?.strength ?? null),
    entry: (prev?.trade_plan?.entry ?? null) !== (next?.trade_plan?.entry ?? null),
    stop:
      (prev?.trade_plan?.stop_loss ?? null) !==
      (next?.trade_plan?.stop_loss ?? null),
    tp:
      (prev?.trade_plan?.take_profit ?? null) !==
      (next?.trade_plan?.take_profit ?? null),
    levels:
      JSON.stringify(prev?.key_levels ?? []) !==
      JSON.stringify(next?.key_levels ?? []),
  };
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 py-1">
      <SnapshotCard title="变更前" json={prev} />
      <SnapshotCard title="变更后（变化项高亮）" json={next} changed={changed} />
    </div>
  );
}

/* ─────────────────── 变动时间线（系统筛选激活时） ─────────────────── */

function ChangeTimeline({ rows, system }: { rows: SignalChangeRow[]; system: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="card">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between text-left"
        title={open ? "折叠时间线" : "展开时间线"}
      >
        <p className="stat-label flex items-center gap-2 mb-0">
          <GitCommitHorizontal size={14} />
          {SYSTEM_LABEL[system] ?? system} · 变动时间线（本页 {rows.length} 条）
        </p>
        <ChevronDown
          size={14}
          className={clsx(
            "text-jarvis-text-secondary transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      {open && (
        <div className="relative pl-6 mt-4">
          {/* 纵向轴线 */}
          <div className="absolute left-2 top-1 bottom-1 w-px bg-jarvis-border" />
          <ul className="space-y-3">
            {rows.map((r) => {
              // 方向翻转节点（prev 存在且与 new 不同）用大圆点强调
              const flip =
                r.prev_direction != null && r.prev_direction !== r.new_direction;
              const meta = dirMeta(r.new_direction) ?? DIR_META.neutral;
              return (
                <li key={r.id} className="relative">
                  <span
                    className={clsx(
                      "absolute rounded-full",
                      flip
                        ? "left-[3px] top-1 w-2.5 h-2.5 ring-2 ring-jarvis-card"
                        : "left-[5px] top-1.5 w-1.5 h-1.5 opacity-70",
                      meta.dot,
                    )}
                  />
                  <div className="flex flex-wrap items-center gap-2 text-xs leading-5">
                    <span className="font-mono text-jarvis-text-secondary whitespace-nowrap">
                      {fmtTs(r.ts)}
                    </span>
                    <span className="inline-flex items-center gap-1">
                      <DirBadge d={r.prev_direction} />
                      <span className="text-jarvis-text-secondary">→</span>
                      <DirBadge d={r.new_direction} />
                    </span>
                    <span className={clsx("text-jarvis-text", flip && "font-medium")}>
                      {r.summary}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/* ─────────────────── 页面 ─────────────────── */

const thCls = "pb-2 pr-3 font-medium whitespace-nowrap";
const tdCls = "py-2 pr-3 align-middle";
const selectCls =
  "bg-jarvis-bg border border-jarvis-border rounded-md px-2 py-1.5 text-xs text-jarvis-text focus:outline-none focus:border-jarvis-blue cursor-pointer";
const dtInputCls =
  "bg-jarvis-bg border border-jarvis-border rounded-md px-2 py-1 text-xs text-jarvis-text focus:outline-none focus:border-jarvis-blue";
const smallBtnCls =
  "flex items-center gap-1 px-2 py-1.5 text-xs rounded-md border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text transition-colors disabled:opacity-40 disabled:cursor-not-allowed";

export default function SignalHistory() {
  // 首次进入读 URL query 预置 symbol/tf（SignalBoard 带 ?symbol=&tf= 跳转过来）
  const [searchParams] = useSearchParams();
  const { supported } = useSymbol();

  const [fSymbol, setFSymbol] = useState<string>(() => {
    const s = (searchParams.get("symbol") ?? "").toUpperCase();
    return /^[A-Z0-9]{2,20}USDT$/.test(s) ? s : "all";
  });
  const [fTf, setFTf] = useState<string>(() => {
    const tf = searchParams.get("tf") ?? "";
    return (TF_OPTIONS as readonly string[]).includes(tf) ? tf : "all";
  });
  const [fSystem, setFSystem] = useState("all");
  // datetime-local 原始字符串（本地时区），请求前转 unix 秒
  const [fSince, setFSince] = useState("");
  const [fUntil, setFUntil] = useState("");
  const [page, setPage] = useState(1);
  // 每页条数（10/20/50），初值读 localStorage 偏好
  const [pageSize, setPageSize] = useState<number>(loadPageSize);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [opMsg, setOpMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const since = useMemo(() => localInputToUnix(fSince), [fSince]);
  const until = useMemo(() => localInputToUnix(fUntil), [fUntil]);

  // 服务端分页 + 条件查询；30s 轮询追新（首屏后台刷新不闪骨架屏）
  const { data, loading, error, refetch } = usePolling(
    () =>
      api.twelveSignalHistory({
        symbol: fSymbol !== "all" ? fSymbol : undefined,
        tf: fTf !== "all" ? fTf : undefined,
        system: fSystem !== "all" ? fSystem : undefined,
        since,
        until,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      }),
    30_000,
    [fSymbol, fTf, fSystem, since, until, page, pageSize],
  );

  const rows = useMemo(() => data?.rows ?? [], [data]);
  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // 传输层错误或业务层 ok=false 都按错误态处理
  const apiError = error ?? (data && !data.ok ? (data.error ?? "接口返回异常") : null);

  const filterActive =
    fSymbol !== "all" || fTf !== "all" || fSystem !== "all" || !!fSince || !!fUntil;

  // 删除后总数缩水可能导致当前页越界（如末页记录被删光），自动回正到最后一页
  useEffect(() => {
    if (!loading && data?.ok && total > 0 && page > totalPages) {
      setPage(totalPages);
    }
  }, [loading, data, total, page, totalPages]);

  // 币种下拉 = useSymbol supported 列表；URL 带来的陌生币种动态补入避免选项丢失
  const symbolOptions = useMemo(() => {
    const opts = supported.map((s) => s.value);
    if (fSymbol !== "all" && !opts.includes(fSymbol)) opts.push(fSymbol);
    return opts;
  }, [supported, fSymbol]);

  /** 修改筛选条件统一入口：回第一页并清空勾选/展开 */
  function applyFilter(update: () => void) {
    update();
    setPage(1);
    setSelected(new Set());
    setExpandedId(null);
  }

  function resetFilters() {
    applyFilter(() => {
      setFSymbol("all");
      setFTf("all");
      setFSystem("all");
      setFSince("");
      setFUntil("");
    });
  }

  function goPage(p: number) {
    setPage(Math.min(Math.max(1, p), totalPages));
    setSelected(new Set());
    setExpandedId(null);
  }

  /** 切每页条数：回第一页重新拉取，并持久化偏好 */
  function changePageSize(n: number) {
    savePageSize(n);
    setPageSize(n);
    setPage(1);
    setSelected(new Set());
    setExpandedId(null);
  }

  /* ── 勾选 ── */
  const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const toggleAll = () =>
    setSelected(allChecked ? new Set() : new Set(rows.map((r) => r.id)));
  const invertSelection = () =>
    setSelected(
      (prev) => new Set(rows.filter((r) => !prev.has(r.id)).map((r) => r.id)),
    );
  const toggleOne = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  /* ── 删除（单行/批量/条件清空共用；均二次确认后执行并 refetch） ── */
  async function runDelete(
    payload: Parameters<typeof api.twelveSignalHistoryDelete>[0],
    confirmText: string,
    resetToFirstPage = false,
  ) {
    if (!window.confirm(confirmText)) return;
    setDeleting(true);
    setOpMsg(null);
    try {
      const res = await api.twelveSignalHistoryDelete(payload);
      if (res.ok) {
        setOpMsg({ ok: true, text: `已删除 ${res.deleted ?? 0} 条记录` });
        setSelected(new Set());
        setExpandedId(null);
        if (resetToFirstPage) setPage(1);
        refetch();
      } else {
        setOpMsg({ ok: false, text: `删除失败：${res.error ?? "未知错误"}` });
      }
    } catch (e) {
      setOpMsg({
        ok: false,
        text: `删除失败：${e instanceof Error ? e.message : "网络错误"}`,
      });
    } finally {
      setDeleting(false);
      setTimeout(() => setOpMsg(null), 5000);
    }
  }

  const deleteSelected = () =>
    void runDelete(
      { ids: [...selected] },
      `确认删除所选 ${selected.size} 条变更记录？删除后不可恢复。`,
    );

  // 「清空当前筛选结果」：按当前 symbol/tf/system 条件 + before=当前时间 整批删除
  const clearFiltered = () => {
    const scopeDesc = [
      fSymbol !== "all" ? fSymbol : "全部币种",
      fTf !== "all" ? fTf : "全部周期",
      fSystem !== "all" ? (SYSTEM_LABEL[fSystem] ?? fSystem) : "全部系统",
    ].join(" / ");
    void runDelete(
      {
        symbol: fSymbol !== "all" ? fSymbol : undefined,
        tf: fTf !== "all" ? fTf : undefined,
        system: fSystem !== "all" ? fSystem : undefined,
        before: Math.floor(Date.now() / 1000),
      },
      `确认清空「${scopeDesc}」范围内的全部 ${total} 条历史记录？删除后不可恢复。`,
      true,
    );
  };

  const deleteOne = (r: SignalChangeRow) =>
    void runDelete(
      { ids: [r.id] },
      `确认删除这条 ${r.symbol} ${r.tf} ${r.name_cn ?? r.system} 变更记录？删除后不可恢复。`,
    );

  /* ── 渲染 ── */
  return (
    <div className="space-y-4">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <History size={22} />
          信号变更历史
        </h1>
        <div className="flex items-center gap-3 text-xs text-jarvis-text-secondary">
          <span>{pageSize} 条/页 · 30s 自动刷新</span>
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

      {/* 筛选栏 */}
      <div className="card">
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-1 text-xs text-jarvis-text-secondary">
            <Filter size={13} />
            筛选
          </span>

          <select
            value={fSymbol}
            onChange={(e) => applyFilter(() => setFSymbol(e.target.value))}
            className={selectCls}
            title="按币种筛选"
          >
            <option value="all">全部币种</option>
            {symbolOptions.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>

          <select
            value={fTf}
            onChange={(e) => applyFilter(() => setFTf(e.target.value))}
            className={selectCls}
            title="按周期筛选"
          >
            <option value="all">全部周期</option>
            {TF_OPTIONS.map((tf) => (
              <option key={tf} value={tf}>
                {tf}
              </option>
            ))}
          </select>

          <select
            value={fSystem}
            onChange={(e) => applyFilter(() => setFSystem(e.target.value))}
            className={selectCls}
            title="按信号系统筛选"
          >
            <option value="all">全部系统</option>
            {SYSTEM_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>

          <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
            从
            <input
              type="datetime-local"
              value={fSince}
              onChange={(e) => applyFilter(() => setFSince(e.target.value))}
              className={dtInputCls}
              title="起始时间（本地时区）"
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
            至
            <input
              type="datetime-local"
              value={fUntil}
              onChange={(e) => applyFilter(() => setFUntil(e.target.value))}
              className={dtInputCls}
              title="结束时间（本地时区）"
            />
          </label>

          {filterActive && (
            <button onClick={resetFilters} className={smallBtnCls}>
              <FilterX size={12} />
              清除筛选
            </button>
          )}

          <span className="ml-auto text-xs text-jarvis-text-secondary whitespace-nowrap">
            {filterActive ? `筛出 ${total} 条` : `共 ${total} 条`}
          </span>
        </div>
      </div>

      {/* 系统筛选激活时的变动时间线（可折叠） */}
      {fSystem !== "all" && rows.length > 0 && (
        <ChangeTimeline rows={rows} system={fSystem} />
      )}

      {/* 流水表格 */}
      <div className="card">
        {/* 批量操作栏 */}
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <p className="stat-label flex items-center gap-2 mb-0">
            <History size={14} />
            变更流水
          </p>
          {opMsg && (
            <span
              className={clsx(
                "text-xs",
                opMsg.ok ? "text-jarvis-green" : "text-jarvis-red",
              )}
            >
              {opMsg.text}
            </span>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-xs text-jarvis-text-secondary">
              已选 {selected.size} 条
            </span>
            <button
              onClick={invertSelection}
              disabled={deleting || rows.length === 0}
              className={smallBtnCls}
              title="反选本页记录"
            >
              反选
            </button>
            <button
              onClick={deleteSelected}
              disabled={deleting || selected.size === 0}
              className={clsx(
                smallBtnCls,
                "border-jarvis-red/40 text-jarvis-red hover:text-jarvis-red hover:bg-jarvis-red/10",
              )}
              title="删除所选记录（二次确认）"
            >
              <Trash2 size={12} />
              删除所选
            </button>
            <button
              onClick={clearFiltered}
              disabled={deleting || total === 0}
              className={clsx(
                smallBtnCls,
                "border-jarvis-red/40 text-jarvis-red hover:text-jarvis-red hover:bg-jarvis-red/10",
              )}
              title="按当前筛选条件整批删除（二次确认）"
            >
              <Trash2 size={12} />
              清空当前筛选结果
            </button>
          </div>
        </div>

        {apiError && !data?.ok ? (
          /* 错误态 */
          <div className="py-14 text-center space-y-2">
            <p className="text-sm text-jarvis-text-secondary">
              历史记录加载失败：{apiError}
            </p>
            <button
              onClick={refetch}
              className="text-xs text-jarvis-blue hover:underline"
            >
              重试
            </button>
          </div>
        ) : loading && !data ? (
          /* 首屏加载骨架 */
          <div className="space-y-2 animate-pulse py-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-9 rounded-lg bg-jarvis-border/30" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          /* 空态（区分「无任何记录」与「筛选无结果」） */
          <div className="py-14 text-center space-y-2">
            <History size={28} className="mx-auto text-jarvis-text-secondary/50" />
            <p className="text-sm text-jarvis-text">
              {filterActive ? "当前筛选条件下没有变更记录" : "还没有信号变更记录"}
            </p>
            <p className="text-xs text-jarvis-text-secondary max-w-md mx-auto leading-relaxed">
              {filterActive
                ? "试试放宽筛选条件，或清除筛选查看全部流水。"
                : "信号引擎每轮计算侦测到方向/强度/计划/关键位实质变更时，会自动落一条流水。"}
            </p>
            {filterActive && (
              <button
                onClick={resetFilters}
                className="text-xs text-jarvis-blue hover:underline"
              >
                清除筛选条件
              </button>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                  <th className={clsx(thCls, "w-8")}>
                    <input
                      type="checkbox"
                      checked={allChecked}
                      onChange={toggleAll}
                      className="cursor-pointer accent-jarvis-blue align-middle"
                      title="全选/取消全选本页"
                    />
                  </th>
                  <th className={thCls}>时间</th>
                  <th className={thCls}>币种</th>
                  <th className={thCls}>周期</th>
                  <th className={thCls}>系统</th>
                  <th className={thCls}>方向变化</th>
                  <th className={thCls}>强度变化</th>
                  <th className={thCls}>变更类型</th>
                  <th className={thCls}>摘要</th>
                  <th className={clsx(thCls, "text-right")}>当时价格</th>
                  <th className={clsx(thCls, "pr-0 text-right")}>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <SignalHistoryRow
                    key={r.id}
                    row={r}
                    checked={selected.has(r.id)}
                    expanded={expandedId === r.id}
                    deleting={deleting}
                    onToggleCheck={() => toggleOne(r.id)}
                    onToggleExpand={() =>
                      setExpandedId((cur) => (cur === r.id ? null : r.id))
                    }
                    onDelete={() => deleteOne(r)}
                  />
                ))}
              </tbody>
            </table>

            {/* 分页控制（服务端 limit/offset） */}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-3 border-t border-jarvis-border/40 mt-1">
              <span className="text-xs text-jarvis-text-secondary">
                共 {total} 条 · 第 {page}/{totalPages} 页
              </span>
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
                  每页
                  <select
                    value={pageSize}
                    onChange={(e) => changePageSize(Number(e.target.value))}
                    disabled={loading}
                    className={selectCls}
                    title="每页显示条数（记住偏好）"
                  >
                    {PAGE_SIZE_OPTIONS.map((n) => (
                      <option key={n} value={n}>
                        {n} 条
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  onClick={() => goPage(page - 1)}
                  disabled={page <= 1 || loading}
                  className={smallBtnCls}
                >
                  <ChevronLeft size={12} />
                  上一页
                </button>
                <button
                  onClick={() => goPage(page + 1)}
                  disabled={page >= totalPages || loading}
                  className={smallBtnCls}
                >
                  下一页
                  <ChevronRight size={12} />
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─────────────────── 表格行（含展开详情行） ─────────────────── */

function SignalHistoryRow({
  row,
  checked,
  expanded,
  deleting,
  onToggleCheck,
  onToggleExpand,
  onDelete,
}: {
  row: SignalChangeRow;
  checked: boolean;
  expanded: boolean;
  deleting: boolean;
  onToggleCheck: () => void;
  onToggleExpand: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <tr
        onClick={onToggleExpand}
        className="border-b border-jarvis-border/40 last:border-0 hover:bg-white/[0.03] cursor-pointer"
        title="点击展开/收起变更前后参数对比"
      >
        <td className={tdCls} onClick={(e) => e.stopPropagation()}>
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggleCheck}
            className="cursor-pointer accent-jarvis-blue align-middle"
          />
        </td>
        <td className={clsx(tdCls, "font-mono text-jarvis-text-secondary whitespace-nowrap")}>
          {fmtTs(row.ts)}
        </td>
        <td className={clsx(tdCls, "text-jarvis-text font-medium whitespace-nowrap")}>
          {row.symbol}
        </td>
        <td className={clsx(tdCls, "font-mono text-jarvis-text whitespace-nowrap")}>
          {row.tf}
        </td>
        <td className={clsx(tdCls, "whitespace-nowrap")}>
          <span className="text-[11px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 border border-jarvis-blue/30 text-jarvis-blue whitespace-nowrap">
            {row.name_cn ?? SYSTEM_LABEL[row.system] ?? row.system}
          </span>
        </td>
        <td className={clsx(tdCls, "whitespace-nowrap")}>
          <span className="inline-flex items-center gap-1">
            <DirBadge d={row.prev_direction} />
            <span className="text-jarvis-text-secondary">→</span>
            <DirBadge d={row.new_direction} />
          </span>
        </td>
        <td className={clsx(tdCls, "font-mono text-jarvis-text whitespace-nowrap")}>
          {fmtStrength(row.prev_strength)}
          <span className="text-jarvis-text-secondary mx-1">→</span>
          {fmtStrength(row.new_strength)}
        </td>
        <td className={tdCls}>
          <KindBadges kinds={row.change_kinds} />
        </td>
        <td className={clsx(tdCls, "min-w-40")}>
          <span
            className="block max-w-80 truncate text-jarvis-text"
            title={row.summary}
          >
            {row.summary}
          </span>
        </td>
        <td className={clsx(tdCls, "font-mono text-jarvis-text text-right whitespace-nowrap")}>
          {formatPrice(row.price)}
        </td>
        <td className={clsx(tdCls, "pr-0 text-right whitespace-nowrap")}>
          <span className="inline-flex items-center gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              disabled={deleting}
              className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-red hover:bg-jarvis-red/10 transition-colors disabled:opacity-40"
              title="删除本条记录"
            >
              <Trash2 size={13} />
            </button>
            <ChevronDown
              size={13}
              className={clsx(
                "text-jarvis-text-secondary transition-transform",
                expanded && "rotate-180",
              )}
            />
          </span>
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-jarvis-border/40 last:border-0 bg-white/[0.02]">
          <td colSpan={11} className="py-3 pr-3 pl-8">
            <ExpandedDetail row={row} />
          </td>
        </tr>
      )}
    </>
  );
}
