import { usePolling } from "@/hooks/useApi";
import { api } from "@/api/client";
import SymbolPicker from "./SymbolPicker";
import { Activity, Wifi, WifiOff } from "lucide-react";

export default function Header() {
  const { data: wallet, error } = usePolling(api.wallet, 30_000);
  const connected = !error;

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
