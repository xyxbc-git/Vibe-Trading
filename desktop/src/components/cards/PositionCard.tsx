import { useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import {
  TrendingUp,
  TrendingDown,
  ArrowUp,
  ArrowDown,
  Bell,
  ShieldAlert,
  CircleCheck,
  CircleAlert,
  CircleDashed,
} from "lucide-react";
import {
  riskReward,
  baseAsset,
  fmtQty,
  type PlanStatus,
} from "@/lib/positionMetrics";

interface PositionCardProps {
  symbol: string;
  direction: "long" | "short";
  entryPrice: number;
  currentPrice?: number;
  pnlPct?: number;
  /** 浮盈金额（USDT，后端按现价补列） */
  pnlUsdt?: number;
  stopLoss?: number;
  takeProfit?: number;
  /** 持仓数量（币数） */
  qty?: number;
  /** 投入/占用保证金（USDT） */
  marginUsdt?: number;
  /** 名义仓位（USDT，杠杆>1 时与保证金不同） */
  notionalUsdt?: number;
  /** 杠杆倍数（现货全额记账=1）；不传则不显示杠杆徽标 */
  leverage?: number;
  /** 现价到止损还差的百分比（负=已越过止损）；T1.7 陪伴条 */
  slDistPct?: number;
  /** 现价到止盈还差的百分比 */
  tpDistPct?: number;
  /** 剩余距离占入场→止损总距离比例%（预警文案用） */
  slRemainingPct?: number;
  /** 止损接近度预警（后端按 risk.sl_proximity_warn_pct 配置判定） */
  slWarn?: boolean;
  /** 计划状态灯：valid=共识仍同向 / reversed=信号已反转 / neutral=共识中性；
      不传=手动/限价单无信号依据（不显示灯） */
  planStatus?: PlanStatus;
  /** 传入后卡片底部显示「平仓」按钮（Trading 页用；Dashboard 不传则不显示） */
  onClose?: () => void;
  closing?: boolean;
  /** 传入后显示「提醒」铃铛按钮（打开该单邮件提醒配置弹窗） */
  onNotify?: () => void;
  /** 该单已配置邮件提醒（铃铛点亮） */
  notifyOn?: boolean;
}

const PLAN_STATUS_META: Record<
  PlanStatus,
  { label: string; cls: string; icon: React.ReactNode; hint: string }
> = {
  valid: {
    label: "计划仍有效",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <CircleCheck size={11} />,
    hint: "当前 12 系统共识与开仓方向一致，按原计划持有（止损止盈不动摇）",
  },
  reversed: {
    label: "信号已反转",
    cls: "bg-jarvis-yellow/15 text-jarvis-yellow",
    icon: <CircleAlert size={11} />,
    hint: "当前 12 系统共识已翻向反方向，建议重新评估该持仓（减仓/收紧止损/离场）",
  },
  neutral: {
    label: "共识中性",
    cls: "bg-jarvis-border/40 text-jarvis-text-secondary",
    icon: <CircleDashed size={11} />,
    hint: "当前 12 系统共识中性或暂不可判，按原计划的止损止盈纪律执行",
  },
};

type Flash = "up" | "down" | null;

const FLASH_MS = 800;

function fmtPrice(n: number) {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export default function PositionCard({
  symbol,
  direction,
  entryPrice,
  currentPrice,
  pnlPct,
  pnlUsdt,
  stopLoss,
  takeProfit,
  qty,
  marginUsdt,
  notionalUsdt,
  leverage,
  slDistPct,
  tpDistPct,
  slRemainingPct,
  slWarn,
  planStatus,
  onClose,
  closing,
  onNotify,
  notifyOn,
}: PositionCardProps) {
  const [flash, setFlash] = useState<Flash>(null);
  const prevPriceRef = useRef<number | undefined>(currentPrice);
  const flashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const prev = prevPriceRef.current;
    if (
      currentPrice !== undefined &&
      prev !== undefined &&
      currentPrice !== prev
    ) {
      setFlash(currentPrice > prev ? "up" : "down");
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
      flashTimerRef.current = setTimeout(() => setFlash(null), FLASH_MS);
    }
    prevPriceRef.current = currentPrice;
    return () => {
      if (flashTimerRef.current) clearTimeout(flashTimerRef.current);
    };
  }, [currentPrice]);

  const hasPrice = currentPrice !== undefined && Number.isFinite(currentPrice);
  const hasPnl = pnlPct !== undefined && Number.isFinite(pnlPct);
  const hasPnlUsd = pnlUsdt !== undefined && Number.isFinite(pnlUsdt);
  const isProfit = hasPnl
    ? (pnlPct as number) >= 0
    : hasPnlUsd && (pnlUsdt as number) >= 0;

  const rr = riskReward(direction, entryPrice, stopLoss, takeProfit);
  const hasSlDist = slDistPct !== undefined && Number.isFinite(slDistPct);
  const hasTpDist = tpDistPct !== undefined && Number.isFinite(tpDistPct);
  const showCompanion = hasSlDist || hasTpDist || planStatus !== undefined;
  const slBreached = hasSlDist && (slDistPct as number) < 0;
  const hasQty = qty !== undefined && Number.isFinite(qty) && qty > 0;
  const hasMargin =
    marginUsdt !== undefined && Number.isFinite(marginUsdt) && marginUsdt > 0;
  const showNotional =
    notionalUsdt !== undefined &&
    Number.isFinite(notionalUsdt) &&
    notionalUsdt > 0 &&
    leverage !== undefined &&
    leverage > 1;

  return (
    <div className="card transition-colors duration-300">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-jarvis-text font-semibold">{symbol}</span>
          <span
            className={clsx(
              "text-xs px-2 py-0.5 rounded-full font-medium",
              direction === "long"
                ? "bg-jarvis-green/15 text-jarvis-green"
                : "bg-jarvis-red/15 text-jarvis-red",
            )}
          >
            {direction === "long" ? "多" : "空"}
          </span>
          {leverage !== undefined && leverage > 0 && (
            <span
              title={
                leverage > 1
                  ? "计划杠杆倍数（来自保存的交易计划）"
                  : "现货全额记账（无杠杆）"
              }
              className={clsx(
                "text-xs px-1.5 py-0.5 rounded font-mono font-medium",
                leverage > 1
                  ? "bg-jarvis-purple/15 text-jarvis-purple"
                  : "bg-jarvis-border/40 text-jarvis-text-secondary",
              )}
            >
              {leverage}x
            </span>
          )}
          {flash && (
            <span
              className={clsx(
                "inline-flex items-center text-[10px] font-mono px-1 rounded transition-opacity",
                flash === "up"
                  ? "text-jarvis-green bg-jarvis-green/10"
                  : "text-jarvis-red bg-jarvis-red/10",
              )}
            >
              {flash === "up" ? <ArrowUp size={10} /> : <ArrowDown size={10} />}
            </span>
          )}
        </div>
        {hasPnl ? (
          isProfit ? (
            <TrendingUp size={16} className="text-jarvis-green" />
          ) : (
            <TrendingDown size={16} className="text-jarvis-red" />
          )
        ) : (
          <span className="text-jarvis-text-secondary text-xs">取价中</span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <span className="text-jarvis-text-secondary">入场</span>
          <span className="ml-2 text-jarvis-text font-mono">
            {fmtPrice(entryPrice)}
          </span>
        </div>
        <div>
          <span className="text-jarvis-text-secondary">现价</span>
          <span
            className={clsx(
              "ml-2 font-mono transition-colors duration-500",
              !hasPrice
                ? "text-jarvis-text-secondary"
                : flash === "up"
                  ? "text-jarvis-green"
                  : flash === "down"
                    ? "text-jarvis-red"
                    : "text-jarvis-text",
            )}
          >
            {hasPrice ? fmtPrice(currentPrice as number) : "——"}
          </span>
        </div>
        {stopLoss && (
          <div>
            <span className="text-jarvis-text-secondary">止损</span>
            <span className="ml-2 text-jarvis-red font-mono">
              {fmtPrice(stopLoss)}
            </span>
          </div>
        )}
        {takeProfit && (
          <div>
            <span className="text-jarvis-text-secondary">止盈</span>
            <span className="ml-2 text-jarvis-green font-mono">
              {fmtPrice(takeProfit)}
            </span>
          </div>
        )}
        {hasQty && (
          <div title="持仓数量（币数）">
            <span className="text-jarvis-text-secondary">数量</span>
            <span className="ml-2 text-jarvis-text font-mono">
              {fmtQty(qty)}
              <span className="ml-1 text-xs text-jarvis-text-secondary">
                {baseAsset(symbol)}
              </span>
            </span>
          </div>
        )}
        {hasMargin && (
          <div title="占用保证金 / 投入金额（USDT）">
            <span className="text-jarvis-text-secondary">投入</span>
            <span className="ml-2 text-jarvis-text font-mono">
              ${fmtUsd(marginUsdt as number)}
            </span>
          </div>
        )}
        {showNotional && (
          <div title="名义仓位 = 保证金 × 杠杆">
            <span className="text-jarvis-text-secondary">名义</span>
            <span className="ml-2 text-jarvis-text font-mono">
              ${fmtUsd(notionalUsdt as number)}
            </span>
          </div>
        )}
        {rr !== null && (
          <div title="盈亏比 = (止盈-入场) / (入场-止损)，按持仓方向">
            <span className="text-jarvis-text-secondary">盈亏比</span>
            <span
              className={clsx(
                "ml-2 font-mono",
                rr >= 1.5
                  ? "text-jarvis-green"
                  : rr >= 1
                    ? "text-jarvis-yellow"
                    : "text-jarvis-red",
              )}
            >
              1:{rr}
            </span>
          </div>
        )}
      </div>

      {/* ── T1.7 持仓陪伴条：距止损/距止盈 + 计划状态灯（防恐慌割肉/麻痹大意） ── */}
      {showCompanion && (
        <div
          className={clsx(
            "mt-3 px-2 py-1.5 rounded-lg flex items-center gap-2 flex-wrap text-[11px] font-mono",
            slWarn ? "bg-jarvis-yellow/10" : "bg-jarvis-bg",
          )}
        >
          {hasSlDist && (
            <span
              title={
                slBreached
                  ? "现价已越过止损价，等待盯盘引擎按纪律平仓"
                  : slRemainingPct !== undefined
                    ? `到止损的剩余空间还剩总距离的 ${slRemainingPct.toFixed(0)}%${slWarn ? "，已低于预警线（可在设置调整 sl_proximity_warn_pct）" : ""}`
                    : "现价到止损价的距离"
              }
              className={clsx(
                "inline-flex items-center gap-1",
                slBreached || slWarn ? "text-jarvis-yellow" : "text-jarvis-text-secondary",
              )}
            >
              {(slBreached || slWarn) && <ShieldAlert size={11} />}
              距止损{" "}
              <span className={slBreached || slWarn ? "font-semibold" : "text-jarvis-text"}>
                {slBreached ? "已触及" : `${(slDistPct as number).toFixed(2)}%`}
              </span>
            </span>
          )}
          {hasTpDist && (
            <span className="text-jarvis-text-secondary" title="现价到止盈价的距离">
              距止盈{" "}
              <span className="text-jarvis-text">
                {(tpDistPct as number) < 0 ? "已越过" : `${(tpDistPct as number).toFixed(2)}%`}
              </span>
            </span>
          )}
          {planStatus !== undefined && (
            <span
              title={PLAN_STATUS_META[planStatus].hint}
              className={clsx(
                "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium ml-auto",
                PLAN_STATUS_META[planStatus].cls,
              )}
            >
              {PLAN_STATUS_META[planStatus].icon}
              {PLAN_STATUS_META[planStatus].label}
            </span>
          )}
        </div>
      )}

      <div className="mt-3 pt-3 border-t border-jarvis-border flex items-center justify-between">
        <div>
          <span className="text-jarvis-text-secondary text-sm">浮盈</span>
          {hasPnl || hasPnlUsd ? (
            <span className="ml-2">
              {hasPnlUsd && (
                <span
                  className={clsx(
                    "text-lg font-semibold font-mono transition-colors duration-300",
                    isProfit ? "text-jarvis-green" : "text-jarvis-red",
                  )}
                >
                  {(pnlUsdt as number) >= 0 ? "+" : ""}
                  {fmtUsd(pnlUsdt as number)} U
                </span>
              )}
              {hasPnl && (
                <span
                  className={clsx(
                    "font-mono transition-colors duration-300",
                    hasPnlUsd
                      ? "ml-1.5 text-xs opacity-80"
                      : "text-lg font-semibold",
                    isProfit ? "text-jarvis-green" : "text-jarvis-red",
                  )}
                >
                  {hasPnlUsd ? "(" : ""}
                  {(pnlPct as number) >= 0 ? "+" : ""}
                  {(pnlPct as number).toFixed(2)}%{hasPnlUsd ? ")" : ""}
                </span>
              )}
            </span>
          ) : (
            <span className="ml-2 text-lg font-semibold font-mono text-jarvis-text-secondary">
              ——
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {onNotify && (
            <button
              onClick={onNotify}
              title={notifyOn ? "已开启邮件提醒（点击修改）" : "配置止盈/止损邮件提醒"}
              className={clsx(
                "p-1.5 rounded-lg border transition-colors",
                notifyOn
                  ? "border-jarvis-yellow/50 text-jarvis-yellow bg-jarvis-yellow/10"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-yellow hover:border-jarvis-yellow/50",
              )}
            >
              <Bell size={14} />
            </button>
          )}
          {onClose && (
            <button
              onClick={onClose}
              disabled={closing}
              className="text-xs px-2.5 py-1.5 rounded-lg border border-jarvis-red/40 text-jarvis-red hover:bg-jarvis-red/10 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {closing ? "平仓中..." : "平仓"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
