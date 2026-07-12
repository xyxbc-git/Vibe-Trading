import { useMemo, useState } from "react";
import {
  Scale,
  AlertTriangle,
  RefreshCw,
  Loader2,
  FlaskConical,
  TrendingUp,
  Clock,
  X,
} from "lucide-react";
import {
  api,
  type FundingArbOpportunity,
  type FundingArbPosition,
} from "@/api/client";
import { usePolling } from "@/hooks/useApi";

/** 费率上色：正=绿（空头收钱），负=红（空头倒贴） */
function rateColor(rate: number | null | undefined): string {
  if (rate == null) return "text-jarvis-text-secondary";
  if (rate > 0) return "text-jarvis-green";
  if (rate < 0) return "text-jarvis-red";
  return "text-jarvis-text-secondary";
}

function pnlColor(v: number | null | undefined): string {
  if (v == null) return "text-jarvis-text-secondary";
  if (v > 0) return "text-jarvis-green";
  if (v < 0) return "text-jarvis-red";
  return "text-jarvis-text";
}

function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "--";
  return `${v >= 0 ? "" : "-"}$${Math.abs(v).toFixed(digits)}`;
}

function fmtCountdown(unixSec: number | null | undefined): string {
  if (!unixSec) return "--";
  const diff = unixSec - Date.now() / 1000;
  if (diff <= 0) return "结算中";
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return h > 0 ? `${h}h${m}m` : `${m}m`;
}

/** 一键模拟建仓对话框（极简：本金输入 + 确认） */
function OpenDialog({
  opp,
  onClose,
  onDone,
}: {
  opp: FundingArbOpportunity;
  onClose: () => void;
  onDone: (msg: string) => void;
}) {
  const [capital, setCapital] = useState("1000");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const capNum = Number(capital);
  const valid = Number.isFinite(capNum) && capNum >= 10;

  const submit = async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.fundingArbOpen(opp.symbol, capNum);
      if (!res.ok) {
        setError(res.error ?? "建仓失败");
        return;
      }
      onDone(
        `已建仓 #${res.position_id} ${opp.symbol}：现货多 ${res.qty?.toFixed(6)} @ ${res.spot_entry} + 永续空 @ ${res.perp_entry}` +
          (res.warning ? `（${res.warning}）` : ""),
      );
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-[380px] rounded-xl bg-jarvis-card border border-jarvis-border p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <FlaskConical size={15} className="text-jarvis-blue" />
            模拟建仓 · {opp.symbol}
          </h3>
          <button onClick={onClose} className="text-jarvis-text-secondary hover:text-jarvis-text">
            <X size={16} />
          </button>
        </div>

        <div className="text-xs text-jarvis-text-secondary space-y-1 mb-4">
          <p>
            当期费率 <span className={rateColor(opp.funding_rate)}>{opp.funding_rate_pct.toFixed(4)}%</span>
            ，年化 <span className={rateColor(opp.apr_now)}>{opp.apr_now.toFixed(1)}%</span>
            {opp.apr_7d != null && <>（7日均年化 {opp.apr_7d.toFixed(1)}%）</>}
          </p>
          <p>本金对半劈两腿：现货买入 + 永续 1x 做空（市场中性，不赌方向）</p>
          {opp.warning && (
            <p className="text-jarvis-yellow flex items-start gap-1">
              <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
              {opp.warning}
            </p>
          )}
        </div>

        <label className="block text-xs text-jarvis-text-secondary mb-1">模拟本金（USDT，≥10）</label>
        <input
          value={capital}
          onChange={(e) => setCapital(e.target.value)}
          inputMode="decimal"
          className="w-full px-3 py-2 rounded-lg bg-jarvis-bg border border-jarvis-border text-sm
                     focus:border-jarvis-blue focus:outline-none"
          placeholder="1000"
        />
        {error && <p className="text-xs text-jarvis-red mt-2">{error}</p>}

        <button
          onClick={submit}
          disabled={!valid || submitting}
          className="mt-4 w-full py-2 rounded-lg bg-jarvis-blue/20 border border-jarvis-blue/50 text-jarvis-blue
                     text-sm font-medium hover:bg-jarvis-blue/30 transition-colors disabled:opacity-40"
        >
          {submitting ? "建仓中..." : "确认模拟建仓"}
        </button>
      </div>
    </div>
  );
}

