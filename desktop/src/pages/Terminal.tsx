import { useEffect, useMemo, useRef, useState } from "react";
import {
  Terminal as TerminalIcon,
  Trash2,
  Pause,
  Play,
  ArrowDownToLine,
  RefreshCw,
  Search,
} from "lucide-react";
import { api, LOG_STREAM_URL, type LogLine } from "@/api/client";

const MAX_LINES = 3000;
type LevelFilter = "all" | "info" | "warn" | "error";

const LEVEL_STYLE: Record<LogLine["level"], string> = {
  info: "text-jarvis-text-secondary",
  warn: "text-amber-400",
  error: "text-jarvis-red",
};

export default function Terminal() {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [connected, setConnected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [level, setLevel] = useState<LevelFilter>("all");
  const [keyword, setKeyword] = useState("");
  const [reconnectKey, setReconnectKey] = useState(0);

  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastSeqRef = useRef(0);

  // 建立 SSE 连接；失败时自动退化为轮询。
  useEffect(() => {
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const pushItems = (items: LogLine[]) => {
      if (pausedRef.current || items.length === 0) return;
      setLines((prev) => {
        const merged = [...prev, ...items];
        for (const it of items) {
          if (it.seq > lastSeqRef.current) lastSeqRef.current = it.seq;
        }
        return merged.length > MAX_LINES ? merged.slice(-MAX_LINES) : merged;
      });
    };

    const startPolling = () => {
      if (pollTimer) return;
      pollTimer = setInterval(async () => {
        try {
          const res = await api.logs(MAX_LINES);
          if (cancelled) return;
          const fresh = (res.lines || []).filter((l) => l.seq > lastSeqRef.current);
          pushItems(fresh);
        } catch {
          /* 轮询失败静默重试 */
        }
      }, 2000);
    };

    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    try {
      es = new EventSource(LOG_STREAM_URL);
      es.onopen = () => {
        if (cancelled) return;
        setConnected(true);
        stopPolling();
      };
      es.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const item = JSON.parse(ev.data) as LogLine;
          pushItems([item]);
        } catch {
          /* 忽略无法解析的行 */
        }
      };
      es.onerror = () => {
        if (cancelled) return;
        setConnected(false);
        // EventSource 会自动重连；同时启用轮询兜底，确保有日志可看。
        startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      cancelled = true;
      es?.close();
      stopPolling();
    };
  }, [reconnectKey]);

  // 自动滚动到底部。
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return lines.filter((l) => {
      if (level !== "all" && l.level !== level) return false;
      if (kw && !l.text.toLowerCase().includes(kw) && !l.source.toLowerCase().includes(kw))
        return false;
      return true;
    });
  }, [lines, level, keyword]);

  const handleClear = async () => {
    setLines([]);
    lastSeqRef.current = 0;
    try {
      await api.clearLogs();
    } catch {
      /* 后端清空失败不影响本地清屏 */
    }
  };

  return (
    <div className="flex flex-col h-full">
      <h1 className="page-title flex items-center gap-2">
        <TerminalIcon size={22} />
        后端终端
      </h1>

      {/* 工具栏 */}
      <div className="card mb-3 flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-2 text-xs">
          <span
            className={`w-2.5 h-2.5 rounded-full ${
              connected ? "bg-jarvis-green animate-pulse" : "bg-jarvis-red"
            }`}
          />
          <span className="text-jarvis-text-secondary">
            {connected ? "实时连接中" : "已断开（轮询兜底）"}
          </span>
        </span>

        <div className="flex items-center gap-1 ml-2">
          {(["all", "info", "warn", "error"] as LevelFilter[]).map((lv) => (
            <button
              key={lv}
              onClick={() => setLevel(lv)}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                level === lv
                  ? "bg-jarvis-blue/20 text-jarvis-blue"
                  : "text-jarvis-text-secondary hover:bg-white/5"
              }`}
            >
              {lv === "all" ? "全部" : lv === "info" ? "信息" : lv === "warn" ? "警告" : "错误"}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1 px-2 py-1 rounded bg-jarvis-bg border border-jarvis-border">
          <Search size={14} className="text-jarvis-text-secondary" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="过滤关键字"
            className="bg-transparent text-xs text-jarvis-text outline-none w-32"
          />
        </div>

        <div className="flex items-center gap-1 ml-auto">
          <button
            onClick={() => setPaused((p) => !p)}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded text-jarvis-text-secondary hover:bg-white/5"
            title={paused ? "继续接收" : "暂停接收"}
          >
            {paused ? <Play size={14} /> : <Pause size={14} />}
            {paused ? "继续" : "暂停"}
          </button>
          <button
            onClick={() => setAutoScroll((s) => !s)}
            className={`flex items-center gap-1 px-2 py-1 text-xs rounded transition-colors ${
              autoScroll
                ? "bg-jarvis-blue/20 text-jarvis-blue"
                : "text-jarvis-text-secondary hover:bg-white/5"
            }`}
            title="自动滚动到底部"
          >
            <ArrowDownToLine size={14} />
            自动滚动
          </button>
          <button
            onClick={() => setReconnectKey((k) => k + 1)}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded text-jarvis-text-secondary hover:bg-white/5"
            title="重新连接"
          >
            <RefreshCw size={14} />
            重连
          </button>
          <button
            onClick={handleClear}
            className="flex items-center gap-1 px-2 py-1 text-xs rounded text-jarvis-red hover:bg-jarvis-red/10"
            title="清空日志"
          >
            <Trash2 size={14} />
            清空
          </button>
        </div>
      </div>

      {/* 日志输出区 */}
      <div
        ref={scrollRef}
        className="card flex-1 min-h-0 overflow-y-auto font-mono text-xs leading-5 bg-jarvis-bg"
      >
        {filtered.length === 0 ? (
          <p className="text-jarvis-text-secondary text-center py-8">
            暂无日志。操作前端功能或等待后端输出后会在此实时显示。
          </p>
        ) : (
          filtered.map((l) => (
            <div key={l.seq} className="flex gap-2 whitespace-pre-wrap break-all">
              <span className="text-jarvis-text-secondary/60 shrink-0">{l.ts}</span>
              <span className="text-jarvis-blue/70 shrink-0 w-16 truncate" title={l.source}>
                {l.source}
              </span>
              <span className={LEVEL_STYLE[l.level]}>{l.text}</span>
            </div>
          ))
        )}
      </div>

      <p className="text-xs text-jarvis-text-secondary mt-2">
        共 {lines.length} 行（最多保留 {MAX_LINES} 行）· 显示 {filtered.length} 行
      </p>
    </div>
  );
}
