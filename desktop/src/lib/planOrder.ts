// 「仓位与风控建议」保存 → 自创限价单的纯函数映射。
//
// 用户在 PositionAdvisor 面板点「保存」时，除了持久化仓位配置，还要按当前
// 建议自动生成一笔 source='user-created'（自创）的限价挂单，与系统自动跟盘
// 产生的 source='system' 订单明确区分。本模块保持纯函数（无 IO、无组件依赖），
// 方便单测锁定字段映射与边界行为。

import type { PositionAdvice, OrderSource } from "../api/client";

/** api.placeOrder 所需的自创订单参数 */
export interface PlanOrderParams {
  symbol: string;
  side: "buy" | "sell";
  price: number;
  qty: number;
  stopLoss?: number;
  takeProfit?: number;
  source: OrderSource;
  note: string;
}

const isPos = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v > 0;

const round8 = (v: number) => Math.round(v * 1e8) / 1e8;

/**
 * 把当前仓位建议映射为一笔可下单的自创限价单；建议不可执行（中性/字段缺失）
 * 返回 null，调用方跳过订单创建只保存配置。
 *
 * qty 采用保证金口径（margin_usdt / entry）：钱包挂单簿按现货全额记账
 * （买单冻结 price×qty），若用杠杆后的名义币数 qty_coin，大杠杆下冻结额
 * = 名义仓位，必然「余额不足」拒单。杠杆/名义仓位/张数等完整计划上下文
 * 保留在 note 快照（JSON）里，供订单详情与邮件通知等下游消费。
 */
export function buildPlanOrder(
  symbol: string,
  tf: string,
  advice: PositionAdvice | null | undefined,
): PlanOrderParams | null {
  if (!advice?.ok) return null;
  const entry = advice.entry;
  const margin = advice.position?.margin_usdt;
  if (!isPos(entry) || !isPos(margin)) return null;

  const sl = advice.sl?.price;
  // 止盈取第一档（1:1.5，最先触发）；无分档时回退信号参考目标
  const tp = advice.take_profits?.[0]?.price ?? advice.plan_tp_ref;

  const note = JSON.stringify({
    kind: "trade-plan",
    tf,
    source_tf: advice.source_tf ?? null,
    capital_usdt: advice.capital_usdt ?? null,
    leverage: advice.leverage ?? null,
    margin_pct: advice.margin_pct ?? null,
    margin_usdt: margin,
    notional_usdt: advice.position?.notional_usdt ?? null,
    qty_coin: advice.position?.qty_coin ?? null,
    contracts: advice.position?.contracts ?? null,
    entry,
    entry_zone: advice.entry_zone ?? null,
    stop_loss: isPos(sl) ? sl : null,
    take_profits: (advice.take_profits ?? []).map((t) => ({ rr: t.rr, price: t.price })),
    liquidation: advice.liquidation?.price ?? null,
  });

  return {
    symbol,
    side: advice.side === "short" ? "sell" : "buy",
    price: entry,
    qty: round8(margin / entry),
    stopLoss: isPos(sl) ? sl : undefined,
    takeProfit: isPos(tp) ? tp : undefined,
    source: "user-created",
    note,
  };
}
