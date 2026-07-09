import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Plus, X } from "lucide-react";
import { useSymbol } from "@/hooks/useSymbol";

/**
 * 顶栏币种选择器：支持切换、添加（格式 + 交易所存在性校验）、删除自定义币种。
 * 列表持久化在 localStorage（jarvis.symbols.custom），切换后经 SymbolContext 全局生效。
 */
export default function SymbolPicker() {
  const { symbol, setSymbol, supported, addSymbol, removeSymbol } = useSymbol();
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const current = supported.find((s) => s.value === symbol);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) {
      setMsg(null);
      setInput("");
    }
  }, [open]);

  const submitAdd = async () => {
    if (!input.trim() || busy) return;
    setBusy(true);
    setMsg(null);
    const res = await addSymbol(input);
    setBusy(false);
    if (res.ok && res.value) {
      setSymbol(res.value);
      setInput("");
      setMsg({ text: `已添加并切换到 ${res.value}`, ok: true });
    } else {
      setMsg({ text: res.reason ?? "添加失败", ok: false });
    }
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 bg-jarvis-bg border border-jarvis-border rounded-md pl-2.5 pr-2 py-1 text-xs font-mono text-jarvis-text hover:border-jarvis-blue focus:outline-none focus:border-jarvis-blue cursor-pointer"
        title="切换/管理币种（影响 总览 / K线 / 交易 等页面）"
      >
        {current?.label ?? symbol}
        <ChevronDown
          size={12}
          className={`text-jarvis-text-secondary transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1.5 w-60 bg-jarvis-card border border-jarvis-border rounded-lg shadow-2xl z-50 overflow-hidden">
          <div className="max-h-64 overflow-y-auto py-1">
            {supported.map((s) => (
              <div
                key={s.value}
                className="group flex items-center hover:bg-white/5"
              >
                <button
                  onClick={() => {
                    setSymbol(s.value);
                    setOpen(false);
                  }}
                  className="flex-1 flex items-center justify-between px-3 py-1.5 text-xs font-mono text-left text-jarvis-text"
                >
                  <span>{s.label}</span>
                  {s.value === symbol && (
                    <Check size={12} className="text-jarvis-blue" />
                  )}
                </button>
                {s.custom && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      removeSymbol(s.value);
                    }}
                    className="px-2 py-1.5 text-jarvis-text-secondary opacity-0 group-hover:opacity-100 hover:text-jarvis-red transition-opacity"
                    title={`删除 ${s.label}`}
                    aria-label={`删除 ${s.label}`}
                  >
                    <X size={12} />
                  </button>
                )}
              </div>
            ))}
          </div>

          <div className="border-t border-jarvis-border p-2">
            <div className="flex gap-1.5">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitAdd();
                }}
                placeholder="如 PEPE 或 PEPEUSDT"
                className="flex-1 min-w-0 px-2 py-1 text-xs font-mono select-text bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text placeholder:text-jarvis-text-secondary/60 focus:outline-none focus:border-jarvis-blue"
                spellCheck={false}
              />
              <button
                onClick={submitAdd}
                disabled={busy || !input.trim()}
                className="flex items-center justify-center w-7 h-7 shrink-0 rounded-md bg-jarvis-blue/15 text-jarvis-blue border border-jarvis-blue/40 hover:bg-jarvis-blue/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                title="添加币种"
                aria-label="添加币种"
              >
                {busy ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Plus size={13} />
                )}
              </button>
            </div>
            {msg && (
              <p
                className={`mt-1.5 text-[11px] leading-snug ${msg.ok ? "text-jarvis-green" : "text-jarvis-red"}`}
              >
                {msg.text}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
