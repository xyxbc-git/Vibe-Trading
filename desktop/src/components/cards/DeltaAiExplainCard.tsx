import { useEffect, useState } from "react";
import { clsx } from "clsx";
import {
  Sparkles,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  Scale,
  Radio,
  Compass,
} from "lucide-react";
import { api, type DeltaAiExplainResponse } from "@/api/client";

function timeHm(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 解读段落行：图标 + 小标题 + 正文 */
function Row({
  icon,
  label,
  text,
}: {
  icon: React.ReactNode;
  label: string;
  text: string;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="flex-shrink-0 mt-0.5 text-jarvis-blue">{icon}</span>
      <p className="text-[11px] leading-relaxed text-jarvis-text">
        <span className="text-jarvis-text-secondary mr-1">{label}</span>
        {text}
      </p>
    </div>
  );
}

/**
 * Delta 面板 AI 解读卡（M2 s7）：把订单流面板翻译成大白话。
 * 默认折叠成按钮，点击展开才触发请求（省 token）；后端 TTL 缓存
 * （signal.ai_explain_cache_min）+ 刷新按钮 force 重算；LLM 未配置时
 * 后端自动降级规则模板（source=rule 徽章标注）。
 */
export default function DeltaAiExplainCard({
  symbol,
  timeframe,
}: {
  symbol: string;
  timeframe: string;
}) {
  const [open, setOpen] = useState(false);
  const [resp, setResp] = useState<DeltaAiExplainResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchExplain = async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.deltaAiExplain(symbol, timeframe, force);
      if (r.disabled) {
        // 配置开关关闭：整卡隐藏（由 render 分支处理）
        setResp(r);
      } else if (!r.ok) {
        setError(r.error ?? "解读生成失败");
        setResp(null);
      } else {
        setResp(r);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "后端服务不可达");
      setResp(null);
    } finally {
      setLoading(false);
    }
  };

  // 展开时才首次拉取；切换币种/周期后已展开的卡重新拉取
  useEffect(() => {
    setResp(null);
    setError(null);
    if (open) void fetchExplain(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, symbol, timeframe]);

  // 配置开关关闭 → 不渲染入口
  if (resp?.disabled) return null;

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        title="让 AI 把 Delta/CVD 订单流面板翻译成大白话：买卖力量、关键信号、建议倾向"
        className="w-full flex items-center justify-center gap-1.5 py-2 rounded-lg border border-jarvis-purple/40 text-jarvis-purple text-xs font-medium hover:bg-jarvis-purple/10 transition-colors"
      >
        <Sparkles size={13} />
        AI 解读当前局势
        <ChevronDown size={13} />
      </button>
    );
  }

  const body = resp?.explain;

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-2">
        <p className="stat-label mb-0 flex items-center gap-1.5">
          <Sparkles size={14} className="text-jarvis-purple" />
          AI 解读 · {symbol.replace("USDT", "")} {timeframe}
          {resp?.source === "rule" && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded bg-jarvis-border/40 text-jarvis-text-secondary"
              title="LLM 未配置或暂不可用，当前为规则引擎解读（到设置页配置大模型可升级为 AI 生成）"
            >
              规则版
            </span>
          )}
          {resp?.cached && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue"
              title="命中缓存结果（可点刷新强制重新生成）"
            >
              缓存
            </span>
          )}
        </p>
        <span className="flex items-center gap-1">
          <button
            onClick={() => void fetchExplain(true)}
            disabled={loading}
            title="跳过缓存，强制重新生成解读"
            className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text transition-colors disabled:opacity-50"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
          </button>
          <button
            onClick={() => setOpen(false)}
            title="收起"
            className="p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
          >
            <ChevronUp size={14} />
          </button>
        </span>
      </div>

      {loading && !body ? (
        <div className="space-y-2 animate-pulse py-1">
          <div className="h-4 rounded bg-jarvis-border/30 w-3/4" />
          <div className="h-3 rounded bg-jarvis-border/30" />
          <div className="h-3 rounded bg-jarvis-border/30 w-5/6" />
          <div className="h-3 rounded bg-jarvis-border/30 w-2/3" />
        </div>
      ) : error ? (
        <div className="py-3 text-center">
          <p className="text-xs text-jarvis-text-secondary">{error}</p>
          <button
            onClick={() => void fetchExplain(false)}
            className="mt-2 text-xs px-3 py-1 rounded-lg border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors"
          >
            重试
          </button>
        </div>
      ) : body ? (
        <div className="space-y-2">
          {/* 一句话结论：加粗置顶 */}
          <p className="text-sm font-medium text-jarvis-text leading-relaxed">
            {body.headline}
          </p>
          <Row icon={<Scale size={12} />} label="力量对比" text={body.power} />
          <Row icon={<Radio size={12} />} label="关键信号" text={body.signals} />
          <Row
            icon={<Compass size={12} />}
            label="建议倾向"
            text={body.suggestion}
          />
          <p className="text-[9px] text-jarvis-text-secondary/70 pt-1 border-t border-jarvis-border/50">
            {resp?.generated_at ? `生成于 ${timeHm(resp.generated_at)} · ` : ""}
            {resp?.disclaimer ?? "自动生成内容，仅供参考，不构成投资建议。"}
          </p>
        </div>
      ) : null}
    </div>
  );
}
