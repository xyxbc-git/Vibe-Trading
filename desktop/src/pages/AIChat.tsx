import { useState, useRef, useEffect, useCallback } from "react";
import {
  Bot,
  Send,
  User,
  Loader2,
  BrainCircuit,
  AlertTriangle,
  ClipboardList,
  X,
} from "lucide-react";
import { clsx } from "clsx";
import {
  api,
  askStream,
  type ChatTurn,
  type JarvisReasonResult,
  type JarvisReviewResponse,
  type SignalDirection,
} from "@/api/client";
import { useSymbol } from "@/hooks/useSymbol";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  /** llm=大模型 rule=规则兜底；streaming 中为 undefined */
  engine?: "llm" | "rule";
}

const QUICK_QUESTIONS = [
  "现在能进场吗",
  "BTC 现在啥情况",
  "帮我看看持仓",
  "最近战绩咋样",
  "资金费率有啥说法",
  "今天市场情绪如何",
];

/** 按时段 + 当前币种生成有人味的开场白（每次进页面小范围随机，避免千篇一律） */
function welcomeText(symbol: string): string {
  const coin = symbol.replace(/USDT$/, "") || "BTC";
  const h = new Date().getHours();
  const greet =
    h < 6
      ? "这个点还没睡，看来是真放不下盘面。"
      : h < 12
        ? "早。"
        : h < 18
          ? "下午好。"
          : "晚上好。";
  const openers = [
    `${greet}盘我一直盯着呢，${coin} 那边有什么风吹草动我都记着。想问啥直接说，别客气。`,
    `${greet}我在。${coin} 的行情、你的持仓、最近的战绩，想聊哪个都行——当我是坐你旁边一起看盘的就好。`,
    `${greet}老规矩，${coin} 我帮你盯着。有想法了说一声，咱一起合计合计。`,
  ];
  return openers[Math.floor(Math.random() * openers.length)];
}

const REASON_DIR_META: Record<
  SignalDirection,
  { label: string; text: string; bg: string }
> = {
  bullish: {
    label: "看涨",
    text: "text-jarvis-green",
    bg: "bg-jarvis-green/15",
  },
  bearish: { label: "看跌", text: "text-jarvis-red", bg: "bg-jarvis-red/15" },
  neutral: {
    label: "中性",
    text: "text-jarvis-text-secondary",
    bg: "bg-jarvis-text-secondary/15",
  },
};

