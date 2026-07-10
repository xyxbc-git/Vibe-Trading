import { useEffect, useState } from "react";
import { usePolling } from "@/hooks/useApi";
import { api, formatPrice } from "@/api/client";
import { useSymbol } from "@/hooks/useSymbol";
import SymbolPicker from "./SymbolPicker";
import {
  Activity,
  Clock,
  TrendingDown,
  TrendingUp,
  Wifi,
  WifiOff,
} from "lucide-react";

interface HeaderQuote {
  symbol: string;
  price: number;
  /** 相邻两次报价的涨跌方向；首个报价 / 切换币种后为 null */
  tick: "up" | "down" | null;
  at: Date;
}

export default function Header() {
  const { symbol } = useSymbol();
  const { data: wallet, error } = usePolling(api.wallet, 30_000);
  // 顶栏现价复用价位提醒的轻量接口（后端 Binance 主源 / OKX 兜底），避免新增重复行情轮询
  const { data: priceData } = usePolling(
    () => api.alertPrice(symbol),
    10_000,
    [symbol],
  );
  const connected = !error;

  const [quote, setQuote] = useState<HeaderQuote | null>(null);

  useEffect(() => {
    if (priceData?.price == null) return;
    const { symbol: sym, price } = priceData;
    setQuote((prev) => ({
      symbol: sym,
      price,
      at: new Date(),
      tick:
        prev && prev.symbol === sym
          ? price > prev.price
            ? "up"
            : price < prev.price
              ? "down"
              : prev.tick
          : null,
    }));
  }, [priceData]);

  // 只显示与当前选中币种匹配的报价，避免切币瞬间残留旧币价格
  const live = quote?.symbol === symbol ? quote : null;
  const priceColor =
    live?.tick === "up"
      ? "text-jarvis-green"
      : live?.tick === "down"
        ? "text-jarvis-red"
        : "text-jarvis-text";

  return (
    <header
      className="h-12 flex items-center justify-between px-6 bg-jarvis-card border-b border-jarvis-border select-none"
      style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
    >
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <Activity size={18} className="text-jarvis-blue" />
          <span className="text-sm font-semibold text-jarvis-text">
            JARVIS Terminal
          </span>
        </div>
      </div>

      <div
        className="flex items-center gap-4"
        style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}
      >
        <div className="flex items-center gap-3">
          <span
            className={`flex items-center gap-1 text-sm font-mono ${priceColor}`}
            title={`${symbol} 现价 · 约 10 秒自动刷新`}
          >
            {live?.tick === "up" && <TrendingUp size={14} />}
            {live?.tick === "down" && <TrendingDown size={14} />}
            {live ? `$${formatPrice(live.price)}` : "—"}
          </span>
          <span
            className="flex items-center gap-1.5 text-xs font-mono text-jarvis-text-secondary"
            title="行情最后刷新时间"
          >
            <Clock size={12} />
            {quote
              ? quote.at.toLocaleTimeString("en-GB", { hour12: false })
              : "--:--:--"}
          </span>
        </div>

        <SymbolPicker />

        {wallet && (
          <span className="text-sm font-mono text-jarvis-text-secondary">
            余额:{" "}
            <span className="text-jarvis-text">
              $
              {Number(
                (wallet as Record<string, unknown>)?.cash_usdt ??
                  (wallet as Record<string, unknown>)?.cash ??
                  0,
              ).toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </span>
          </span>
        )}
        <div className="flex items-center gap-1.5">
          {connected ? (
            <>
              <Wifi size={14} className="text-jarvis-green" />
              <span className="text-xs text-jarvis-green">已连接</span>
            </>
          ) : (
            <>
              <WifiOff size={14} className="text-jarvis-red" />
              <span className="text-xs text-jarvis-red">断开</span>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