export default function FundingArb() {
  // 后端机会列表自带 120s 缓存；持仓查询会触发懒结算，60s 轮询足够
  const opps = usePolling(() => api.fundingArbOpportunities(), 60_000);
  const positions = usePolling(() => api.fundingArbPositions("all"), 60_000);
  const pnl = usePolling(() => api.fundingArbPnl(), 60_000);

  const [dialogOpp, setDialogOpp] = useState<FundingArbOpportunity | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [closingId, setClosingId] = useState<number | null>(null);

  const openRows = useMemo(
    () => (positions.data?.positions ?? []).filter((p) => p.status === "open"),
    [positions.data],
  );
  const closedRows = useMemo(
    () => (positions.data?.positions ?? []).filter((p) => p.status === "closed"),
    [positions.data],
  );

  const refreshAll = () => {
    opps.refetch();
    positions.refetch();
    pnl.refetch();
  };

  const doClose = async (pos: FundingArbPosition) => {
    if (closingId != null) return;
    if (!window.confirm(`确认平仓 #${pos.id} ${pos.symbol}？两腿同时按当前价平出。`)) return;
    setClosingId(pos.id);
    try {
      const res = await api.fundingArbClose(pos.id);
      setToast(
        res.ok
          ? `已平仓 #${pos.id}：费率收益 ${fmtUsd(res.funding_accrued_usdt)} + 基差 ${fmtUsd(res.basis_pnl_usdt)} - 手续费 ${fmtUsd(res.fees_usdt)} = 总盈亏 ${fmtUsd(res.total_pnl_usdt)}`
          : `平仓失败：${res.error ?? "未知错误"}`,
      );
      refreshAll();
    } catch (e) {
      setToast(`平仓失败：${(e as Error).message}`);
    } finally {
      setClosingId(null);
    }
  };

  const oppLoading = opps.loading && !opps.data;

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Scale size={22} />
        费率套利
        <span
          className="text-[10px] px-1.5 py-0.5 rounded border font-medium
                     bg-jarvis-yellow/10 text-jarvis-yellow border-jarvis-yellow/40"
          title="全部为模拟持仓：费率按交易所真实历史结算，但未建模滑点/借币成本"
        >
          模拟盘
        </span>
      </h1>

      <p className="text-xs text-jarvis-text-secondary mb-4 -mt-2">
        现货多头 + 永续空头等量对冲（delta 中性），不赌方向、赚正资金费率；费率每 8 小时结算一次。
        概率与收益为统计参考，非投资建议。
      </p>

      {toast && (
        <div className="mb-4 flex items-center justify-between gap-3 p-3 rounded-lg bg-jarvis-blue/10 border border-jarvis-blue/40 text-sm">
          <span className="break-all">{toast}</span>
          <button onClick={() => setToast(null)} className="text-jarvis-text-secondary hover:text-jarvis-text flex-shrink-0">
            <X size={14} />
          </button>
        </div>
      )}

      {/* 收益总览 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
        <div className="card p-4">
          <p className="text-xs text-jarvis-text-secondary">持仓中本金</p>
          <p className="text-lg font-semibold mt-1">{fmtUsd(pnl.data?.open?.capital_usdt)}</p>
          <p className="text-[10px] text-jarvis-text-secondary mt-0.5">{pnl.data?.open?.count ?? 0} 笔持仓</p>
        </div>
        <div className="card p-4">
          <p className="text-xs text-jarvis-text-secondary">持仓累计费率收益</p>
          <p className={`text-lg font-semibold mt-1 ${pnlColor(pnl.data?.open?.funding_accrued_usdt)}`}>
            {fmtUsd(pnl.data?.open?.funding_accrued_usdt, 4)}
          </p>
          <p className="text-[10px] text-jarvis-text-secondary mt-0.5">
            年化 {pnl.data?.open?.funding_apr_pct != null ? `${pnl.data.open.funding_apr_pct.toFixed(2)}%` : "--"}
          </p>
        </div>
        <div className="card p-4">
          <p className="text-xs text-jarvis-text-secondary">已平仓总盈亏</p>
          <p className={`text-lg font-semibold mt-1 ${pnlColor(pnl.data?.closed?.total_pnl_usdt)}`}>
            {fmtUsd(pnl.data?.closed?.total_pnl_usdt, 4)}
          </p>
          <p className="text-[10px] text-jarvis-text-secondary mt-0.5">{pnl.data?.closed?.count ?? 0} 笔已平</p>
        </div>
        <div className="card p-4">
          <p className="text-xs text-jarvis-text-secondary">历史累计费率收益</p>
          <p className={`text-lg font-semibold mt-1 ${pnlColor(pnl.data?.all_time_funding_usdt)}`}>
            {fmtUsd(pnl.data?.all_time_funding_usdt, 4)}
          </p>
          <p className="text-[10px] text-jarvis-text-secondary mt-0.5">开仓至今全部结算期</p>
        </div>
      </div>

      {/* 机会列表 */}
      <div className="card mb-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold flex items-center gap-2">
            <TrendingUp size={15} className="text-jarvis-green" />
            套利机会（按当期年化降序）
          </h2>
          <button
            onClick={refreshAll}
            disabled={opps.loading}
            className="flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-lg border border-jarvis-border
                       text-jarvis-text-secondary hover:text-jarvis-text transition-colors disabled:opacity-50"
          >
            <RefreshCw size={12} className={opps.loading ? "animate-spin" : ""} />
            刷新
          </button>
        </div>

        {oppLoading ? (
          <div className="h-32 flex items-center justify-center gap-2 text-jarvis-text-secondary text-sm">
            <Loader2 size={15} className="animate-spin" />
            正在拉取全市场资金费率...
          </div>
        ) : opps.error || !opps.data?.ok ? (
          <p className="text-sm text-jarvis-text-secondary py-6 text-center">
            机会接口未就绪{opps.data?.error ? `：${opps.data.error}` : opps.error ? `：${opps.error}` : ""}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-jarvis-text-secondary border-b border-jarvis-border">
                  <th className="text-left py-2 pr-3 font-medium">币种</th>
                  <th className="text-right py-2 px-3 font-medium">标记价</th>
                  <th className="text-right py-2 px-3 font-medium">当期费率</th>
                  <th className="text-right py-2 px-3 font-medium">年化(当期)</th>
                  <th className="text-right py-2 px-3 font-medium">年化(7日均)</th>
                  <th className="text-right py-2 px-3 font-medium">回本天数</th>
                  <th className="text-right py-2 px-3 font-medium">下次结算</th>
                  <th className="text-right py-2 pl-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {(opps.data.opportunities ?? []).map((o) => (
                  <tr key={o.symbol} className="border-b border-jarvis-border/40 hover:bg-white/[0.02]">
                    <td className="py-2.5 pr-3 font-medium">
                      <span className="flex items-center gap-1.5">
                        {o.symbol.replace("USDT", "")}
                        {o.warning && (
                          <AlertTriangle size={12} className="text-jarvis-yellow flex-shrink-0" aria-label={o.warning} />
                        )}
                      </span>
                    </td>
                    <td className="text-right py-2.5 px-3 tabular-nums">
                      {o.mark_price >= 1 ? o.mark_price.toLocaleString("en-US", { maximumFractionDigits: 2 }) : o.mark_price.toPrecision(4)}
                    </td>
                    <td className={`text-right py-2.5 px-3 tabular-nums ${rateColor(o.funding_rate)}`}>
                      {o.funding_rate_pct.toFixed(4)}%
                    </td>
                    <td className={`text-right py-2.5 px-3 tabular-nums font-medium ${rateColor(o.apr_now)}`}>
                      {o.apr_now.toFixed(1)}%
                    </td>
                    <td className={`text-right py-2.5 px-3 tabular-nums ${rateColor(o.apr_7d)}`}>
                      {o.apr_7d != null ? `${o.apr_7d.toFixed(1)}%` : "--"}
                    </td>
                    <td className="text-right py-2.5 px-3 tabular-nums text-jarvis-text-secondary">
                      {o.break_even_days != null ? `${o.break_even_days}天` : "--"}
                    </td>
                    <td className="text-right py-2.5 px-3 tabular-nums text-jarvis-text-secondary">
                      <span className="inline-flex items-center gap-1">
                        <Clock size={11} />
                        {fmtCountdown(o.next_funding_ts)}
                      </span>
                    </td>
                    <td className="text-right py-2.5 pl-3">
                      <button
                        onClick={() => setDialogOpp(o)}
                        className="px-2.5 py-1 rounded-md border border-jarvis-blue/50 text-jarvis-blue
                                   hover:bg-jarvis-blue/10 transition-colors"
                      >
                        模拟建仓
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[10px] text-jarvis-text-secondary mt-2">{opps.data.basis}</p>
          </div>
        )}
      </div>

      {/* 持仓列表 */}
      <div className="card mb-5">
        <h2 className="text-sm font-semibold mb-3">模拟持仓（{openRows.length}）</h2>
        {openRows.length === 0 ? (
          <p className="text-sm text-jarvis-text-secondary py-4 text-center">
            暂无持仓——从上方机会列表选一个正费率币种模拟建仓
          </p>
        ) : (
          <div className="space-y-3">
            {openRows.map((p) => (
              <div key={p.id} className="rounded-lg border border-jarvis-border/60 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    #{p.id} {p.symbol.replace("USDT", "")}
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-green/10 text-jarvis-green border border-jarvis-green/40">
                      持仓中 {p.held_days?.toFixed(1)}天
                    </span>
                    {p.warning && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-yellow/10 text-jarvis-yellow border border-jarvis-yellow/40 flex items-center gap-1">
                        <AlertTriangle size={10} />
                        {p.warning}
                      </span>
                    )}
                  </div>
                  <button
                    onClick={() => doClose(p)}
                    disabled={closingId === p.id}
                    className="px-2.5 py-1 text-xs rounded-md border border-jarvis-red/50 text-jarvis-red
                               hover:bg-jarvis-red/10 transition-colors disabled:opacity-50"
                  >
                    {closingId === p.id ? "平仓中..." : "平仓"}
                  </button>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1.5 text-xs">
                  <p className="text-jarvis-text-secondary">
                    现货多腿：<span className="text-jarvis-text tabular-nums">{p.qty.toFixed(6)} @ {p.spot_entry}</span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    永续空腿：<span className="text-jarvis-text tabular-nums">{p.qty.toFixed(6)} @ {p.perp_entry}</span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    本金：<span className="text-jarvis-text tabular-nums">{fmtUsd(p.capital_usdt)}</span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    净敞口：<span className="text-jarvis-text tabular-nums">{p.net_exposure_pct?.toFixed(3) ?? "--"}%</span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    累计费率收益：
                    <span className={`tabular-nums ${pnlColor(p.funding_accrued_usdt)}`}>
                      {fmtUsd(p.funding_accrued_usdt, 4)}
                    </span>
                    <span className="text-jarvis-text-secondary/70">（{p.settle_count} 期）</span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    费率年化：
                    <span className={`tabular-nums ${pnlColor(p.funding_apr_pct)}`}>
                      {p.funding_apr_pct != null ? `${p.funding_apr_pct.toFixed(2)}%` : "--"}
                    </span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    当期费率：
                    <span className={`tabular-nums ${rateColor(p.current_funding_rate)}`}>
                      {p.current_funding_rate != null ? `${(p.current_funding_rate * 100).toFixed(4)}%` : "--"}
                    </span>
                  </p>
                  <p className="text-jarvis-text-secondary">
                    下次结算：<span className="text-jarvis-text tabular-nums">{fmtCountdown(p.next_funding_ts)}</span>
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 已平仓记录 */}
      {closedRows.length > 0 && (
        <div className="card">
          <h2 className="text-sm font-semibold mb-3">已平仓（{closedRows.length}）</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-jarvis-text-secondary border-b border-jarvis-border">
                  <th className="text-left py-2 pr-3 font-medium">#</th>
                  <th className="text-left py-2 px-3 font-medium">币种</th>
                  <th className="text-right py-2 px-3 font-medium">本金</th>
                  <th className="text-right py-2 px-3 font-medium">费率收益</th>
                  <th className="text-right py-2 px-3 font-medium">基差损益</th>
                  <th className="text-right py-2 px-3 font-medium">手续费</th>
                  <th className="text-right py-2 px-3 font-medium">总盈亏</th>
                  <th className="text-right py-2 pl-3 font-medium">平仓时间</th>
                </tr>
              </thead>
              <tbody>
                {closedRows.map((p) => (
                  <tr key={p.id} className="border-b border-jarvis-border/40">
                    <td className="py-2 pr-3 text-jarvis-text-secondary">{p.id}</td>
                    <td className="py-2 px-3 font-medium">{p.symbol.replace("USDT", "")}</td>
                    <td className="text-right py-2 px-3 tabular-nums">{fmtUsd(p.capital_usdt)}</td>
                    <td className={`text-right py-2 px-3 tabular-nums ${pnlColor(p.funding_accrued_usdt)}`}>
                      {fmtUsd(p.funding_accrued_usdt, 4)}
                    </td>
                    <td className={`text-right py-2 px-3 tabular-nums ${pnlColor(p.basis_pnl_usdt)}`}>
                      {fmtUsd(p.basis_pnl_usdt, 4)}
                    </td>
                    <td className="text-right py-2 px-3 tabular-nums text-jarvis-text-secondary">
                      {fmtUsd(p.fees_usdt, 4)}
                    </td>
                    <td className={`text-right py-2 px-3 tabular-nums font-medium ${pnlColor(p.total_pnl_usdt)}`}>
                      {fmtUsd(p.total_pnl_usdt, 4)}
                    </td>
                    <td className="text-right py-2 pl-3 text-jarvis-text-secondary">{p.closed_at ?? "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="text-[10px] text-jarvis-text-secondary mt-4">
        {positions.data?.disclaimer ?? "模拟盘：费率按交易所真实历史结算，但未建模滑点/借币成本/保证金波动；收益为统计参考，非投资建议。"}
      </p>

      {dialogOpp && (
        <OpenDialog
          opp={dialogOpp}
          onClose={() => setDialogOpp(null)}
          onDone={(msg) => {
            setToast(msg);
            refreshAll();
          }}
        />
      )}
    </div>
  );
}
