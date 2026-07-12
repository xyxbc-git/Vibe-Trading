import { useEffect, useRef, useState } from "react";
import { usePriceMeta } from "@/hooks/usePrice";
import { AlertCircle } from "lucide-react";

/**
 * 顶栏数据刷新倒计时。
 *
 * 主体是一个 10s 周期的倒计时环（随剩余时间消退，刷新瞬间回满并闪绿），
 * 锚定 PriceProvider 最近一次**真实请求成功时刻**——不是独立计时器，
 * 视觉节奏与实际轮询严格同步；请求失败时环转红并显示感叹号。
 * hover 弹出全应用各数据源的刷新频率表。
 */

/** 各数据源刷新频率一览（与代码实际轮询/缓存参数对齐，改动轮询时同步维护）：
 *  - 行情价格：hooks/usePrice.tsx PRICE_POLL_INTERVAL_MS = 10s，后端 /alerts/price 无缓存
 *  - K 线：pages/Chart.tsx 1m 10s / 其它 60s；后端 /kline 缓存 60s
 *  - 信号矩阵：cards/SignalBoard.tsx 60s；后端 /twelve/signals 缓存 5m=60s、其它 120s
 *  - 共识计划：pages/Chart.tsx 90s；后端 /twelve/consensus 缓存 180s
 *  - 市场情报：pages/MarketIntel.tsx 60s
 *  - 牛熊状态：layout/RegimeBadge.tsx 300s；后端 /regime 缓存 900s
 *  - 持仓/挂单：pages/Trading.tsx 3s / 15s
 *  - 钱包余额：layout/Header.tsx 30s
 */
const REFRESH_TABLE: { name: string; freq: string; note?: string }[] = [
  { name: "行情价格（顶栏 / K线最新价）", freq: "10 秒", note: "Binance ticker 实时价" },
  { name: "K 线图", freq: "60 秒", note: "1m 周期 10 秒 · 后端缓存 60 秒" },
  { name: "信号矩阵（十二系统）", freq: "60 秒", note: "后端缓存 5m 60 秒 / 其它 120 秒" },
  { name: "共识交易计划", freq: "90 秒", note: "后端缓存 180 秒" },
  { name: "市场情报", freq: "60 秒" },
  { name: "牛熊市场状态", freq: "5 分钟", note: "后端缓存 15 分钟" },
  { name: "持仓 / 挂单", freq: "3 秒 / 15 秒" },
  { name: "钱包余额", freq: "30 秒" },
];

const RING_R = 7;
const RING_C = 2 * Math.PI * RING_R;

export default function RefreshCountdown() {
  const meta = usePriceMeta();
  const [now, setNow] = useState(() => Date.now());
  const [open, setOpen] = useState(false);
  const [flash, setFlash] = useState(false);
  const prevFetchRef = useRef<number | null>(null);

  // 本地 250ms tick 只驱动本组件重渲染，不影响 Header 其它部分
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, []);

  // 真实刷新落地瞬间（lastFetchAt 变化）闪一下作为反馈
  useEffect(() => {
    if (meta.lastFetchAt == null) return;
    const changed =
      prevFetchRef.current !== null && meta.lastFetchAt !== prevFetchRef.current;
    prevFetchRef.current = meta.lastFetchAt;
    if (!changed) return;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 450);
    return () => clearTimeout(t);
  }, [meta.lastFetchAt]);

  const hasAnchor = meta.lastFetchAt != null;
  const remainMs = hasAnchor
    ? Math.max(0, meta.lastFetchAt! + meta.intervalMs - now)
    : 0;
  const remainSec = Math.ceil(remainMs / 1000);
  /** 剩余占比：1=刚刷新（满环）→ 0=即将刷新（空环） */
  const remainRatio = hasAnchor ? remainMs / meta.intervalMs : 0;
  const failed = meta.error != null;

  const ringColor = failed ? "#f85149" : flash ? "#3fb950" : "#58a6ff";
  const label = failed ? "重试中" : hasAnchor ? `${remainSec}s` : "—";

  return (
    <div className="relative">
      <button
        type="button"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        title={
          failed
            ? "行情请求失败，正在自动重试（悬停看各数据刷新频率）"
            : "距下一次行情价格刷新（悬停看各数据刷新频率）"
        }
        className="flex items-center gap-1 cursor-default"
      >
        <span className="relative inline-flex items-center justify-center">
          <svg width="18" height="18" viewBox="0 0 18 18" className="-rotate-90">
            <circle
              cx="9"
              cy="9"
              r={RING_R}
              fill="none"
              stroke="#30363d"
              strokeWidth="2"
            />
            <circle
              cx="9"
              cy="9"
              r={RING_R}
              fill="none"
              stroke={ringColor}
              strokeWidth="2"
              strokeDasharray={RING_C}
              strokeDashoffset={RING_C * (1 - remainRatio)}
              strokeLinecap="round"
              style={{ transition: "stroke-dashoffset 0.25s linear, stroke 0.2s" }}
            />
          </svg>
          {failed && (
            <AlertCircle
              size={9}
              className="absolute text-jarvis-red"
              strokeWidth={3}
            />
          )}
        </span>
        <span
          className={`hidden sm:inline text-xs font-mono tabular-nums ${
            failed
              ? "text-jarvis-red"
              : flash
                ? "text-jarvis-green"
                : "text-jarvis-text-secondary"
          }`}
        >
          {label}
        </span>
      </button>

      {open && (
        <div
          className="absolute top-full right-0 mt-2 z-50 w-72 bg-jarvis-card border border-jarvis-border rounded-lg shadow-lg p-3"
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => setOpen(false)}
        >
          <p className="text-xs text-jarvis-text font-medium mb-2">
            数据刷新频率
          </p>
          <div className="space-y-1.5">
            {REFRESH_TABLE.map((row) => (
              <div key={row.name} className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-xs text-jarvis-text">{row.name}</p>
                  {row.note && (
                    <p className="text-[10px] text-jarvis-text-secondary">
                      {row.note}
                    </p>
                  )}
                </div>
                <span className="text-xs font-mono text-jarvis-blue shrink-0">
                  {row.freq}
                </span>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-jarvis-text-secondary mt-2 pt-2 border-t border-jarvis-border">
            行情上次刷新：
            {meta.lastFetchAt != null
              ? new Date(meta.lastFetchAt).toLocaleTimeString("en-GB", {
                  hour12: false,
                })
              : "—"}
            {failed && <span className="text-jarvis-red ml-1">· 请求失败，自动重试中</span>}
          </p>
        </div>
      )}
    </div>
  );
}
