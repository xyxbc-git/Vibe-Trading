import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { Scale, PenLine, RefreshCw, Activity } from "lucide-react";
import { useApi } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import {
  mentorApi,
  recallIntent,
  calcRR,
  lossStreak,
  LIGHT_CN,
  type MentorLight,
  type MentorOutcomeInput,
  type MentorOutcomeResult,
  type MentorPlanRow,
} from "@/api/mentor";

const LIGHT_DOT: Record<MentorLight, string> = {
  green: "bg-jarvis-green",
  yellow: "bg-jarvis-yellow",
  red: "bg-jarvis-red",
};

const RESULT_CN: Record<MentorOutcomeResult, string> = {
  win: "盈利",
  loss: "亏损",
  scratch: "保本",
  skipped: "未执行",
};

function pnlCls(v: number | null | undefined) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "text-jarvis-text-secondary";
  return n > 0 ? "text-jarvis-green" : "text-jarvis-red";
}

/** 行为透视数据（V2）：当日计数 / 分时段胜率 / 情绪高低分对比——由台账行现算，
 *  真/mock 数据同一口径；后端 V1 行为字段上线后可在 normalizeStats 优先采用 */
function useBehavior(rows: MentorPlanRow[]) {
  return useMemo(() => {
    const today = (() => {
      const d = new Date();
      const p = (n: number) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    })();
    const todayRows = rows.filter((r) => String(r.created_at ?? "").startsWith(today));
    const executed = (r: MentorPlanRow) =>
      r.outcome
        ? r.outcome.result !== "skipped"
        : recallIntent(r.id)?.action === "executed";

    // 分时段（按创建时刻小时分桶）
    const buckets = [
      { key: "dawn", label: "凌晨 0-6", lo: 0, hi: 6 },
      { key: "morning", label: "早 6-12", lo: 6, hi: 12 },
      { key: "afternoon", label: "午 12-18", lo: 12, hi: 18 },
      { key: "night", label: "晚 18-24", lo: 18, hi: 24 },
    ].map((b) => {
      const done = rows.filter((r) => {
        if (!r.outcome || (r.outcome.result !== "win" && r.outcome.result !== "loss")) return false;
        const hh = Number(String(r.created_at ?? "").slice(11, 13));
        return Number.isFinite(hh) && hh >= b.lo && hh < b.hi;
      });
      const wins = done.filter((r) => r.outcome?.result === "win").length;
      return {
        ...b,
        trades: done.length,
        winRate: done.length ? Math.round((wins / done.length) * 100) : null,
      };
    });

    // 情绪高分（≥4）vs 低分（≤3）
    const emoBucket = (hi: boolean) => {
      const done = rows.filter(
        (r) =>
          r.outcome &&
          (r.outcome.result === "win" || r.outcome.result === "loss") &&
          r.emotion_score != null &&
          (hi ? Number(r.emotion_score) >= 4 : Number(r.emotion_score) <= 3),
      );
      const wins = done.filter((r) => r.outcome?.result === "win").length;
      const pnl = done.reduce((s, r) => s + (Number(r.outcome?.pnl_pct) || 0), 0);
      return {
        trades: done.length,
        winRate: done.length ? Math.round((wins / done.length) * 100) : null,
        pnl: Math.round(pnl * 100) / 100,
      };
    };

    return {
      todaySubmitted: todayRows.length,
      todayExecuted: todayRows.filter(executed).length,
      todayLossStreak: lossStreak(todayRows),
      buckets,
      emotionHigh: emoBucket(true),
      emotionLow: emoBucket(false),
    };
  }, [rows]);
}

