import { clsx } from "clsx";
import { TrendingUp, TrendingDown } from "lucide-react";

interface PositionCardProps {
  symbol: string;
  direction: "long" | "short";
  entryPrice: number;
  currentPrice: number;
  pnlPct: number;
  stopLoss?: number;
  takeProfit?: number;
}

export default function PositionCard({
  symbol,
  direction,
  entryPrice,
  currentPrice,
  pnlPct,
  stopLoss,
  takeProfit,
}: PositionCardProps) {
  const isProfit = pnlPct >= 0;

  return (
    <div className="card">
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
        </div>
        {isProfit ? (
          <TrendingUp size={16} className="text-jarvis-green" />
        ) : (
          <TrendingDown size={16} className="text-jarvis-red" />
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <span className="text-jarvis-text-secondary">入场</span>
          <span className="ml-2 text-jarvis-text font-mono">
            {entryPrice.toLocaleString()}
          </span>
        </div>
        <div>
          <span className="text-jarvis-text-secondary">现价</span>
          <span className="ml-2 text-jarvis-text font-mono">
            {currentPrice.toLocaleString()}
          </span>
        </div>
        {stopLoss && (
          <div>
            <span className="text-jarvis-text-secondary">止损</span>
            <span className="ml-2 text-jarvis-red font-mono">
              {stopLoss.toLocaleString()}
            </span>
          </div>
        )}
        {takeProfit && (
          <div>
            <span className="text-jarvis-text-secondary">止盈</span>
            <span className="ml-2 text-jarvis-green font-mono">
              {takeProfit.toLocaleString()}
            </span>
          </div>
        )}
      </div>

      <div className="mt-3 pt-3 border-t border-jarvis-border">
        <span className="text-jarvis-text-secondary text-sm">浮盈</span>
        <span
          className={clsx("ml-2 text-lg font-semibold font-mono", {
            "text-jarvis-green": isProfit,
            "text-jarvis-red": !isProfit,
          })}
        >
          {isProfit ? "+" : ""}
          {pnlPct.toFixed(2)}%
        </span>
      </div>
    </div>
  );
}
