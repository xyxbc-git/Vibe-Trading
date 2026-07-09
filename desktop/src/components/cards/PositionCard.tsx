import { useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import { TrendingUp, TrendingDown, ArrowUp, ArrowDown } from "lucide-react";

interface PositionCardProps {
  symbol: string;
  direction: "long" | "short";
  entryPrice: number;
  currentPrice?: number;
  pnlPct?: number;
  stopLoss?: number;
  takeProfit?: number;
  /** 传入后卡片底部显示「平仓」按钮（Trading 页用；Dashboard 不传则不显示） */
  onClose?: () => void;
  closing?: boolean;
}

type Flash = "up" | "down" | null;

const FLASH_MS = 800;

function fmtPrice(n: number) {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

export default function PositionCard({
  symbol,
  direction,
  entryPrice,
  currentPrice,
  pnlPct,
  stopLoss,
  takeProfit,
  onClose,
  closing,
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
  const isProfit = hasPnl && (pnlPct as number) >= 0;

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
      </div>

      <div className="mt-3 pt-3 border-t border-jarvis-border flex items-center justify-between">
        <div>
          <span className="text-jarvis-text-secondary text-sm">浮盈</span>
          {hasPnl ? (
            <span
              className={clsx(
                "ml-2 text-lg font-semibold font-mono transition-colors duration-300",
                isProfit ? "text-jarvis-green" : "text-jarvis-red",
              )}
            >
              {isProfit ? "+" : ""}
              {(pnlPct as number).toFixed(2)}%
            </span>
          ) : (
            <span className="ml-2 text-lg font-semibold font-mono text-jarvis-text-secondary">
              ——
            </span>
          )}
        </div>
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
  );
}
