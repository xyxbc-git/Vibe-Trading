/**
 * 金十财经事件日历（任务 U · 事件风险窗口）。
 *
 * 两个导出：
 *   EventRibbon      —— K 线页顶部标记条：未来 24h 高影响事件 + 倒计时，
 *                       风险窗口内整条变红（数据瞬间插针警示）。
 *   TodayEventsCard  —— 总览页「今日大事」小卡片（默认导出）。
 *
 * 数据 60s 轮询（后端 1h TTL 挡在前面，前端轮询只为刷倒计时与新事件）；
 * 未配置金十 key 时后端诚实返回 configured:false —— 卡片显示注册引导，
 * 横条隐藏（绝不伪造日历）。
 */
import { useEffect, useState } from "react";
import { CalendarClock, AlertTriangle, KeyRound } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api } from "@/api/client";

export interface CalendarEvent {
  ts: number;
  country: string;
  title: string;
  importance: number;
  previous?: string | number | null;
  forecast?: string | number | null;
  actual?: string | number | null;
  minutes_to?: number;
}

export interface EventsUpcomingResponse {
  ok: boolean;
  configured: boolean;
  events: CalendarEvent[];
  risk: {
    available?: boolean;
    in_window: boolean;
    event?: CalendarEvent | null;
    minutes_to?: number | null;
    note?: string | null;
  } | null;
  stale?: boolean;
  note?: string | null;
}

const fetchUpcoming = (hours: number, minStar: number) =>
  api.get<EventsUpcomingResponse>(`/events/upcoming?hours=${hours}&min_star=${minStar}`);

function Stars({ n }: { n: number }) {
  return <span className="text-jarvis-yellow">{"★".repeat(Math.max(1, n))}</span>;
}

/** 倒计时人话：-8 → 「8分钟前」；75 → 「1h15m 后」。 */
function countdown(minutes: number | undefined): string {
  if (minutes === undefined || minutes === null || Number.isNaN(minutes)) return "";
  const m = Math.round(minutes);
  if (m < 0) return `${-m} 分钟前`;
  if (m < 60) return `${m} 分钟后`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m 后`;
}

/** 本地每秒重算倒计时（数据 60s 才轮询一次，倒计时不能一分钟跳一次）。 */
function useNowTick(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!enabled) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [enabled]);
  return now;
}

/** K 线页顶部事件标记条：只显示未来 24h 高影响（后端 min_star 阈值）事件。 */
export function EventRibbon() {
  const { data } = usePolling(() => fetchUpcoming(24, 3), 60_000, []);
  const now = useNowTick(Boolean(data?.configured && data.events.length));
  if (!data?.configured || !data.ok) return null; // 未配置：横条不占位，引导在总览卡片
  const events = (data.events ?? []).slice(0, 6);
  if (!events.length) return null;
  const inWindow = Boolean(data.risk?.in_window);
  return (
    <div
      className={`flex items-center gap-3 px-3 py-1.5 rounded-md border text-xs overflow-x-auto ${
        inWindow
          ? "bg-jarvis-red/15 border-jarvis-red text-jarvis-red"
          : "bg-jarvis-panel border-jarvis-border text-jarvis-text-secondary"
      }`}
    >
      {inWindow ? (
        <AlertTriangle size={14} className="shrink-0" />
      ) : (
        <CalendarClock size={14} className="shrink-0" />
      )}
      {inWindow && data.risk?.note ? (
        <span className="shrink-0 font-medium">{data.risk.note}</span>
      ) : null}
      {events.map((e) => {
        const mins = (e.ts - now) / 60;
        return (
          <span key={`${e.ts}-${e.title}`} className="shrink-0 whitespace-nowrap">
            <Stars n={e.importance} /> {e.country}
            {e.title}（{countdown(mins)}）
          </span>
        );
      })}
      {data.stale ? <span className="shrink-0 opacity-60">[缓存]</span> : null}
    </div>
  );
}

/** 总览页「今日大事」卡片：未配置 key 时显示注册与落盘引导。 */
export default function TodayEventsCard() {
  const { data, loading } = usePolling(() => fetchUpcoming(24, 1), 60_000, []);
  const now = useNowTick(Boolean(data?.configured && (data.events?.length ?? 0) > 0));
  return (
    <div className="card flex flex-col">
      <p className="stat-label flex items-center gap-2 mb-3">
        <CalendarClock size={14} />
        今日大事（金十日历）
      </p>
      {!data && loading ? (
        <p className="text-xs text-jarvis-text-secondary">加载中…</p>
      ) : !data?.configured ? (
        <div className="text-xs text-jarvis-text-secondary space-y-2">
          <p className="flex items-center gap-1.5">
            <KeyRound size={13} className="shrink-0 text-jarvis-yellow" />
            未配置金十数据 secret-key
          </p>
          <p>
            去 open.jin10.com 注册开发者账号，控制台申请 secret-key 后写入
            <code className="mx-1 px-1 bg-jarvis-panel rounded">
              ~/.vibe-trading/jin10.json
            </code>
            （格式 {"{"}"secret_key": "..."{"}"}），保存后 1 分钟内自动生效。
          </p>
        </div>
      ) : !data.ok ? (
        <p className="text-xs text-jarvis-red">{data.note ?? "事件日历拉取失败"}</p>
      ) : (
        <div className="space-y-1.5">
          {data.risk?.in_window && data.risk.note ? (
            <p className="text-xs text-jarvis-red flex items-center gap-1.5">
              <AlertTriangle size={13} className="shrink-0" />
              {data.risk.note}
            </p>
          ) : null}
          {(data.events ?? []).slice(0, 5).map((e) => {
            const mins = (e.ts - now) / 60;
            const hot = e.importance >= 3;
            return (
              <div
                key={`${e.ts}-${e.title}`}
                className="flex items-center gap-2 text-xs border-b border-jarvis-border/50 last:border-0 py-1"
              >
                <Stars n={e.importance} />
                <span
                  className={`flex-1 min-w-0 truncate ${
                    hot ? "text-jarvis-text" : "text-jarvis-text-secondary"
                  }`}
                  title={`${e.country}${e.title}`}
                >
                  {e.country}
                  {e.title}
                </span>
                <span
                  className={`shrink-0 ${
                    hot && mins >= 0 && mins <= 60
                      ? "text-jarvis-red"
                      : "text-jarvis-text-secondary"
                  }`}
                >
                  {countdown(mins)}
                </span>
              </div>
            );
          })}
          {!(data.events ?? []).length ? (
            <p className="text-xs text-jarvis-text-secondary">未来 24h 无已收录事件</p>
          ) : null}
        </div>
      )}
    </div>
  );
}
