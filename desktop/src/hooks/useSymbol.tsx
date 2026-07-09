import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  type ReactNode,
} from "react";
import { api } from "@/api/client";

const STORAGE_KEY = "jarvis.symbol";
const CUSTOM_LIST_KEY = "jarvis.symbols.custom";
const DEFAULT_SYMBOL = "BTCUSDT";

export interface SymbolInfo {
  value: string;
  label: string;
  short: string;
  /** true = 用户自行添加，可删除；内置币种不可删除 */
  custom?: boolean;
}

/** 内置币种（不可删除） */
export const DEFAULT_SYMBOLS: SymbolInfo[] = [
  { value: "BTCUSDT", label: "BTC/USDT", short: "BTC" },
  { value: "ETHUSDT", label: "ETH/USDT", short: "ETH" },
  { value: "SOLUSDT", label: "SOL/USDT", short: "SOL" },
  { value: "BNBUSDT", label: "BNB/USDT", short: "BNB" },
  { value: "XRPUSDT", label: "XRP/USDT", short: "XRP" },
  { value: "DOGEUSDT", label: "DOGE/USDT", short: "DOGE" },
];

/** @deprecated 兼容旧引用；新代码请使用 useSymbol().supported（含用户自定义币种） */
export const SUPPORTED_SYMBOLS = DEFAULT_SYMBOLS;

const SYMBOL_RE = /^[A-Z0-9]{2,20}USDT$/;

function toInfo(value: string, custom = false): SymbolInfo {
  const short = value.replace(/USDT$/, "");
  return { value, label: `${short}/USDT`, short, custom };
}

/** 规范化用户输入：大写、去空白与分隔符；未带 USDT 后缀时自动补全 */
export function normalizeSymbolInput(raw: string): string {
  let s = (raw || "").toUpperCase().replace(/[\s/\-_.]/g, "");
  if (!s) return "";
  if (!s.endsWith("USDT")) s = `${s}USDT`;
  return s;
}

export interface AddSymbolResult {
  ok: boolean;
  /** 规范化后的 symbol（成功时返回） */
  value?: string;
  reason?: string;
}

interface SymbolContextValue {
  symbol: string;
  setSymbol: (s: string) => void;
  /** 内置 + 用户自定义币种合并列表 */
  supported: SymbolInfo[];
  /** 校验（格式 + 交易所存在性）并添加自定义币种，持久化到 localStorage */
  addSymbol: (raw: string) => Promise<AddSymbolResult>;
  /** 删除自定义币种（内置币种忽略）；若删除的是当前选中币种则回退到 BTCUSDT */
  removeSymbol: (value: string) => void;
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

function readCustomSymbols(): string[] {
  try {
    const raw = localStorage.getItem(CUSTOM_LIST_KEY);
    if (!raw) return [];
    const arr: unknown = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    const builtin = new Set(DEFAULT_SYMBOLS.map((s) => s.value));
    return [...new Set(
      arr
        .filter((v): v is string => typeof v === "string" && SYMBOL_RE.test(v))
        .filter((v) => !builtin.has(v)),
    )];
  } catch {
    return [];
  }
}

export function SymbolProvider({ children }: { children: ReactNode }) {
  const [symbol, setSymbolState] = useState<string>(readInitialSymbol);
  const [customSymbols, setCustomSymbols] = useState<string[]>(readCustomSymbols);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, symbol);
    } catch {
      // 忽略写入失败
    }
  }, [symbol]);

  useEffect(() => {
    try {
      localStorage.setItem(CUSTOM_LIST_KEY, JSON.stringify(customSymbols));
    } catch {
      // 忽略写入失败
    }
  }, [customSymbols]);

  const supported = useMemo<SymbolInfo[]>(
    () => [...DEFAULT_SYMBOLS, ...customSymbols.map((v) => toInfo(v, true))],
    [customSymbols],
  );

  const setSymbol = useCallback((s: string) => {
    const next = normalizeSymbolInput(s);
    if (!next) return;
    setSymbolState(next);
  }, []);

  const addSymbol = useCallback(
    async (raw: string): Promise<AddSymbolResult> => {
      const value = normalizeSymbolInput(raw);
      if (!value || !SYMBOL_RE.test(value)) {
        return { ok: false, reason: "格式不正确，示例：PEPE 或 PEPEUSDT" };
      }
      if (
        DEFAULT_SYMBOLS.some((s) => s.value === value) ||
        customSymbols.includes(value)
      ) {
        return { ok: false, reason: `${value} 已在列表中` };
      }
      // 远端校验：交易所是否存在该交易对（后端 Binance 主源 / OKX 兜底）
      try {
        const res = await api.alertPrice(value);
        if (res.price == null) {
          return { ok: false, reason: `交易所查不到 ${value}，请确认拼写` };
        }
      } catch {
        return { ok: false, reason: "无法校验交易对（后端未连接），请稍后重试" };
      }
      setCustomSymbols((prev) => (prev.includes(value) ? prev : [...prev, value]));
      return { ok: true, value };
    },
    [customSymbols],
  );

  const removeSymbol = useCallback((value: string) => {
    setCustomSymbols((prev) => prev.filter((v) => v !== value));
    setSymbolState((cur) => (cur === value ? DEFAULT_SYMBOL : cur));
  }, []);

  const ctxValue = useMemo(
    () => ({ symbol, setSymbol, supported, addSymbol, removeSymbol }),
    [symbol, setSymbol, supported, addSymbol, removeSymbol],
  );

  return (
    <SymbolContext.Provider value={ctxValue}>{children}</SymbolContext.Provider>
  );
}

export function useSymbol() {
  const ctx = useContext(SymbolContext);
  if (!ctx) {
    throw new Error("useSymbol 必须在 <SymbolProvider> 内部使用");
  }
  return ctx;
}
