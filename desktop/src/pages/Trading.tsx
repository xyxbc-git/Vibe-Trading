import { useMemo, useState } from "react";
import { useApi, usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api, ApiError } from "@/api/client";
import PositionCard from "@/components/cards/PositionCard";
import OrderNotifyDialog from "@/components/cards/OrderNotifyDialog";
import BehaviorTagDialog from "@/components/cards/BehaviorTagDialog";
import { extractCardMetrics } from "@/lib/positionMetrics";
import { ArrowLeftRight, ShieldCheck, AlertTriangle, Zap, FileText, Radar, Play, RotateCw, TrendingUp, Compass, Bell, Wallet } from "lucide-react";
import { clsx } from "clsx";

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2 });
}

/** 跑一轮 12 系统信号矩阵跟盘（多币多周期拉 K 线较慢，独立 90s 超时） */
async function runTwelveCycle(): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 90_000);
  try {
    const res = await fetch("/api/twelve/cycle", {
      method: "POST",
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
    return res.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error("请求超时（多币种共识计算中，稍后查看交易记录）");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export default function Trading() {
  const { symbol } = useSymbol();
  const { data: positions, refetch: refetchPositions } = usePolling(api.positions, 3_000);
  const { data: orders, refetch: refetchOrders } = usePolling(api.orders, 15_000);
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
    // 邮件提醒（可选）：填邮箱即在挂单成功后为该单开启通知
    notifyEmail: "",
    notifyTp: true,
    notifySl: true,
  });
  const [ordering, setOrdering] = useState(false);
  // 下单结果横幅：成功/失败都要给用户反馈，禁止静默吞错
  const [orderResult, setOrderResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<{ type: string; ok: boolean; msg: string } | null>(null);

  // 订单邮件提醒配置索引（order_id → 配置），铃铛点亮已配置的单
  const { data: notifyConfigs, refetch: refetchNotify } = useApi(api.orderNotifyList);
  const notifyMap = useMemo(() => {
    const m = new Map<string, boolean>();
    for (const c of notifyConfigs ?? []) m.set(c.order_id, true);
    return m;
  }, [notifyConfigs]);
  const [notifyDialog, setNotifyDialog] = useState<{ orderId: string; title: string } | null>(null);

  // ── T1.3 下单前计划确认层（不阻断只提示；开关 trading.plan_confirm_enabled）──
  // 配置读一次（Settings 改配置后重进页面生效）；共识与后端缓存同节奏 3 分钟轮询
  const { data: confirmCfg } = useApi(api.configCenter);
  const planConfirmEnabled = confirmCfg?.groups?.trading?.plan_confirm_enabled !== false;
  const minRrWarning = Number(confirmCfg?.groups?.risk?.min_rr_warning ?? 1.5);
  const { data: consensusData } = usePolling(
    () => api.twelveConsensus(symbol),
    180_000,
    [symbol],
  );

  // 盈亏比 = 止盈% / 止损%（多空皆按距离比口径）；输入非法/未填时不校验
  const rrValue = useMemo(() => {
    const sl = parseFloat(orderForm.stopLoss);
    const tp = parseFloat(orderForm.takeProfit);
    if (!Number.isFinite(sl) || !Number.isFinite(tp) || sl <= 0 || tp <= 0) return null;
    return tp / sl;
  }, [orderForm.stopLoss, orderForm.takeProfit]);
  const rrTooLow = rrValue != null && rrValue < minRrWarning;

  // 逆信号：12 系统共识方向与下单方向相反（中性/无数据不提示）
  const consensus = consensusData?.ok ? consensusData.consensus : null;
  const againstConsensus = useMemo(() => {
    if (!consensus) return null;
    const dir = consensus.direction;
    if (dir === "bullish" && orderForm.direction === "short") return consensus;
    if (dir === "bearish" && orderForm.direction === "long") return consensus;
    return null;
  }, [consensus, orderForm.direction]);

  // ── T1.4 平仓复盘打标弹窗（平仓成功后弹出；也可在交易记录页补标）──
  const [tagDialog, setTagDialog] = useState<{ positionId: number; title: string } | null>(null);

  // 可用余额（现金，不含挂单冻结）：买单冻结资金，下单金额硬约束 ≤ 可用余额。
  // 钱包未加载（null）时不放行下单，避免绕过校验。
  const availableCash = w != null ? Number(w.cash_usdt ?? w.cash ?? 0) : null;
  const amountNum = parseFloat(orderForm.amount);
  const amountValid = Number.isFinite(amountNum) && amountNum > 0;
  const exceedsBalance =
    amountValid && availableCash != null && amountNum > availableCash + 1e-9;
  const canSubmit = amountValid && availableCash != null && !exceedsBalance;

  /** 按可用余额的百分比快捷填充金额（向下取 2 位小数，不超余额） */
  const fillAmountPct = (pct: number) => {
    if (availableCash == null || availableCash <= 0) return;
    const v = Math.floor(availableCash * pct * 100) / 100;
    setOrderForm((f) => ({ ...f, amount: v > 0 ? String(v) : "" }));
  };

  const handleOrder = async () => {
    if (!canSubmit) return;
    setOrdering(true);
    setOrderResult(null);
    try {
      // 后端 /orders/place 需要限价+数量：以最新市价为限价，金额换算成数量
      const { price } = await api.alertPrice(symbol);
      if (price == null || !Number.isFinite(price) || price <= 0) {
        throw new Error(`无法获取 ${symbol} 最新价格，后端行情源可能未就绪`);
      }
      const amount = parseFloat(orderForm.amount);
      if (!Number.isFinite(amount) || amount <= 0) {
        throw new Error("下单金额必须大于 0");
      }
      // 与后端 freeze 拒单同口径的前置校验（后端仍是最终裁决，拒单而非静默截断）
      if (availableCash != null && amount > availableCash + 1e-9) {
        throw new Error(
          `下单金额 ${amount.toFixed(2)}U 超过可用余额 ${availableCash.toFixed(2)}U`,
        );
      }
      const slPct = parseFloat(orderForm.stopLoss) / 100;
      const tpPct = parseFloat(orderForm.takeProfit) / 100;
      const isLong = orderForm.direction === "long";
      const res = await api.placeOrder({
        symbol,
        side: isLong ? "buy" : "sell",
        price,
        qty: amount / price,
        stopLoss: Number.isFinite(slPct) && slPct > 0
          ? price * (isLong ? 1 - slPct : 1 + slPct)
          : undefined,
        takeProfit: Number.isFinite(tpPct) && tpPct > 0
          ? price * (isLong ? 1 + tpPct : 1 - tpPct)
          : undefined,
      });
      if (res.ok) {
        let notifyMsg = "";
        // 填了邮箱 → 为这笔挂单登记邮件提醒（order-<id>，成交转持仓后仍生效）
        const email = orderForm.notifyEmail.trim();
        if (email && res.order_id != null) {
          try {
            const nr = await api.orderNotifySet(`order-${res.order_id}`, {
              email,
              notify_take_profit: orderForm.notifyTp,
              notify_stop_loss: orderForm.notifySl,
            });
            notifyMsg = nr.ok
              ? "，邮件提醒已开启"
              : `，但邮件提醒开启失败：${nr.reason ?? "未知原因"}`;
            refetchNotify();
          } catch {
            notifyMsg = "，但邮件提醒开启失败（后端不可达）";
          }
        }
        setOrderResult({
          ok: true,
          msg: `挂单成功 #${res.order_id}（限价 ${price.toLocaleString()}）${notifyMsg}`,
        });
        setOrderForm((f) => ({ ...f, amount: "" }));
        refetchOrders();
      } else {
        setOrderResult({ ok: false, msg: `下单失败：${res.reason ?? "未知原因"}` });
      }
    } catch (e) {
      // 423 = 熔断冷静期锁单：展示剩余时间 + 触发原因 + 解锁指引（后端 body 含结构化 cooldown）
      if (e instanceof ApiError && e.status === 423) {
        const cd = e.body?.cooldown as { remaining_s?: number; expired?: boolean } | undefined;
        const remainMin = cd?.remaining_s != null ? Math.ceil(cd.remaining_s / 60) : null;
        const head =
          cd?.expired === false || (remainMin != null && remainMin > 0)
            ? `冷静期锁单中${remainMin != null ? `（剩余约 ${remainMin} 分钟）` : ""}`
            : "冷静期待确认";
        setOrderResult({
          ok: false,
          msg: `${head}：${e.reason ?? "开仓被风控拦截"} · 去「设置 → 风控」查看当日亏损归因，确认后可解锁`,
        });
      } else {
        setOrderResult({
          ok: false,
          msg: `下单失败：${e instanceof Error ? e.message : "后端服务不可达"}`,
        });
      }
    } finally {
      setOrdering(false);
    }
  };

  const handleClosePosition = async (posSymbol: string) => {
    if (!window.confirm(`确认平掉 ${posSymbol} 的全部持仓吗？将按最新市价手动平仓。`)) return;
    setClosingSymbol(posSymbol);
    try {
      const res = await api.closePosition(posSymbol);
      const n = res.closed?.length ?? 0;
      setOrderResult(
        n > 0
          ? { ok: true, msg: `已平仓 ${posSymbol} × ${n} 笔` }
          : { ok: false, msg: `${posSymbol} 无可平仓位（可能已被止盈/止损平掉）` },
      );
      // T1.4 平仓成功 → 弹复盘打标（多笔时标第一笔，其余去交易记录页补标）
      const first = res.closed?.[0] as Record<string, unknown> | undefined;
      const pid = Number(first?.position_id ?? 0);
      if (pid > 0) {
        const pnlPct = first?.pnl_pct != null ? Number(first.pnl_pct) : null;
        setTagDialog({
          positionId: pid,
          title:
            `${posSymbol} 持仓 #${pid} 已平仓` +
            (pnlPct != null ? `（${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%）` : "") +
            (n > 1 ? ` · 另有 ${n - 1} 笔可在交易记录页补标` : ""),
        });
      }
      refetchPositions();
    } catch (e) {
      setOrderResult({
        ok: false,
        msg: `平仓失败：${e instanceof Error ? e.message : "后端服务不可达"}`,
      });
    } finally {
      setClosingSymbol(null);
    }
  };

  const handleCancelOrder = async (orderId: number) => {
    if (!window.confirm(`确认撤销挂单 #${orderId} 吗？买单冻结资金将解冻退回。`)) return;
    setCancellingId(orderId);
    try {
      const res = await api.cancelOrder(orderId);
      setOrderResult(
        res.ok
          ? { ok: true, msg: `已撤销挂单 #${orderId}` }
          : { ok: false, msg: `撤单失败：${res.reason ?? "未知原因"}` },
      );
      refetchOrders();
    } catch (e) {
      setOrderResult({
        ok: false,
        msg: `撤单失败：${e instanceof Error ? e.message : "后端服务不可达"}`,
      });
    } finally {
      setCancellingId(null);
    }
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
      } else if (name === "twelve") {
        const opened = (data?.opened as unknown[]) ?? [];
        const closed = (data?.closed as unknown[])?.length ?? 0;
        const basis = opened
          .map((o) => {
            const r = o as Record<string, unknown>;
            return `${r.symbol}(${((r.systems as string[]) ?? []).length}系统)`;
          })
          .join("、");
        msg = `12系统共识：开仓 ${opened.length}${basis ? `（${basis}）` : ""} | 平仓 ${closed}`;
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
                {pos.map((p, i) => {
                  const posSymbol = String(p.symbol ?? "—");
                  const posId = Number(p.id ?? 0);
                  const notifyOrderId = `pos-${posId}`;
                  const isShort = String(p.direction ?? "long") === "short";
                  const metrics = extractCardMetrics(p);
                  return (
                    <PositionCard
                      key={`${posSymbol}-${i}`}
                      symbol={posSymbol}
                      direction={isShort ? "short" : "long"}
                      entryPrice={Number(p.entry_price ?? 0)}
                      currentPrice={
                        p.current_price != null
                          ? Number(p.current_price)
                          : undefined
                      }
                      pnlPct={
                        p.pnl_pct != null ? Number(p.pnl_pct) : undefined
                      }
                      pnlUsdt={metrics.pnlUsdt}
                      stopLoss={p.stop_loss ? Number(p.stop_loss) : undefined}
                      takeProfit={
                        p.take_profit ? Number(p.take_profit) : undefined
                      }
                      qty={metrics.qty}
                      marginUsdt={metrics.marginUsdt}
                      notionalUsdt={metrics.notionalUsdt}
                      leverage={metrics.leverage}
                      slDistPct={metrics.slDistPct}
                      tpDistPct={metrics.tpDistPct}
                      slRemainingPct={metrics.slRemainingPct}
                      slWarn={metrics.slWarn}
                      planStatus={metrics.planStatus}
                      onClose={() => handleClosePosition(posSymbol)}
                      closing={closingSymbol === posSymbol}
                      onNotify={
                        posId > 0
                          ? () =>
                              setNotifyDialog({
                                orderId: notifyOrderId,
                                title: `${posSymbol} ${isShort ? "空单" : "多单"} 持仓 #${posId}`,
                              })
                          : undefined
                      }
                      notifyOn={notifyMap.has(notifyOrderId)}
                    />
                  );
                })}
              </div>
            ) : (
              <p className="text-jarvis-text-secondary text-sm">暂无活跃持仓</p>
            )}
          </div>

          <div className="card">
            <p className="stat-label mb-4">当前挂单</p>
            {ord.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-jarvis-text-secondary border-b border-jarvis-border">
                      <th className="text-left py-2 font-medium">时间</th>
                      <th className="text-left py-2 font-medium">币种</th>
                      <th className="text-left py-2 font-medium">方向</th>
                      <th className="text-left py-2 font-medium">来源</th>
                      <th className="text-right py-2 font-medium">限价</th>
                      <th className="text-right py-2 font-medium">金额</th>
                      <th className="text-right py-2 font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* /api/orders 返回 pending 限价单，字段对齐后端 limit_orders 表 */}
                    {ord.slice(0, 10).map((o, i) => {
                      const orderId = Number((o as Record<string, number>).id ?? 0);
                      const side = String((o as Record<string, string>).side ?? "buy");
                      const createdTs = Number((o as Record<string, number>).created_ts ?? 0);
                      const userCreated =
                        String((o as Record<string, string>).source ?? "system") ===
                        "user-created";
                      return (
                        <tr
                          key={orderId || i}
                          className="border-b border-jarvis-border/50 last:border-0"
                        >
                          <td className="py-2 text-jarvis-text-secondary font-mono">
                            {createdTs > 0
                              ? new Date(createdTs * 1000).toLocaleString("zh-CN", {
                                  month: "2-digit",
                                  day: "2-digit",
                                  hour: "2-digit",
                                  minute: "2-digit",
                                })
                              : "—"}
                          </td>
                          <td className="py-2 text-jarvis-text">
                            {String((o as Record<string, string>).symbol ?? "—")}
                          </td>
                          <td className="py-2">
                            <span
                              className={clsx(
                                "text-xs px-2 py-0.5 rounded-full",
                                side === "sell"
                                  ? "bg-jarvis-red/15 text-jarvis-red"
                                  : "bg-jarvis-green/15 text-jarvis-green",
                              )}
                            >
                              {side === "sell" ? "卖" : "买"}
                            </span>
                          </td>
                          <td className="py-2">
                            <span
                              title={
                                userCreated
                                  ? "保存交易计划时自动生成的用户自创订单"
                                  : "系统/手动挂单"
                              }
                              className={clsx(
                                "text-xs px-2 py-0.5 rounded-full whitespace-nowrap",
                                userCreated
                                  ? "bg-jarvis-purple/15 text-jarvis-purple border border-jarvis-purple/30"
                                  : "bg-jarvis-border/40 text-jarvis-text-secondary",
                              )}
                            >
                              {userCreated ? "自创" : "系统"}
                            </span>
                          </td>
                          <td className="py-2 text-right text-jarvis-text font-mono">
                            ${fmtUsd(Number((o as Record<string, number>).limit_price ?? 0))}
                          </td>
                          <td className="py-2 text-right text-jarvis-text font-mono">
                            ${fmtUsd(Number((o as Record<string, number>).notional_usdt ?? 0))}
                          </td>
                          <td className="py-2 text-right">
                            <div className="inline-flex items-center gap-1.5">
                              <button
                                onClick={() =>
                                  setNotifyDialog({
                                    orderId: `order-${orderId}`,
                                    title: `${String((o as Record<string, string>).symbol ?? "—")} ${side === "sell" ? "卖" : "买"}单 挂单 #${orderId}`,
                                  })
                                }
                                disabled={!orderId}
                                title={
                                  notifyMap.has(`order-${orderId}`)
                                    ? "已开启邮件提醒（点击修改）"
                                    : "配置止盈/止损邮件提醒"
                                }
                                className={clsx(
                                  "p-1 rounded-md border transition-colors disabled:opacity-50",
                                  notifyMap.has(`order-${orderId}`)
                                    ? "border-jarvis-yellow/50 text-jarvis-yellow bg-jarvis-yellow/10"
                                    : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-yellow hover:border-jarvis-yellow/50",
                                )}
                              >
                                <Bell size={13} />
                              </button>
                              <button
                                onClick={() => handleCancelOrder(orderId)}
                                disabled={cancellingId === orderId || !orderId}
                                className="text-xs px-2 py-1 rounded-md border border-jarvis-red/40 text-jarvis-red hover:bg-jarvis-red/10 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                {cancellingId === orderId ? "撤销中..." : "撤单"}
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-jarvis-text-secondary text-sm">暂无挂单</p>
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
                <div className="flex items-center justify-between">
                  <label className="text-xs text-jarvis-text-secondary">
                    数量 (USDT)
                  </label>
                  <span
                    className="flex items-center gap-1 text-xs text-jarvis-text-secondary"
                    title="可用现金（不含挂单冻结），买入上限"
                  >
                    <Wallet size={11} />
                    可用
                    <span className="font-mono text-jarvis-text">
                      {availableCash != null
                        ? `$${fmtUsd(availableCash)}`
                        : "加载中..."}
                    </span>
                  </span>
                </div>
                <input
                  type="number"
                  value={orderForm.amount}
                  onChange={(e) =>
                    setOrderForm((f) => ({ ...f, amount: e.target.value }))
                  }
                  placeholder="100"
                  max={availableCash ?? undefined}
                  className={clsx(
                    "w-full mt-1 px-3 py-2 bg-jarvis-bg border rounded-lg text-sm text-jarvis-text font-mono",
                    exceedsBalance
                      ? "border-jarvis-red focus:outline-jarvis-red"
                      : "border-jarvis-border",
                  )}
                />
                <div className="flex gap-1.5 mt-1.5">
                  {([0.25, 0.5, 0.75, 1] as const).map((pct) => (
                    <button
                      key={pct}
                      onClick={() => fillAmountPct(pct)}
                      disabled={availableCash == null || availableCash <= 0}
                      className="flex-1 py-1 text-xs rounded-md bg-jarvis-bg border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {pct === 1 ? "全部" : `${pct * 100}%`}
                    </button>
                  ))}
                </div>
                {exceedsBalance && (
                  <p className="flex items-center gap-1 mt-1.5 text-xs text-jarvis-red">
                    <AlertTriangle size={12} className="flex-shrink-0" />
                    超过可用余额 ${fmtUsd(availableCash as number)}
                    ，请减少金额或先入金
                  </p>
                )}
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

              <div className="pt-1 border-t border-jarvis-border/60">
                <label className="text-xs text-jarvis-text-secondary flex items-center gap-1">
                  <Bell size={12} className="text-jarvis-yellow" />
                  邮件提醒（可选，填邮箱即开启）
                </label>
                <input
                  type="email"
                  value={orderForm.notifyEmail}
                  onChange={(e) =>
                    setOrderForm((f) => ({ ...f, notifyEmail: e.target.value }))
                  }
                  placeholder="you@example.com"
                  className="w-full mt-1 px-3 py-2 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text font-mono"
                />
                {orderForm.notifyEmail.trim() && (
                  <div className="flex gap-4 mt-2">
                    <label className="flex items-center gap-1.5 text-xs text-jarvis-text cursor-pointer">
                      <input
                        type="checkbox"
                        checked={orderForm.notifyTp}
                        onChange={(e) =>
                          setOrderForm((f) => ({ ...f, notifyTp: e.target.checked }))
                        }
                        className="w-3.5 h-3.5 accent-jarvis-blue cursor-pointer"
                      />
                      <span className="text-jarvis-green">止盈通知</span>
                    </label>
                    <label className="flex items-center gap-1.5 text-xs text-jarvis-text cursor-pointer">
                      <input
                        type="checkbox"
                        checked={orderForm.notifySl}
                        onChange={(e) =>
                          setOrderForm((f) => ({ ...f, notifySl: e.target.checked }))
                        }
                        className="w-3.5 h-3.5 accent-jarvis-blue cursor-pointer"
                      />
                      <span className="text-jarvis-red">止损通知</span>
                    </label>
                  </div>
                )}
              </div>

              {/* T1.3 下单前计划确认层：只提示不阻断（trading.plan_confirm_enabled 可关） */}
              {planConfirmEnabled && rrTooLow && (
                <div
                  role="alert"
                  className="flex items-start gap-1.5 p-2.5 rounded-lg text-xs bg-jarvis-yellow/10 border border-jarvis-yellow/40 text-jarvis-yellow"
                >
                  <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
                  <span>
                    盈亏比 1:{(rrValue as number).toFixed(2)} 低于建议 1:
                    {minRrWarning}——止损 {orderForm.stopLoss}% 换止盈{" "}
                    {orderForm.takeProfit}%，长期期望偏弱，建议拉大止盈或收紧止损
                  </span>
                </div>
              )}
              {planConfirmEnabled && againstConsensus && (
                <div
                  role="alert"
                  className="flex items-start gap-1.5 p-2.5 rounded-lg text-xs bg-jarvis-yellow/10 border border-jarvis-yellow/40 text-jarvis-yellow"
                >
                  <Compass size={13} className="flex-shrink-0 mt-0.5" />
                  <span>
                    当前 12 系统共识为
                    <span className="font-medium">
                      {againstConsensus.direction === "bullish" ? "看涨" : "看跌"}
                    </span>
                    （置信度 {Math.round((againstConsensus.confidence ?? 0) * 100)}%），你在
                    <span className="font-medium">逆信号交易</span>
                    ——确认有独立依据再下单
                  </span>
                </div>
              )}

              {/* [M2 s5] 磁吸位提醒：现价逼近清算/止损密集区 → 插针风险 */}
              {consensus?.seatbelt?.magnet_warning?.near && (
                <div
                  role="alert"
                  className="flex items-start gap-1.5 p-2.5 rounded-lg text-xs bg-jarvis-red/10 border border-jarvis-red/40 text-jarvis-red"
                >
                  <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
                  <span>{consensus.seatbelt.magnet_warning.note}</span>
                </div>
              )}

              <button
                onClick={handleOrder}
                disabled={ordering || !canSubmit}
                className={clsx(
                  "w-full py-2.5 rounded-lg font-medium text-white transition-colors",
                  orderForm.direction === "long"
                    ? "bg-jarvis-green hover:bg-jarvis-green/80"
                    : "bg-jarvis-red hover:bg-jarvis-red/80",
                  (ordering || !canSubmit) && "opacity-50 cursor-not-allowed",
                )}
              >
                {ordering
                  ? "下单中..."
                  : exceedsBalance
                    ? "余额不足"
                    : `确认${orderForm.direction === "long" ? "做多" : "做空"}`}
              </button>

              {orderResult && (
                <div
                  role="status"
                  className={clsx(
                    "flex items-start justify-between gap-2 p-2.5 rounded-lg text-xs",
                    orderResult.ok
                      ? "bg-jarvis-green/10 text-jarvis-green"
                      : "bg-jarvis-red/10 text-jarvis-red",
                  )}
                >
                  <span className="flex items-start gap-1.5">
                    {!orderResult.ok && (
                      <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />
                    )}
                    {orderResult.msg}
                  </span>
                  <button
                    onClick={() => setOrderResult(null)}
                    className="flex-shrink-0 opacity-60 hover:opacity-100 transition-opacity"
                    aria-label="关闭提示"
                  >
                    ✕
                  </button>
                </div>
              )}
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
              <button
                onClick={() => runAction("twelve", runTwelveCycle)}
                disabled={!!actionLoading}
                className="w-full flex items-center gap-2 px-3 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg text-sm text-jarvis-text hover:border-jarvis-purple transition-colors disabled:opacity-50"
              >
                <Compass size={14} className="text-jarvis-purple" />
                {actionLoading === "twelve" ? "共识计算中..." : "12系统信号跟盘"}
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

      {notifyDialog && (
        <OrderNotifyDialog
          orderId={notifyDialog.orderId}
          title={notifyDialog.title}
          onClose={() => setNotifyDialog(null)}
          onSaved={refetchNotify}
        />
      )}

      {tagDialog && (
        <BehaviorTagDialog
          positionId={tagDialog.positionId}
          title={tagDialog.title}
          onClose={() => setTagDialog(null)}
        />
      )}
    </div>
  );
}
