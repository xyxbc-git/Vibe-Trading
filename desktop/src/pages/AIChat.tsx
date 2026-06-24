import { useState, useRef, useEffect, useCallback } from "react";
import { Bot, Send, User, Loader2 } from "lucide-react";
import { api } from "@/api/client";
import { useSymbol } from "@/hooks/useSymbol";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

const QUICK_QUESTIONS = [
  "现在能开仓吗",
  "分析 BTC",
  "今日晨报",
  "策略建议",
  "资金费率分析",
  "市场情绪如何",
];

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
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "welcome",
      role: "assistant",
      content:
        "你好，我是 **JARVIS AI 交易助手**。\n\n我可以帮你：\n- 分析市场行情和技术指标\n- 提供交易策略建议\n- 解读资金费率与持仓数据\n- 生成每日晨报\n\n有什么可以帮你的？",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const sendMessage = async (text: string) => {
    if (!text.trim() || loading) return;

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text.trim(),
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const res = await api.ask(text.trim(), symbol);
      const aiMsg: Message = {
        id: `ai-${Date.now()}`,
        role: "assistant",
        content: res.answer,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, aiMsg]);
    } catch {
      const errMsg: Message = {
        id: `err-${Date.now()}`,
        role: "assistant",
        content: "抱歉，暂时无法处理你的请求。后端服务可能未启动，请稍后重试。",
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, errMsg]);
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
      <h1 className="page-title flex items-center gap-2">
        <Bot size={22} />
        AI 助手
      </h1>

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
                <p className="text-[10px] text-jarvis-text-secondary mt-1.5 text-right">
                  {msg.timestamp.toLocaleTimeString("zh-CN", {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </p>
              </div>
            </div>
          ))}

          {loading && (
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
