import { useState } from "react";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api } from "@/api/client";
import PositionCard from "@/components/cards/PositionCard";
import { ArrowLeftRight, ShieldCheck, AlertTriangle, Zap, FileText, Radar, Play, RotateCw, TrendingUp } from "lucide-react";
import { clsx } from "clsx";

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2 });
}

export default function Trading() {
  const { symbol } = useSymbol();
  const { data: positions } = usePolling(api.positions, 3_000);
  const { data: orders } = usePolling(api.orders, 15_000);
  const { data: wallet } = usePolling(api.wallet, 30_000);
  const { data: traderStatus } = usePolling(api.traderStatus, 15_000);

  const pos = (positions ?? []) as Record<string, unknown>[];
  const ord = (orders ?? []) as Record<string, unknown>[];
  const w = wallet as Record<string, number> | null;
  const status = traderStatus as Record<string, unknown> | null;

  const [orderForm, setOrderForm] = useState({
    direction: "long" as "long" | "short",
    amount: "",
    stopLoss: "1.5",
    takeProfit: "3.0",
  });
  const [ordering, setOrdering] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<{ type: string; ok: boolean; msg: string } | null>(null);

  const handleOrder = async () => {
    if (!orderForm.amount) return;
    setOrdering(true);
    try {
      await api.post("/orders/place", {
        symbol,
        direction: orderForm.direction,
        amount: parseFloat(orderForm.amount),
        stop_loss_pct: parseFloat(orderForm.stopLoss),
        take_profit_pct: parseFloat(orderForm.takeProfit),
      });
    } catch {
      // API 可能未就绪
    }
    setOrdering(false);
  };

  const runAction = async (name: string, fn: () => Promise<Record<string, unknown>>) => {
    setActionLoading(name);
    setActionResult(null);
    try {
      const res = await fn();
      const ok = Boolean(res?.ok);
      const data = res?.data as Record<string, unknown> | undefined;
      let msg = "";
      if (name === "brief") {
        const dec = data?.decision as Record<string, unknown> | undefined;
        msg = dec
          ? `${dec.direction} | 信心 ${dec.conviction_score} | 仓位 ${dec.suggested_position_pct}%`
          : (res?.error as string) ?? "无数据";
      } else if (name === "execute") {
        const g = data?.guardrails as Record<string, unknown> | undefined;
        msg = g
          ? `${g.action === "place" ? "可下单" : "跳过"} — ${g.reason}`
          : (res?.error as string) ?? "无数据";
      } else if (name === "radar") {
        const act = data?.actionable as unknown[] | undefined;
        const total = (data?.results as unknown[])?.length ?? 0;
        msg = `扫描 ${total} 币，达标信号 ${act?.length ?? 0} 个`;
      } else if (name === "cycle") {
        const opened = (data?.opened as unknown[])?.length ?? 0;
        const closed = (data?.closed as unknown[])?.length ?? 0;
        msg = `开仓 ${opened} | 平仓 ${closed}`;
      } else if (name === "open") {
        msg = data?.action === "opened"
          ? `已开仓 #${data.position_id} ${data.side === "sell" ? "做空" : "做多"}`
          : `${data?.reason ?? (res?.error as string) ?? "无数据"}`;
      }
      setActionResult({ type: name, ok, msg });
    } catch (e) {
      setActionResult({ type: name, ok: false, msg: e instanceof Error ? e.message : "请求失败" });
    } finally {
      setActionLoading(null);
    }
  };

  const dailyLoss = Number(status?.daily_pnl ?? 0);
  const dailyLimit = -0.02;
  const dailyProgress = Math.min(1, Math.abs(dailyLoss / dailyLimit));
  const positionCount = pos.length;
  const maxPositions = 3;
  const posProgress = positionCount / maxPositions;

  return (
    <div className="space-y-6">
      <h1 className="page-title flex items-center gap-2">
        <ArrowLeftRight size={22} />
        交易中心
      </h1>

      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-2 space-y-4">
          <div className="card">
            <p className="stat-label mb-4">活跃持仓</p>
            {pos.length > 0 ? (
              <div className="grid grid-cols-2 gap-3">
                {pos.map((p, i) => (
                  <PositionCard
                    key={`${String(p.symbol ?? "—")}-${i}`}
                    symbol={String(p.symbol ?? "—")}
                    direction={
                      String(p.direction ?? "long") === "short"
                        ? "short"
                        : "long"
                    }
                    entryPrice={Number(p.entry_price ?? 0)}
                    currentPrice={
                      p.current_price != null
                        ? Number(p.current_price)
                        : undefined
                    }
                    pnlPct={
                      p.pnl_pct != null ? Number(p.pnl_pct) : undefined
                    }
                    stopLoss={p.stop_loss ? Number(p.stop_loss) : undefined}
                    takeProfit={
                      p.take_profit ? Number(p.take_profit) : undefined
                    }
                  />
                ))}
              </div>
            ) : (
              <p className="text-jarvis-text-secondary text-sm">暂无活跃持仓</p>
            )}
          </div>

          <div className="card">
            <p className="stat-label mb-4">历史订单</p>
            {ord.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-jarvis-text-secondary border-b border-jarvis-border">
                      <th className="text-left py-2 font-medium">时间</th>
                      <th className="text-left py-2 font-medium">币种</th>
                      <th className="text-left py-2 font-medium">方向</th>
                      <th className="text-right py-2 font-medium">金额</th>
                      <th className="text-right py-2 font-medium">收益</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ord.slice(-10).reverse().map((o, i) => {
                      const pnl = Number((o as Record<string, number>).pnl ?? 0);
                      return (
                        <tr
                          key={i}
                          className="border-b border-jarvis-border/50 last:border-0"
                        >
                          <td className="py-2 text-jarvis-text-secondary font-mono">
                            {String((o as Record<string, string>).time ?? "—")}
                          </td>
                          <td className="py-2 text-jarvis-text">
                            {String((o as Record<string, string>).symbol ?? "—")}
                          </td>
                          <td className="py-2">
                            <span
                              className={clsx(
                                "text-xs px-2 py-0.5 rounded-full",
                                String((o as Record<string, string>).direction) === "short"
                                  ? "bg-jarvis-red/15 text-jarvis-red"
                                  : "bg-jarvis-green/15 text-jarvis-green",
                              )}
                            >
                              {String((o as Record<string, string>).direction) === "short" ? "空" : "多"}
                            </span>
                          </td>
                          <td className="py-2 text-right text-jarvis-text font-mono">
                            ${fmtUsd(Number((o as Record<string, number>).amount ?? 0))}
                          </td>
                          <td
                            className={clsx("py-2 text-right font-mono", {
                              "text-jarvis-green": pnl >= 0,
                              "text-jarvis-red": pnl < 0,
                            })}
                          >
                            {pnl >= 0 ? "+" : ""}${fmtUsd(pnl)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-jarvis-text-secondary text-sm">暂无订单记录</p>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="card">
            <div className="flex items-center gap-2 mb-4">
              <ShieldCheck size={16} className="text-jarvis-blue" />
              <p className="stat-label">风控面板</p>
            </div>

            <div className="space-y-4">
              <div>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-jarvis-text-secondary">日亏进度</span>
                  <span
                    className={clsx("font-mono", {
                      "text-jarvis-green": dailyProgress < 0.5,
                      "text-jarvis-yellow": dailyProgress >= 0.5 && dailyProgress < 0.8,
                      "text-jarvis-red": dailyProgress >= 0.8,
                    })}
                  >
                    {(dailyLoss * 100).toFixed(1)}% / {(dailyLimit * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="h-2 bg-jarvis-bg rounded-full overflow-hidden">
                  <div
                    className={clsx("h-full rounded-full transition-all", {
                      "bg-jarvis-green": dailyProgress < 0.5,
                      "bg-jarvis-yellow": dailyProgress >= 0.5 && dailyProgress < 0.8,
                      "bg-jarvis-red": dailyProgress >= 0.8,
                    })}
                    style={{ width: `${dailyProgress * 100}%` }}
                  />
                </div>
              </div>

              <div>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-jarvis-text-secondary">仓位使用</span>
                  <span className="text-jarvis-text font-mono">
                    {positionCount}/{maxPositions}
                  </span>
                </div>
                <div className="h-2 bg-jarvis-bg rounded-full overflow-hidden">
                  <div
                    className="h-full bg-jarvis-blue rounded-full transition-all"
                    style={{ width: `${posProgress * 100}%` }}
                  />
                </div>
              </div>

              <div className="flex items-center gap-2 text-sm">
                <span className="text-jarvis-text-secondary">熔断状态</span>
                {dailyProgress < 0.8 ? (
                  <span className="flex items-center gap-1 text-jarvis-green">
                    <span className="w-2 h-2 bg-jarvis-green rounded-full" />
                    正常
                  </span>
                ) : (
                  <span className="flex items-center gap-1 text-jarvis-yellow">
                    <AlertTriangle size={14} />
                    接近限额
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="card">
            <p className="stat-label mb-4">手动下单（模拟盘）</p>
            <div className="space-y-3">
              <div className="flex gap-2">
                <button
                  onClick={() =>
                    setOrderForm((f) => ({ ...f, direction: "long" }))
                  }
                  className={clsx(
                    "flex-1 py-2 text-sm rounded-lg font-medium transition-colors",
                    orderForm.direction === "long"
                      ? "bg-jarvis-green text-white"
                      : "bg-jarvis-bg text-jarvis-text-secondary hover:text-jarvis-text",
                  )}
                >
                  做多
                </button>
                <button
                  onClick={() =>
                    setOrderForm((f) => ({ ...f, direction: "short" }))
                  }
                  className={clsx(
                    "flex-1 py-2 text-sm rounded-lg font-medium transition-colors",
                    orderForm.direction === "short"
                      ? "bg-jarvis-red text-white"
                      : "bg-jarvis-bg text-jarvis-text-secondary hover:text-jarvis-text",
                  )}
                >
                  做空
                </button>
              </div>

              <div>
                <label className="text-xs text-jarvis-text-secondary">
                  数量 (USDT)
                </label>
                <input
                  type="number"
                  value={orderForm.amount}
                  onChange={(e) =>
                    setOrderForm((f) => ({ ...f, amount: e.target.value }))
                  }
                  placeholder="100"
                  className="w-full mt-1 px-3 py-2 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text font-mono"
                />
              </div>

              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="text-xs text-jarvis-text-secondary">
                    止损 %
                  </label>
                  <input
                    type="number"
                    value={orderForm.stopLoss}
                    onChange={(e) =>
                      setOrderForm((f) => ({
                        ...f,
                        stopLoss: e.target.value,
                      }))
                    }
                    className="w-full mt-1 px-3 py-2 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text font-mono"
                  />
                </div>
                <div>
                  <label className="text-xs text-jarvis-text-secondary">
                    止盈 %
                  </label>
                  <input
                    type="number"
                    value={orderForm.takeProfit}
                    onChange={(e) =>
                      setOrderForm((f) => ({
                        ...f,
                        takeProfit: e.target.value,
                      }))
                    }
                    className="w-full mt-1 px-3 py-2 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text font-mono"
                  />
                </div>
              </div>

              <button
                onClick={handleOrder}
                disabled={ordering || !orderForm.amount}
                className={clsx(
                  "w-full py-2.5 rounded-lg font-medium text-white transition-colors",
                  orderForm.direction === "long"
                    ? "bg-jarvis-green hover:bg-jarvis-green/80"
                    : "bg-jarvis-red hover:bg-jarvis-red/80",
                  (ordering || !orderForm.amount) && "opacity-50 cursor-not-allowed",
                )}
              >
                {ordering
                  ? "下单中..."
                  : `确认${orderForm.direction === "long" ? "做多" : "做空"}`}
              </button>
            </div>
          </div>

          <div className="card">
            <div className="flex items-center gap-2 mb-4">
              <Zap size={16} className="text-jarvis-yellow" />
              <p className="stat-label">快捷操作</p>
            </div>
            <div className="space-y-2">
              <button
                onClick={() => runAction("brief", () => api.actionBrief(symbol))}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-blue transition-colors disabled:opacity-50"
              >
                <FileText size={14} className="text-jarvis-blue" />
                {actionLoading === "brief" ? "生成中..." : "生成决策简报"}
              </button>
              <button
                onClick={() => runAction("execute", () => api.actionExecute(symbol, true))}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-blue transition-colors disabled:opacity-50"
              >
                <Play size={14} className="text-jarvis-green" />
                {actionLoading === "execute" ? "演练中..." : "执行手演练 (dry-run)"}
              </button>
              <button
                onClick={() => runAction("radar", () => api.actionRadar())}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-purple transition-colors disabled:opacity-50"
              >
                <Radar size={14} className="text-jarvis-purple" />
                {actionLoading === "radar" ? "扫描中..." : "雷达扫描全币种"}
              </button>
              <button
                onClick={() => runAction("open", () => api.actionOpen(symbol, false))}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-green transition-colors disabled:opacity-50"
              >
                <TrendingUp size={14} className="text-jarvis-green" />
                {actionLoading === "open" ? "开仓中..." : "按决策自动开仓"}
              </button>
              <button
                onClick={() => runAction("cycle", () => api.traderCycle("BTC,ETH,SOL", false))}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-yellow transition-colors disabled:opacity-50"
              >
                <RotateCw size={14} className="text-jarvis-yellow" />
                {actionLoading === "cycle" ? "执行中..." : "跑一轮自动跟盘"}
              </button>
            </div>
            {actionResult && (
              <div className={clsx(
                "mt-3 p-2.5 rounded-lg text-xs",
                actionResult.ok ? "bg-jarvis-green/10 text-jarvis-green" : "bg-jarvis-red/10 text-jarvis-red",
              )}>
                {actionResult.msg}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
