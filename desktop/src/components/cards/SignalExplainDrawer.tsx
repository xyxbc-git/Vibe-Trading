// 信号「一键解读」右侧抽屉：调 /api/twelve/signal-explain/stream 把信号术语
// 解释成大白话（SSE 流式渲染）。打开即请求；关闭（X / ESC / 遮罩）中断流。
// 后端未配置 LLM 时降级为「未配置 AI」提示 + 去设置页入口，不报错。

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import { Sparkles, X, Loader2, Settings } from "lucide-react";
import { signalExplainStream, type SignalExplainBody } from "@/api/client";

export interface ExplainRequest {
  /** 抽屉标题（如「三重平滑 RSI · 解读」「12 系统整体解读」） */
  title: string;
  body: SignalExplainBody;
}

export default function SignalExplainDrawer({
  request,
  onClose,
}: {
  /** null = 关闭态 */
  request: ExplainRequest | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<"loading" | "streaming" | "done" | "no-key" | "error">("loading");
  const [errMsg, setErrMsg] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // 打开 / 切换目标时发起流式请求；关闭或切换时 abort 旧流
  useEffect(() => {
    if (!request) return;
    const ctrl = new AbortController();
    setText("");
    setErrMsg("");
    setModel(null);
    setPhase("loading");
    (async () => {
      try {
        await signalExplainStream(
          request.body,
          {
            onMeta: (m) => setModel(m.model ?? null),
            onDelta: (t) => {
              setPhase("streaming");
              setText((prev) => prev + t);
            },
            onDone: () => setPhase((p) => (p === "no-key" ? p : "done")),
            onNotConfigured: (msg) => {
              setErrMsg(msg);
              setPhase("no-key");
            },
          },
          ctrl.signal,
        );
      } catch (e) {
        if (ctrl.signal.aborted) return;
        setErrMsg(e instanceof Error ? e.message : "解读失败，稍后重试");
        setPhase((p) => (p === "streaming" ? "done" : "error"));
      }
    })();
    return () => ctrl.abort();
  }, [request]);

  // ESC 关闭
  useEffect(() => {
    if (!request) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, onClose]);

  // 流式输出时跟随滚动到底部
  useEffect(() => {
    const el = bodyRef.current;
    if (el && phase === "streaming") el.scrollTop = el.scrollHeight;
  }, [text, phase]);

  if (!request) return null;

  return (
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label={request.title}>
      {/* 遮罩：点击关闭 */}
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="absolute inset-y-0 right-0 w-full max-w-md bg-jarvis-card border-l border-jarvis-border shadow-2xl flex flex-col">
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-jarvis-border">
          <p className="flex items-center gap-2 text-sm font-medium text-jarvis-text min-w-0">
            <Sparkles size={15} className="text-jarvis-blue shrink-0" />
            <span className="truncate">{request.title}</span>
          </p>
          <button
            onClick={onClose}
            aria-label="关闭解读"
            className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5 transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        <div ref={bodyRef} className="flex-1 overflow-y-auto px-4 py-3">
          {phase === "loading" && (
            <p className="flex items-center gap-2 text-sm text-jarvis-text-secondary">
              <Loader2 size={14} className="animate-spin" />
              AI 正在组织语言…
            </p>
          )}
          {phase === "no-key" && (
            <div className="space-y-3">
              <p className="text-sm text-jarvis-yellow leading-relaxed">{errMsg}</p>
              <button
                onClick={() => {
                  onClose();
                  navigate("/settings");
                }}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-jarvis-blue/40 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors"
              >
                <Settings size={12} />
                去设置页配置 AI
              </button>
            </div>
          )}
          {phase === "error" && (
            <p className="text-sm text-jarvis-yellow leading-relaxed">{errMsg}</p>
          )}
          {text && (
            <div className="text-[13px] text-jarvis-text leading-relaxed whitespace-pre-wrap">
              {text}
              {phase === "streaming" && (
                <span className="inline-block w-1.5 h-3.5 ml-0.5 align-middle bg-jarvis-blue animate-pulse" />
              )}
            </div>
          )}
          {phase === "done" && errMsg && (
            <p className="text-xs text-jarvis-yellow mt-3">{errMsg}</p>
          )}
        </div>

        <div
          className={clsx(
            "px-4 py-2 border-t border-jarvis-border text-[10px] text-jarvis-text-secondary",
            "flex items-center justify-between gap-2",
          )}
        >
          <span>AI 生成的教学解释，不构成投资建议</span>
          {model && <span className="font-mono truncate">{model}</span>}
        </div>
      </div>
    </div>
  );
}
