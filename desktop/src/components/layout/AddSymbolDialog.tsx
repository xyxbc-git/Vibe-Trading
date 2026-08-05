import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  Search,
  X,
  XCircle,
} from "lucide-react";
import {
  api,
  formatPrice,
  type SymbolCheckResult,
  type WatchlistAddResult,
} from "@/api/client";
import { normalizeSymbolInput, useSymbol } from "@/hooks/useSymbol";

interface AddSymbolDialogProps {
  open: boolean;
  onClose: () => void;
}

/**
 * 「添加币种」弹窗：输入币种 → 后端接入检测（GET /symbol/check）→
 * 可接入则确认加入 watchlist（POST /watchlist/add）。
 * 加入成功后同步登记到本地选择器列表并切换选中；
 * need_daemon_restart=true 时提示「daemon 重启后开始产数」。
 */
export default function AddSymbolDialog({ open, onClose }: AddSymbolDialogProps) {
  const { registerSymbol, setSymbol } = useSymbol();
  const [input, setInput] = useState("");
  const [checking, setChecking] = useState(false);
  const [adding, setAdding] = useState(false);
  /** 检测结果与其对应的规范化 symbol（输入再变化即失效） */
  const [checked, setChecked] = useState<{
    symbol: string;
    result: SymbolCheckResult;
  } | null>(null);
  const [added, setAdded] = useState<{
    symbol: string;
    result: WatchlistAddResult;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** 请求序号：丢弃过期返回（快速连点/输入变化后的旧响应） */
  const reqSeq = useRef(0);

  // 打开时聚焦输入框；关闭时重置全部状态
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
    reqSeq.current += 1;
    setInput("");
    setChecking(false);
    setAdding(false);
    setChecked(null);
    setAdded(null);
    setError(null);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const normalized = normalizeSymbolInput(input);
  /** 当前展示的检测结果是否仍与输入匹配 */
  const checkedValid = checked !== null && checked.symbol === normalized;
  const canConfirm =
    checkedValid && checked.result.ok && !adding && added === null;

  const handleInput = (v: string) => {
    setInput(v);
    // 输入变化即作废旧结果，避免「上一个币的检测结论」误导确认动作
    setChecked(null);
    setAdded(null);
    setError(null);
  };

  const runCheck = async () => {
    if (!normalized || checking) return;
    if (!/^[A-Z0-9]{2,20}USDT$/.test(normalized)) {
      setError("格式不正确，示例：PEPE 或 PEPEUSDT");
      return;
    }
    const seq = ++reqSeq.current;
    setChecking(true);
    setError(null);
    setChecked(null);
    setAdded(null);
    try {
      const result = await api.checkSymbol(normalized);
      if (seq !== reqSeq.current) return;
      setChecked({ symbol: normalized, result });
    } catch (e) {
      if (seq !== reqSeq.current) return;
      setError(e instanceof Error ? e.message : "接入检测失败，请稍后重试");
    } finally {
      if (seq === reqSeq.current) setChecking(false);
    }
  };

  const runAdd = async () => {
    if (!canConfirm) return;
    const target = checked.symbol;
    const seq = ++reqSeq.current;
    setAdding(true);
    setError(null);
    try {
      const result = await api.addWatchlist(target);
      if (seq !== reqSeq.current) return;
      if (result.ok) {
        setAdded({ symbol: target, result });
        // 后端已确认，同步登记本地选择器并切换选中
        const registered = registerSymbol(target);
        if (registered) setSymbol(registered);
      } else {
        setError(result.reason ?? "加入 watchlist 失败");
      }
    } catch (e) {
      if (seq !== reqSeq.current) return;
      setError(e instanceof Error ? e.message : "加入 watchlist 失败，请稍后重试");
    } finally {
      if (seq === reqSeq.current) setAdding(false);
    }
  };

  const onKeyDownInput = (e: React.KeyboardEvent) => {
    if (e.key !== "Enter") return;
    if (canConfirm) runAdd();
    else runCheck();
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-[380px] bg-jarvis-card border border-jarvis-border rounded-lg shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3 border-b border-jarvis-border">
          <span className="text-sm font-semibold text-jarvis-text">
            添加币种到 watchlist
          </span>
          <button
            onClick={onClose}
            className="text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
            aria-label="关闭"
          >
            <X size={15} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <div className="flex gap-2">
            <input
              ref={inputRef}
              value={input}
              onChange={(e) => handleInput(e.target.value)}
              onKeyDown={onKeyDownInput}
              placeholder="如 PEPE 或 PEPEUSDT"
              className="flex-1 min-w-0 px-2.5 py-1.5 text-sm font-mono select-text bg-jarvis-bg border border-jarvis-border rounded-md text-jarvis-text placeholder:text-jarvis-text-secondary/60 focus:outline-none focus:border-jarvis-blue"
              spellCheck={false}
              disabled={adding || added !== null}
            />
            <button
              onClick={runCheck}
              disabled={checking || adding || !normalized || added !== null}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md bg-jarvis-blue/15 text-jarvis-blue border border-jarvis-blue/40 hover:bg-jarvis-blue/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
            >
              {checking ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Search size={13} />
              )}
              接入检测
            </button>
          </div>

          {/* 检测结果区 */}
          {checkedValid && checked.result.ok && (
            <div className="flex items-start gap-2 px-3 py-2.5 rounded-md bg-jarvis-green/10 border border-jarvis-green/30">
              <CheckCircle2 size={15} className="text-jarvis-green mt-0.5 shrink-0" />
              <div className="text-xs leading-relaxed">
                <p className="text-jarvis-green font-medium">
                  {checked.symbol} 可接入
                </p>
                <p className="text-jarvis-text-secondary font-mono">
                  行情源：{checked.result.market ?? "未知"}
                  {checked.result.price != null && (
                    <> · 现价 ${formatPrice(checked.result.price)}</>
                  )}
                </p>
              </div>
            </div>
          )}
          {checkedValid && !checked.result.ok && (
            <div className="flex items-start gap-2 px-3 py-2.5 rounded-md bg-jarvis-red/10 border border-jarvis-red/30">
              <XCircle size={15} className="text-jarvis-red mt-0.5 shrink-0" />
              <div className="text-xs leading-relaxed">
                <p className="text-jarvis-red font-medium">
                  {checked.symbol} 不可接入
                </p>
                <p className="text-jarvis-text-secondary">
                  {checked.result.reason ?? "行情源不支持该交易对"}
                </p>
                {checked.result.hint && (
                  <p className="text-jarvis-text-secondary/80 mt-0.5">
                    提示：{checked.result.hint}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* 加入成功区 */}
          {added && (
            <div className="flex items-start gap-2 px-3 py-2.5 rounded-md bg-jarvis-green/10 border border-jarvis-green/30">
              <CheckCircle2 size={15} className="text-jarvis-green mt-0.5 shrink-0" />
              <div className="text-xs leading-relaxed">
                <p className="text-jarvis-green font-medium">
                  {added.symbol} 已加入 watchlist
                  {added.result.added === false && "（此前已存在）"}
                </p>
                {added.result.need_daemon_restart && (
                  <p className="text-jarvis-text-secondary">
                    daemon 重启后开始产数，届时相关页面才有该币数据
                  </p>
                )}
              </div>
            </div>
          )}

          {/* 错误区（格式错 / 请求异常 / add 拒绝） */}
          {error && (
            <div className="flex items-start gap-2 px-3 py-2 rounded-md bg-jarvis-red/10 border border-jarvis-red/30">
              <AlertCircle size={14} className="text-jarvis-red mt-0.5 shrink-0" />
              <p className="text-xs text-jarvis-red leading-relaxed">{error}</p>
            </div>
          )}

          {/* 底部动作区 */}
          <div className="flex justify-end gap-2 pt-1">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-xs rounded-md border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-text-secondary transition-colors"
            >
              {added ? "完成" : "取消"}
            </button>
            {added === null && (
              <button
                onClick={runAdd}
                disabled={!canConfirm}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md bg-jarvis-green/15 text-jarvis-green border border-jarvis-green/40 hover:bg-jarvis-green/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {adding && <Loader2 size={13} className="animate-spin" />}
                确认加入
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