/** 信任看板：改变行为的核心反馈，视觉最重 */
function TrustBoard({ mock, refreshKey, rows }: { mock: boolean; refreshKey: number; rows: MentorPlanRow[] }) {
  const { data: stats } = useApi(() => mentorApi.stats(), [refreshKey]);
  const behavior = useBehavior(rows);

  const red = stats?.by_light.find((b) => b.light === "red");
  const green = stats?.by_light.find((b) => b.light === "green");
  const yellow = stats?.by_light.find((b) => b.light === "yellow");
  const fmtRate = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);
  const fmtPnl = (v: number | null | undefined) =>
    v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
  /** 桶盈亏展示：mock 口径累计 total、真后端口径均值 avg（标「均」） */
  const bucketPnl = (b?: { total_pnl_pct?: number; avg_pnl_pct?: number | null }) => {
    if (!b) return "—";
    if (b.avg_pnl_pct != null) return `均 ${fmtPnl(b.avg_pnl_pct)}`;
    return fmtPnl(b.total_pnl_pct);
  };

  return (
    <div className="card mb-4 border-jarvis-blue/40">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-base font-semibold text-jarvis-text">
          <Scale size={18} className="text-jarvis-blue" />
          信任看板 · 数据替导师说话
        </h2>
        {(mock || stats?.mock) && (
          <span className="rounded-full border border-jarvis-yellow/50 px-2 py-0.5 text-[10px] text-jarvis-yellow">
            演示数据 · 待后端联调
          </span>
        )}
      </div>

      {/* 听 vs 不听 —— 最重的对比 */}
      <div className="mb-3 grid grid-cols-2 gap-3">
        <div className="rounded-xl border border-jarvis-green/40 bg-jarvis-green/10 p-4 text-center">
          <p className="text-sm text-jarvis-text-secondary">听导师的单</p>
          <p className="mt-1 font-mono text-3xl font-bold text-jarvis-green">
            {fmtRate(stats?.followed.win_rate_pct)}
          </p>
          <p className="mt-1 text-xs text-jarvis-text-secondary">
            {stats?.followed.trades ?? 0} 笔 · 盈亏{" "}
            <span className={pnlCls(stats?.followed.avg_pnl_pct ?? stats?.followed.total_pnl_pct)}>
              {bucketPnl(stats?.followed)}
            </span>
          </p>
        </div>
        <div className="rounded-xl border border-jarvis-red/40 bg-jarvis-red/10 p-4 text-center">
          <p className="text-sm text-jarvis-text-secondary">凭感觉硬来的单</p>
          <p className="mt-1 font-mono text-3xl font-bold text-jarvis-red">
            {fmtRate(stats?.not_followed.win_rate_pct)}
          </p>
          <p className="mt-1 text-xs text-jarvis-text-secondary">
            {stats?.not_followed.trades ?? 0} 笔 · 盈亏{" "}
            <span className={pnlCls(stats?.not_followed.avg_pnl_pct ?? stats?.not_followed.total_pnl_pct)}>
              {bucketPnl(stats?.not_followed)}
            </span>
          </p>
        </div>
      </div>

      {/* 红黄绿灯各自战绩 */}
      <div className="grid grid-cols-3 gap-3">
        {[green, yellow, red].map((b, i) => {
          const light = (["green", "yellow", "red"] as MentorLight[])[i];
          return (
            <div key={light} className="rounded-lg border border-jarvis-border bg-jarvis-bg/60 p-3 text-center">
              <p className="flex items-center justify-center gap-1.5 text-xs text-jarvis-text-secondary">
                <span className={clsx("h-2 w-2 rounded-full", LIGHT_DOT[light])} />
                {light === "green" ? "绿灯单" : light === "yellow" ? "黄灯单" : "红灯单（你仍执行的）"}
              </p>
              <p className="mt-1 font-mono text-xl font-semibold text-jarvis-text">
                {fmtRate(b?.win_rate_pct)}
              </p>
              <p className="text-[11px] text-jarvis-text-secondary">
                执行 {b?.executed ?? 0}/{b?.plans ?? 0} 笔 ·{" "}
                <span className={pnlCls(b?.avg_pnl_pct ?? b?.total_pnl_pct)}>{bucketPnl(b)}</span>
              </p>
            </div>
          );
        })}
      </div>

      {/* V2 行为透视：当日纪律计数 / 分时段胜率 / 情绪高低分对比（台账现算） */}
      <div className="mt-4 border-t border-jarvis-border/60 pt-3">
        <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-jarvis-text-secondary">
          <Activity size={13} className="text-jarvis-blue" />
          行为透视 · 按当前筛选范围现算
        </p>

        {/* 当日计数 */}
        <div className="grid grid-cols-3 gap-3">
          <div className="rounded-lg border border-jarvis-border bg-jarvis-bg/60 p-3 text-center">
            <p className="text-[11px] text-jarvis-text-secondary">今日提交</p>
            <p className="font-mono text-xl font-semibold text-jarvis-text">{behavior.todaySubmitted}</p>
          </div>
          <div className="rounded-lg border border-jarvis-border bg-jarvis-bg/60 p-3 text-center">
            <p className="text-[11px] text-jarvis-text-secondary">今日执行</p>
            <p className="font-mono text-xl font-semibold text-jarvis-text">{behavior.todayExecuted}</p>
          </div>
          <div
            className={clsx(
              "rounded-lg border p-3 text-center",
              behavior.todayLossStreak >= 2
                ? "border-jarvis-red/60 bg-jarvis-red/10"
                : "border-jarvis-border bg-jarvis-bg/60",
            )}
          >
            <p className="text-[11px] text-jarvis-text-secondary">今日连亏</p>
            <p
              className={clsx(
                "font-mono text-xl font-semibold",
                behavior.todayLossStreak >= 2 ? "text-jarvis-red" : "text-jarvis-text",
              )}
            >
              {behavior.todayLossStreak}
            </p>
          </div>
        </div>
        {behavior.todayLossStreak >= 2 && (
          <div className="mt-2 rounded-lg border border-jarvis-red/50 bg-jarvis-red/15 px-3 py-2 text-center text-sm font-bold text-jarvis-red">
            已连亏 {behavior.todayLossStreak} 笔——今天建议收手，明天再来
          </div>
        )}

        {/* 分时段胜率 */}
        <div className="mt-3">
          <p className="mb-1.5 text-[11px] text-jarvis-text-secondary">
            分时段胜率（按开单时刻）——看看自己哪个时段是重灾区
          </p>
          <div className="space-y-1.5">
            {behavior.buckets.map((b) => (
              <div key={b.key} className="flex items-center gap-2">
                <span className="w-16 shrink-0 text-[11px] text-jarvis-text-secondary">{b.label}</span>
                <div className="h-3.5 flex-1 overflow-hidden rounded bg-jarvis-bg">
                  {b.winRate != null && (
                    <div
                      className={clsx("h-full rounded", b.winRate >= 50 ? "bg-jarvis-green/70" : "bg-jarvis-red/70")}
                      style={{ width: `${Math.max(b.winRate, 4)}%` }}
                    />
                  )}
                </div>
                <span className="w-20 shrink-0 text-right font-mono text-[11px] text-jarvis-text-secondary">
                  {b.winRate != null ? `${b.winRate}% · ${b.trades}笔` : "无成交"}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* 情绪高分 vs 低分 */}
        <div className="mt-3 grid grid-cols-2 gap-3">
          {[
            { label: "冲动开的单（情绪≥4分）", d: behavior.emotionHigh, danger: true },
            { label: "平静开的单（情绪≤3分）", d: behavior.emotionLow, danger: false },
          ].map(({ label, d, danger }) => (
            <div
              key={label}
              className={clsx(
                "rounded-lg border p-3 text-center",
                danger ? "border-jarvis-red/30 bg-jarvis-red/5" : "border-jarvis-green/30 bg-jarvis-green/5",
              )}
            >
              <p className="text-[11px] text-jarvis-text-secondary">{label}</p>
              {d.trades >= 3 ? (
                <p className="mt-1 font-mono text-sm text-jarvis-text">
                  胜率 {d.winRate}% · <span className={pnlCls(d.pnl)}>{d.pnl > 0 ? "+" : ""}{d.pnl}%</span>
                  <span className="ml-1 text-[10px] text-jarvis-text-secondary">（{d.trades}笔）</span>
                </p>
              ) : (
                <p className="mt-1 text-xs text-jarvis-text-secondary">数据积累中（{d.trades}/3 笔）</p>
              )}
            </div>
          ))}
        </div>
      </div>

      <p className="mt-3 text-xs text-jarvis-text-secondary">
        红灯单胜率持续低于绿灯单，就是导师值得信的证据；反过来也一样——这里不讲道理，只看账。
      </p>
    </div>
  );
}

/** 结果回填弹窗 */
function OutcomeDialog({
  plan,
  onClose,
  onSaved,
}: {
  plan: MentorPlanRow;
  onClose: () => void;
  onSaved: () => void;
}) {
  const intent = recallIntent(plan.id);
  const [result, setResult] = useState<MentorOutcomeResult>(
    intent?.action === "skipped" ? "skipped" : "win",
  );
  const [pnlPct, setPnlPct] = useState("");
  const [followed, setFollowed] = useState(intent?.followed ?? plan.verdict?.light !== "red");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setSaving(true);
    setErr(null);
    const payload: MentorOutcomeInput = {
      result,
      followed,
      ...(result !== "skipped" && pnlPct !== "" ? { pnl_pct: Number(pnlPct) } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
    };
    const r = await mentorApi.recordOutcome(plan.id, payload);
    setSaving(false);
    if (r.ok) {
      onSaved();
      onClose();
    } else {
      setErr(r.error ?? "保存失败");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="card w-[420px]" onClick={(e) => e.stopPropagation()}>
        <h3 className="mb-3 text-base font-semibold text-jarvis-text">
          回填结果 · {plan.symbol} {plan.direction === "long" ? "多" : "空"}
        </h3>

        <span className="text-xs text-jarvis-text-secondary">这单最后怎么样了？</span>
        <div className="mb-3 mt-1.5 grid grid-cols-4 gap-2">
          {(Object.keys(RESULT_CN) as MentorOutcomeResult[]).map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setResult(r)}
              className={clsx(
                "rounded-lg border py-1.5 text-sm transition-colors",
                result === r
                  ? r === "win"
                    ? "border-jarvis-green bg-jarvis-green/15 text-jarvis-green"
                    : r === "loss"
                      ? "border-jarvis-red bg-jarvis-red/15 text-jarvis-red"
                      : "border-jarvis-blue bg-jarvis-blue/15 text-jarvis-blue"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              {RESULT_CN[r]}
            </button>
          ))}
        </div>

        {result !== "skipped" && (
          <label className="mb-3 block">
            <span className="text-xs text-jarvis-text-secondary">盈亏 %（正赚负亏，可空）</span>
            <input
              type="number"
              step="any"
              className="mt-1 w-full rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 font-mono text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
              value={pnlPct}
              placeholder="如 +3.2 或 -1.5"
              onChange={(e) => setPnlPct(e.target.value)}
            />
          </label>
        )}

        <label className="mb-3 flex items-center gap-2 text-sm text-jarvis-text">
          <input
            type="checkbox"
            checked={followed}
            onChange={(e) => setFollowed(e.target.checked)}
            className="h-4 w-4 accent-jarvis-blue"
          />
          这单听了导师建议
          <span className="text-xs text-jarvis-text-secondary">（红灯仍执行 = 没听）</span>
        </label>

        <label className="mb-4 block">
          <span className="text-xs text-jarvis-text-secondary">备注（可空）</span>
          <input
            className="mt-1 w-full rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
            value={note}
            placeholder="如：止损被扫后按原方向重进，第二次拿住了"
            onChange={(e) => setNote(e.target.value)}
          />
        </label>

        {err && <p className="mb-2 text-xs text-jarvis-red">{err}</p>}
        <div className="grid grid-cols-2 gap-2">
          <button type="button" onClick={onClose} className="rounded-lg border border-jarvis-border py-2 text-sm text-jarvis-text-secondary hover:text-jarvis-text">
            取消
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={save}
            className="rounded-lg bg-jarvis-blue py-2 text-sm font-medium text-jarvis-accent-fg hover:bg-jarvis-blue/80"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function LedgerPage() {
  const { symbol, supported } = useSymbol();
  const [filterSymbol, setFilterSymbol] = useState<string>("");
  const [days, setDays] = useState(30);
  const [refreshKey, setRefreshKey] = useState(0);
  const [editing, setEditing] = useState<MentorPlanRow | null>(null);

  const { data, loading, refetch } = useApi(
    () => mentorApi.plans(filterSymbol || undefined, days),
    [filterSymbol, days, refreshKey],
  );
  const rows = useMemo(() => data?.rows ?? [], [data]);

  function bumpRefresh() {
    setRefreshKey((k) => k + 1);
  }

  return (
    <div>
      <TrustBoard mock={Boolean(data?.mock)} refreshKey={refreshKey} rows={rows} />

      {/* 筛选条 */}
      <div className="mb-3 flex items-center gap-2">
        <select
          className="rounded-lg border border-jarvis-border bg-jarvis-card px-3 py-1.5 text-sm text-jarvis-text outline-none"
          value={filterSymbol}
          onChange={(e) => setFilterSymbol(e.target.value)}
        >
          <option value="">全部币种</option>
          {supported.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
              {s.value === symbol ? "（当前）" : ""}
            </option>
          ))}
        </select>
        <select
          className="rounded-lg border border-jarvis-border bg-jarvis-card px-3 py-1.5 text-sm text-jarvis-text outline-none"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          <option value={7}>近 7 天</option>
          <option value={30}>近 30 天</option>
          <option value={90}>近 90 天</option>
        </select>
        <button
          type="button"
          onClick={() => refetch()}
          className="flex items-center gap-1 rounded-lg border border-jarvis-border px-3 py-1.5 text-sm text-jarvis-text-secondary hover:text-jarvis-text"
        >
          <RefreshCw size={13} className={clsx(loading && "animate-spin")} /> 刷新
        </button>
        <span className="ml-auto text-xs text-jarvis-text-secondary">{rows.length} 条计划</span>
      </div>

      {/* 计划列表 */}
      <div className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-jarvis-border text-left text-xs text-jarvis-text-secondary">
              <th className="px-4 py-2.5 font-medium">裁决</th>
              <th className="px-2 py-2.5 font-medium">时间</th>
              <th className="px-2 py-2.5 font-medium">币种/方向</th>
              <th className="px-2 py-2.5 font-medium">点位（入场/止损/止盈）</th>
              <th className="px-2 py-2.5 font-medium">本金</th>
              <th className="px-2 py-2.5 font-medium">RR</th>
              <th className="px-2 py-2.5 font-medium">情绪</th>
              <th className="px-2 py-2.5 font-medium">结果</th>
              <th className="px-4 py-2.5 text-right font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-10 text-center text-jarvis-text-secondary">
                  {loading ? "加载中…" : "还没有计划记录——去「写计划」提交第一单，让导师开始攒证据。"}
                </td>
              </tr>
            )}
            {rows.map((r) => {
              // R1 热修：后端 light 值防御——非法值不进 LIGHT_CN/LIGHT_DOT 查表（避免 undefined 崩溃）
              const rawLight = r.verdict?.light;
              const light: MentorLight | undefined =
                rawLight === "green" || rawLight === "yellow" || rawLight === "red" ? rawLight : undefined;
              const rr = calcRR(r.direction, Number(r.entry), Number(r.stop_loss), Number(r.take_profit));
              const intent = !r.outcome ? recallIntent(r.id) : null;
              const principalN = Number(r.principal);
              return (
                <tr key={String(r.id)} className="border-b border-jarvis-border/50 hover:bg-white/[0.02]">
                  <td className="px-4 py-2.5">
                    {light ? (
                      <span className="flex items-center gap-1.5 whitespace-nowrap text-xs">
                        <span className={clsx("h-2.5 w-2.5 rounded-full", LIGHT_DOT[light])} />
                        {LIGHT_CN[light].slice(0, 2)}
                        <span className="font-mono text-jarvis-text-secondary">{r.verdict?.score}</span>
                      </span>
                    ) : (
                      <span className="text-xs text-jarvis-text-secondary">—</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2.5 font-mono text-xs text-jarvis-text-secondary">
                    {(r.created_at ?? "").slice(5, 16) || "—"}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2.5">
                    <span className="text-jarvis-text">{r.symbol.replace(/USDT$/, "")}</span>{" "}
                    <span className={r.direction === "long" ? "text-jarvis-green" : "text-jarvis-red"}>
                      {r.direction === "long" ? "多" : "空"}
                    </span>
                    {r.leverage ? (
                      <span className="ml-1 font-mono text-xs text-jarvis-text-secondary">{r.leverage}x</span>
                    ) : null}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2.5 font-mono text-xs text-jarvis-text-secondary">
                    {Number(r.entry)} / {Number(r.stop_loss)} / {Number(r.take_profit)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-2.5 font-mono text-xs text-jarvis-text-secondary">
                    {Number.isFinite(principalN) && principalN > 0 ? `${principalN}U` : "—"}
                  </td>
                  <td className={clsx("px-2 py-2.5 font-mono text-xs", rr != null && rr < 1.5 ? "text-jarvis-red" : "text-jarvis-text")}>
                    {rr != null ? rr.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-2.5">
                    <span className={clsx("font-mono text-xs", (r.emotion_score ?? 0) >= 4 ? "text-jarvis-red" : "text-jarvis-text-secondary")}>
                      {r.emotion_score ?? "—"}/5
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-2.5 text-xs">
                    {r.outcome ? (
                      <span
                        className={clsx(
                          r.outcome.result === "win" && "text-jarvis-green",
                          r.outcome.result === "loss" && "text-jarvis-red",
                          (r.outcome.result === "scratch" || r.outcome.result === "skipped") &&
                            "text-jarvis-text-secondary",
                        )}
                      >
                        {RESULT_CN[r.outcome.result]}
                        {r.outcome.pnl_pct != null && (
                          <span className="ml-1 font-mono">
                            {Number(r.outcome.pnl_pct) > 0 ? "+" : ""}
                            {Number(r.outcome.pnl_pct)}%
                          </span>
                        )}
                        {!r.outcome.followed && <span className="ml-1 text-jarvis-red">未听</span>}
                      </span>
                    ) : intent ? (
                      <span className="text-jarvis-yellow">
                        {intent.action === "skipped" ? "已放弃待确认" : "已执行待回填"}
                      </span>
                    ) : (
                      <span className="text-jarvis-text-secondary">待回填</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      type="button"
                      onClick={() => setEditing(r)}
                      className="inline-flex items-center gap-1 rounded-md border border-jarvis-border px-2 py-1 text-xs text-jarvis-text-secondary transition-colors hover:border-jarvis-blue hover:text-jarvis-blue"
                    >
                      <PenLine size={12} />
                      {r.outcome ? "改结果" : "回填结果"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {editing && (
        <OutcomeDialog
          plan={editing}
          onClose={() => setEditing(null)}
          onSaved={bumpRefresh}
        />
      )}
    </div>
  );
}