/** 贾维斯推理结果面板：分步推理链 + 风险警示 + 结构化建议 */
function ReasonPanel({
  result,
  cached,
  onClose,
}: {
  result: JarvisReasonResult;
  cached?: boolean;
  onClose: () => void;
}) {
  const dir: SignalDirection =
    result.direction === "bullish" || result.direction === "bearish"
      ? result.direction
      : "neutral";
  const meta = REASON_DIR_META[dir];
  const confidence = Math.max(0, Math.min(1, Number(result.confidence ?? 0)));
  const lowConfidence = confidence < 0.5;
  const sug = result.suggestion;

  return (
    <div className="border border-jarvis-border rounded-xl bg-jarvis-bg p-4 mb-3 max-h-[45vh] overflow-y-auto">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <BrainCircuit size={16} className="text-jarvis-purple" />
          <span className="text-sm font-semibold text-jarvis-text">
            贾维斯推理结论
          </span>
          <span
            className={clsx(
              "text-xs px-2 py-0.5 rounded-full font-medium",
              meta.bg,
              meta.text,
              lowConfidence && "opacity-60",
            )}
          >
            {meta.label} · {(confidence * 100).toFixed(0)}%
          </span>
          {result.degraded && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-jarvis-yellow/15 text-jarvis-yellow">
              降级模式
            </span>
          )}
          {cached && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-jarvis-blue/15 text-jarvis-blue">
              缓存结果
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
          aria-label="关闭推理面板"
        >
          <X size={16} />
        </button>
      </div>

      {/* 推理链：分步编号卡片 */}
      {(result.reasoning_chain?.length ?? 0) > 0 && (
        <div className="space-y-2 mb-3">
          {result.reasoning_chain.map((step, i) => (
            <div
              key={i}
              className="flex gap-2.5 bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2"
            >
              <span className="w-5 h-5 rounded-full bg-jarvis-purple/20 text-jarvis-purple text-xs font-mono flex items-center justify-center flex-shrink-0 mt-0.5">
                {i + 1}
              </span>
              <p className="text-xs text-jarvis-text leading-relaxed">{step}</p>
            </div>
          ))}
        </div>
      )}

      {/* 风险警示条 */}
      {(result.risks?.length ?? 0) > 0 && (
        <div className="space-y-1.5 mb-3">
          {result.risks.map((risk, i) => (
            <div
              key={i}
              className="flex items-start gap-2 bg-jarvis-red/10 border-l-2 border-jarvis-red rounded-r-lg px-3 py-2"
            >
              <AlertTriangle
                size={13}
                className="text-jarvis-red flex-shrink-0 mt-0.5"
              />
              <p className="text-xs text-jarvis-red leading-relaxed">{risk}</p>
            </div>
          ))}
        </div>
      )}

      {/* 结构化建议 */}
      {sug && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <div className="bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
            <p className="text-[10px] text-jarvis-text-secondary">操作</p>
            <p className={clsx("text-sm font-medium", meta.text)}>
              {sug.action ?? "—"}
            </p>
          </div>
          <div className="bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
            <p className="text-[10px] text-jarvis-text-secondary">入场区</p>
            <p className="text-sm font-mono text-jarvis-text">
              {sug.entry_zone ?? "—"}
            </p>
          </div>
          <div className="bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
            <p className="text-[10px] text-jarvis-text-secondary">止损</p>
            <p className="text-sm font-mono text-jarvis-red">
              {sug.stop_loss ?? "—"}
            </p>
          </div>
          <div className="bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
            <p className="text-[10px] text-jarvis-text-secondary">
              目标 / 仓位
            </p>
            <p className="text-sm font-mono text-jarvis-green">
              {sug.target ?? "—"}
              {sug.position_pct != null && (
                <span className="text-jarvis-text-secondary text-xs ml-1">
                  · {sug.position_pct}%仓
                </span>
              )}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

/** AI 交易复盘面板：统计卡 + 诊断/建议清单（借鉴 QD 策略复盘的展示结构） */
function ReviewPanel({
  data,
  onClose,
}: {
  data: JarvisReviewResponse;
  onClose: () => void;
}) {
  const stats = data.stats;
  const review = data.review;
  if (!stats || !review) return null;
  const statItems: { label: string; value: string; tone?: string }[] = [
    { label: "已平仓", value: `${stats.closed_trades} 笔` },
    { label: "胜率", value: stats.win_rate_pct != null ? `${stats.win_rate_pct}%` : "—" },
    { label: "盈亏比", value: stats.profit_factor != null ? `${stats.profit_factor}` : "—" },
    {
      label: "累计盈亏",
      value: `${stats.total_pnl_usdt}U`,
      tone: stats.total_pnl_usdt >= 0 ? "text-jarvis-green" : "text-jarvis-red",
    },
    { label: "平均持有", value: stats.avg_hold_days != null ? `${stats.avg_hold_days} 天` : "—" },
    { label: "最大连亏", value: `${stats.max_consecutive_losses} 笔` },
  ];
  return (
    <div className="border border-jarvis-border rounded-xl bg-jarvis-bg p-4 mb-3 max-h-[50vh] overflow-y-auto">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <ClipboardList size={16} className="text-jarvis-blue" />
          <span className="text-sm font-semibold text-jarvis-text">
            AI 交易复盘 · {data.symbol === "ALL" ? "全部币种" : data.symbol}
          </span>
          <span
            className={clsx(
              "text-xs px-2 py-0.5 rounded-full",
              data.source === "llm"
                ? "bg-jarvis-purple/15 text-jarvis-purple"
                : "bg-jarvis-yellow/15 text-jarvis-yellow",
            )}
          >
            {data.source === "llm" ? "LLM 深度复盘" : "规则复盘"}
          </span>
          {data.cached && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-jarvis-blue/15 text-jarvis-blue">
              缓存结果
            </span>
          )}
        </div>
        <button
          onClick={onClose}
          className="text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
          aria-label="关闭复盘面板"
        >
          <X size={16} />
        </button>
      </div>

      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mb-3">
        {statItems.map((s) => (
          <div key={s.label} className="bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2">
            <p className="text-[10px] text-jarvis-text-secondary">{s.label}</p>
            <p className={clsx("text-sm font-mono", s.tone ?? "text-jarvis-text")}>{s.value}</p>
          </div>
        ))}
      </div>

      <p className="text-xs text-jarvis-text leading-relaxed mb-3">{review.summary}</p>

      {review.diagnosis.length > 0 && (
        <div className="space-y-1.5 mb-3">
          <p className="text-xs font-semibold text-jarvis-text">诊断</p>
          {review.diagnosis.map((d, i) => (
            <div
              key={i}
              className="flex items-start gap-2 bg-jarvis-red/10 border-l-2 border-jarvis-red rounded-r-lg px-3 py-2"
            >
              <AlertTriangle size={13} className="text-jarvis-red flex-shrink-0 mt-0.5" />
              <p className="text-xs text-jarvis-red leading-relaxed">{d}</p>
            </div>
          ))}
        </div>
      )}

      {review.recommendations.length > 0 && (
        <div className="space-y-1.5 mb-3">
          <p className="text-xs font-semibold text-jarvis-text">建议</p>
          {review.recommendations.map((r, i) => (
            <div
              key={i}
              className="flex gap-2.5 bg-jarvis-card border border-jarvis-border rounded-lg px-3 py-2"
            >
              <span className="w-5 h-5 rounded-full bg-jarvis-green/20 text-jarvis-green text-xs font-mono flex items-center justify-center flex-shrink-0 mt-0.5">
                {i + 1}
              </span>
              <p className="text-xs text-jarvis-text leading-relaxed">{r}</p>
            </div>
          ))}
        </div>
      )}

      {review.cautions.length > 0 && (
        <p className="text-[10px] text-jarvis-text-secondary">
          {review.cautions.join(" · ")}
        </p>
      )}
    </div>
  );
}

function simpleMarkdown(text: string): string {
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  html = html.replace(/```([\s\S]*?)```/g, '<pre class="bg-jarvis-bg rounded-lg p-3 my-2 text-xs font-mono overflow-x-auto"><code>$1</code></pre>');
  html = html.replace(/`([^`]+)`/g, '<code class="bg-jarvis-bg px-1.5 py-0.5 rounded text-xs font-mono">$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  html = html.replace(/^### (.+)$/gm, '<h3 class="text-sm font-semibold mt-3 mb-1">$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2 class="text-sm font-semibold mt-3 mb-1">$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1 class="font-semibold mt-3 mb-1">$1</h1>');
  html = html.replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>');
  html = html.replace(/\n/g, "<br/>");

  return html;
}

export default function AIChat() {
  const { symbol } = useSymbol();
  const [messages, setMessages] = useState<Message[]>(() => [
    {
      id: "welcome",
      role: "assistant",
      content: welcomeText(symbol),
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [reasoning, setReasoning] = useState(false);
  const [reasonResult, setReasonResult] = useState<JarvisReasonResult | null>(
    null,
  );
  const [reasonCached, setReasonCached] = useState(false);
  const [reasonError, setReasonError] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [reviewResult, setReviewResult] = useState<JarvisReviewResponse | null>(
    null,
  );
  const [reviewError, setReviewError] = useState("");
  // 流式已开始出字：隐藏「思考中…」占位气泡
  const [streamStarted, setStreamStarted] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const runReason = async () => {
    if (reasoning) return;
    setReasoning(true);
    setReasonError("");
    try {
      // 封套：{ok, reasoning:{...}, cached?}；ok:false 时 HTTP 仍 200，失败保留旧结果
      const res = await api.jarvisReason(symbol);
      if (res.ok && res.reasoning) {
        setReasonResult(res.reasoning);
        setReasonCached(Boolean(res.cached));
      } else {
        setReasonError(`推理失败：${res.error ?? "信号引擎可能未就绪"}`);
      }
    } catch (e) {
      setReasonError(
        e instanceof Error
          ? `推理失败：${e.message}`
          : "推理失败：信号引擎可能未启动",
      );
    } finally {
      setReasoning(false);
    }
  };

  const runReview = async () => {
    if (reviewing) return;
    setReviewing(true);
    setReviewError("");
    try {
      // 复盘全部币种的模拟盘已平仓交易（symbol 传空 = ALL）
      const res = await api.jarvisReview();
      if (res.ok) {
        setReviewResult(res);
      } else {
        setReviewError(`复盘失败：${res.error ?? "暂无可复盘的交易"}`);
      }
    } catch (e) {
      setReviewError(
        e instanceof Error ? `复盘失败：${e.message}` : "复盘失败：后端可能未启动",
      );
    } finally {
      setReviewing(false);
    }
  };

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const sendMessage = async (text: string) => {
    const q = text.trim();
    if (!q || loading) return;

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content: q,
      timestamp: new Date(),
    };

    // 多轮上下文：携带刚发这条之前的对话（排除欢迎语），后端最多取 8 条
    const history: ChatTurn[] = messages
      .filter((m) => m.id !== "welcome")
      .slice(-8)
      .map((m) => ({ role: m.role, content: m.content }));

    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);
    setStreamStarted(false);

    const aiId = `ai-${Date.now()}`;
    let created = false;
    let engine: "llm" | "rule" | undefined;
    const appendDelta = (delta: string) => {
      if (!created) {
        created = true;
        setStreamStarted(true);
        setMessages((prev) => [
          ...prev,
          { id: aiId, role: "assistant", content: delta, timestamp: new Date(), engine },
        ]);
      } else {
        setMessages((prev) =>
          prev.map((m) => (m.id === aiId ? { ...m, content: m.content + delta } : m)),
        );
      }
    };

    try {
      // 首选流式（token 级增量渲染）；后端未配置 LLM 时 engine=rule 整段一次推完
      await askStream(
        { question: q, symbol, history },
        {
          onMeta: (meta) => {
            engine = meta.engine;
          },
          onDelta: appendDelta,
          onDone: () => {
            if (engine) {
              setMessages((prev) =>
                prev.map((m) => (m.id === aiId ? { ...m, engine } : m)),
              );
            }
          },
        },
      );
      if (!created) throw new Error("流式响应为空");
    } catch {
      // 流式不可用（旧后端/网络中断）→ 回退非流式；再失败给错误提示
      try {
        const res = await api.ask(q, symbol, history);
        const resEngine =
          res.engine === "llm" || res.engine === "rule" ? res.engine : undefined;
        if (created) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === aiId ? { ...m, content: res.answer, engine: resEngine } : m,
            ),
          );
        } else {
          setMessages((prev) => [
            ...prev,
            {
              id: aiId,
              role: "assistant",
              content: res.answer,
              timestamp: new Date(),
              engine: resEngine,
            },
          ]);
        }
      } catch {
        const errText =
          "抱歉，暂时无法处理你的请求。后端服务可能未启动，请稍后重试。";
        if (created) {
          setMessages((prev) =>
            prev.map((m) => (m.id === aiId ? { ...m, content: errText } : m)),
          );
        } else {
          setMessages((prev) => [
            ...prev,
            {
              id: `err-${Date.now()}`,
              role: "assistant",
              content: errText,
              timestamp: new Date(),
            },
          ]);
        }
      }
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-140px)]">
      <div className="flex items-center justify-between mb-6">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <Bot size={22} />
          AI 助手
        </h1>
        <div className="flex items-center gap-2">
          <button
            onClick={runReview}
            disabled={reviewing}
            className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50 !bg-jarvis-card border border-jarvis-border"
          >
            {reviewing ? (
              <Loader2 size={15} className="animate-spin" />
            ) : (
              <ClipboardList size={15} />
            )}
            {reviewing ? "复盘中…" : "AI 交易复盘"}
          </button>
          <button
            onClick={runReason}
            disabled={reasoning}
            className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50"
          >
            {reasoning ? (
              <Loader2 size={15} className="animate-spin" />
            ) : (
              <BrainCircuit size={15} />
            )}
            {reasoning ? "推理中…" : `让贾维斯推理一次（${symbol}）`}
          </button>
        </div>
      </div>

      {reasonError && (
        <div className="flex items-center gap-2 bg-jarvis-red/10 border border-jarvis-red/30 rounded-lg px-3 py-2 mb-3">
          <AlertTriangle size={14} className="text-jarvis-red flex-shrink-0" />
          <p className="text-xs text-jarvis-red">{reasonError}</p>
        </div>
      )}
      {reviewError && (
        <div className="flex items-center gap-2 bg-jarvis-red/10 border border-jarvis-red/30 rounded-lg px-3 py-2 mb-3">
          <AlertTriangle size={14} className="text-jarvis-red flex-shrink-0" />
          <p className="text-xs text-jarvis-red">{reviewError}</p>
        </div>
      )}
      {reviewResult && (
        <ReviewPanel data={reviewResult} onClose={() => setReviewResult(null)} />
      )}
      {reasonResult && (
        <ReasonPanel
          result={reasonResult}
          cached={reasonCached}
          onClose={() => setReasonResult(null)}
        />
      )}

      <div className="card flex-1 flex flex-col min-h-0">
        {/* 消息列表 */}
        <div className="flex-1 overflow-y-auto space-y-4 pr-1">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : ""}`}
            >
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
                  msg.role === "user"
                    ? "bg-jarvis-blue/20"
                    : "bg-jarvis-green/20"
                }`}
              >
                {msg.role === "user" ? (
                  <User size={16} className="text-jarvis-blue" />
                ) : (
                  <Bot size={16} className="text-jarvis-green" />
                )}
              </div>
              <div
                className={`max-w-[75%] px-4 py-3 rounded-xl text-sm leading-relaxed ${
                  msg.role === "user"
                    ? "bg-jarvis-blue/20 text-jarvis-text rounded-tr-sm"
                    : "bg-jarvis-bg border border-jarvis-border text-jarvis-text rounded-tl-sm"
                }`}
              >
                <div
                  dangerouslySetInnerHTML={{ __html: simpleMarkdown(msg.content) }}
                />
                <div className="flex items-center justify-end gap-2 mt-1.5">
                  {msg.role === "assistant" && msg.engine === "rule" && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-yellow/15 text-jarvis-yellow">
                      规则兜底
                    </span>
                  )}
                  <p className="text-[10px] text-jarvis-text-secondary">
                    {msg.timestamp.toLocaleTimeString("zh-CN", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </p>
                </div>
              </div>
            </div>
          ))}

          {loading && !streamStarted && (
            <div className="flex gap-3">
              <div className="w-8 h-8 rounded-full flex items-center justify-center bg-jarvis-green/20 flex-shrink-0">
                <Bot size={16} className="text-jarvis-green" />
              </div>
              <div className="bg-jarvis-bg border border-jarvis-border rounded-xl rounded-tl-sm px-4 py-3 flex items-center gap-2">
                <Loader2 size={14} className="animate-spin text-jarvis-blue" />
                <span className="text-sm text-jarvis-text-secondary">思考中...</span>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* 快捷问题 */}
        <div className="flex flex-wrap gap-2 mt-3 mb-3">
          {QUICK_QUESTIONS.map((q) => (
            <button
              key={q}
              onClick={() => sendMessage(q)}
              disabled={loading}
              className="text-xs px-3 py-1.5 rounded-full border border-jarvis-border
                         text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue
                         transition-colors disabled:opacity-50"
            >
              {q}
            </button>
          ))}
        </div>

        {/* 输入框 */}
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入你的问题..."
            disabled={loading}
            className="flex-1 px-4 py-2.5 bg-jarvis-bg border border-jarvis-border rounded-lg
                       text-sm text-jarvis-text placeholder-jarvis-text-secondary
                       focus:outline-none focus:border-jarvis-blue transition-colors
                       disabled:opacity-50"
          />
          <button
            onClick={() => sendMessage(input)}
            disabled={loading || !input.trim()}
            className="btn-primary flex items-center gap-2 disabled:opacity-50"
          >
            {loading ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Send size={16} />
            )}
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
