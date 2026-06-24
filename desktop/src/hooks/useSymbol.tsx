import {
  createContext,
  useContext,
  useState,
  useEffect,
  type ReactNode,
} from "react";

const STORAGE_KEY = "jarvis.symbol";
const DEFAULT_SYMBOL = "BTCUSDT";

export const SUPPORTED_SYMBOLS = [
  { value: "BTCUSDT", label: "BTC/USDT", short: "BTC" },
  { value: "ETHUSDT", label: "ETH/USDT", short: "ETH" },
  { value: "SOLUSDT", label: "SOL/USDT", short: "SOL" },
  { value: "BNBUSDT", label: "BNB/USDT", short: "BNB" },
  { value: "XRPUSDT", label: "XRP/USDT", short: "XRP" },
  { value: "DOGEUSDT", label: "DOGE/USDT", short: "DOGE" },
] as const;

export type SymbolValue = (typeof SUPPORTED_SYMBOLS)[number]["value"];

interface SymbolContextValue {
  symbol: string;
  setSymbol: (s: string) => void;
  supported: typeof SUPPORTED_SYMBOLS;
}

const SymbolContext = createContext<SymbolContextValue | null>(null);

function readInitialSymbol(): string {
  try {
    const fromStorage = localStorage.getItem(STORAGE_KEY);
    if (fromStorage && /^[A-Z0-9]+USDT$/.test(fromStorage)) {
      return fromStorage;
    }
  } catch {
    // localStorage 不可用时 fallback
  }
  return DEFAULT_SYMBOL;
}

export function SymbolProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbolState] = useState<string>(readInitialSymbol);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, symbol);
    } catch {
      // 忽略写入失败
    }
  }, [symbol]);

  const setSymbol = (s: string) => {
    const next = s.toUpperCase().trim();
    if (!next) return;
    setSymbolState(next.endsWith("USDT") ? next : `${next}USDT`);
  };

  return (
    <SymbolContext.Provider
      value={{ symbol, setSymbol, supported: SUPPORTED_SYMBOLS }}
    >
      {children}
    </SymbolContext.Provider>
  );
}

export function useSymbol() {
  const ctx = useContext(SymbolContext);
  if (!ctx) {
    throw new Error("useSymbol 必须在 <SymbolProvider> 内部使用");
  }
  return ctx;
}
