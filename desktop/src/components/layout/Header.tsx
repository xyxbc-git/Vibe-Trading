import { usePolling } from "@/hooks/useApi";
import { api } from "@/api/client";
import { useSymbol } from "@/hooks/useSymbol";
import { Activity, Wifi, WifiOff, ChevronDown } from "lucide-react";

export default function Header() {
  const { data: wallet, error } = usePolling(api.wallet, 30_000);
  const { symbol, setSymbol, supported } = useSymbol();
  const connected = !error;

  return (
    <header
      className="h-12 flex items-center justify-between pl-20 pr-6 bg-jarvis-card border-b border-jarvis-border select-none"
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
        <div className="relative">
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="appearance-none bg-jarvis-bg border border-jarvis-border rounded-md pl-2 pr-7 py-1 text-xs font-mono text-jarvis-text hover:border-jarvis-blue focus:outline-none focus:border-jarvis-blue cursor-pointer"
            title="切换币种（影响 总览 / K线 / 交易 等页面）"
          >
            {supported.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <ChevronDown
            size={12}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 text-jarvis-text-secondary pointer-events-none"
          />
        </div>

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
