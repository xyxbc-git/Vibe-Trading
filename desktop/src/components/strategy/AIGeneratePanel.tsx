import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Sparkles,
  RotateCcw,
  Lightbulb,
  X,
  Wand2,
  Settings as SettingsIcon,
} from "lucide-react";
import { api, type StrategyGenResult } from "@/api/client";
import { useApi, usePolling } from "@/hooks/useApi";
import AIStrategySummary from "./AIStrategySummary";

const EXAMPLE_IDEAS = [
  "均线金叉的时候买入，死叉卖出，要放量确认",
  "跌得恐慌的时候抄底做多，涨回来就止盈",
  "放量突破近期高点就追多，快进快出",
];

interface AIGeneratePanelProps {
  symbol: string;
  timeframe: string;
  /** 生成成功后回调：把代码/策略名填回宿主页面（每次生成只触发一次） */
  onGenerated: (r: { name: string; code: string; result: StrategyGenResult }) => void;
  onClose: () => void;
}

/**
 * 内联「让 AI 帮我写策略」面板：自然语言想法 → /api/strategy/generate → 代码回填宿主。
 * 回测页就地使用；完整体验（一键回测/存策略库）在 /ai-strategy 工坊页。
 */
export default function AIGeneratePanel({
  symbol,
  timeframe,
  onGenerated,
  onClose,
}: AIGeneratePanelProps) {
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [startError, setStartError] = useState("");
  const [startedHere, setStartedHere] = useState(false);
  const [polling, setPolling] = useState(false);
  const filledRef = useRef(false);

  const { data: llmCfg } = useApi(() => api.llmConfig(), []);
  const llmReady = llmCfg?.configured ?? false;

  const { data: genState, refetch } = usePolling(
    () => api.strategyGenerateResult(),
    polling ? 2000 : 0,
  );
  const generating = genState?.running ?? false;
  const finished = startedHere && !generating && (genState?.finished_at ?? 0) > 0;
  const genResult = finished ? (genState?.result ?? null) : null;
  const genError = finished && !genResult?.ok ? (genState?.error ?? "生成失败") : "";

  useEffect(() => {
    if (polling && genState && !genState.running && genState.finished_at > 0) {
      setPolling(false);
    }
  }, [polling, genState]);

  // 生成成功 → 把代码/策略名填回宿主（一次性）
  useEffect(() => {
    if (genResult?.ok && genResult.code && !filledRef.current) {
      filledRef.current = true;
      onGenerated({
        name: genResult.name ?? "ai_strategy",
        code: genResult.code,
        result: genResult,
      });
    }
  }, [genResult, onGenerated]);

  const handleGenerate = async () => {
    if (!description.trim() || generating) return;
    setBusy(true);
    setStartError("");
    filledRef.current = false;
    try {
      const res = await api.strategyGenerate({
        description: description.trim(),
        symbol,
        timeframe,
      });
      if (res.ok) {
        setStartedHere(true);
        setPolling(true);
        refetch();
      } else {
        setStartError(res.error ?? "启动生成失败");
      }
    } catch (e) {
      setStartError(e instanceof Error ? e.message : "网络错误，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const errorText = startError || genError;
  const needConfig = !llmReady || errorText.includes("未配置");

  return (
    <div className="border border-jarvis-blue/40 bg-jarvis-blue/5 rounded-lg p-3 mb-2">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-sm font-medium text-jarvis-text flex items-center gap-1.5">
          <Wand2 size={14} className="text-jarvis-blue" />
          让 AI 帮我写策略
        </h4>
        <button
          onClick={onClose}
          aria-label="关闭 AI 面板"
          className="text-jarvis-text-secondary hover:text-jarvis-text"
        >
          <X size={14} />
        </button>
      </div>

      {llmCfg && !llmReady ? (
        <div className="text-sm text-jarvis-text space-y-2">
          <p>还没配置大模型，AI 无法生成策略。</p>
          <Link
            to="/settings"
            className="btn-primary inline-flex items-center gap-1.5 text-xs"
          >
            <SettingsIcon size={13} />
            去设置页配置大模型
          </Link>
        </div>
      ) : (
        <>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={`用大白话说想法，例如：${EXAMPLE_IDEAS[0]}`}
            rows={2}
            className="w-full text-sm bg-jarvis-bg border border-jarvis-border rounded p-2.5 text-jarvis-text resize-none outline-none focus:border-jarvis-blue"
          />
          <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
            <Lightbulb size={12} className="text-jarvis-yellow shrink-0" />
            {EXAMPLE_IDEAS.map((idea) => (
              <button
                key={idea}
                onClick={() => setDescription(idea)}
                className="text-[11px] px-2 py-0.5 rounded-full border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors"
              >
                {idea.length > 16 ? idea.slice(0, 16) + "…" : idea}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3 mt-2">
            <button
              onClick={handleGenerate}
              disabled={busy || generating || !description.trim()}
              className="btn-primary flex items-center gap-1.5 text-xs py-1.5 disabled:opacity-50"
            >
              {generating || busy ? (
                <RotateCcw size={13} className="animate-spin" />
              ) : (
                <Sparkles size={13} />
              )}
              {generating ? "AI 生成中…" : "生成并填入代码框"}
            </button>
            {generating && (
              <span className="text-xs text-jarvis-text-secondary">
                大模型思考中（约 10~60 秒）… {genState?.elapsed_seconds ?? 0}s
              </span>
            )}
            <Link
              to="/ai-strategy"
              className="text-xs text-jarvis-blue hover:underline ml-auto"
            >
              去 AI 策略工坊完整体验 →
            </Link>
          </div>

          {errorText && (
            <div className="mt-2 text-xs text-jarvis-red flex items-center gap-2 flex-wrap">
              <span>生成失败：{errorText}</span>
              {needConfig && (
                <Link to="/settings" className="text-jarvis-blue hover:underline">
                  去设置页配置大模型 →
                </Link>
              )}
            </div>
          )}

          {genResult?.ok && (
            <div className="mt-3">
              <p className="text-xs text-jarvis-green mb-2">
                ✓ 已生成「{genResult.name}」并填入左侧代码框，直接点「运行回测」即可
              </p>
              <AIStrategySummary result={genResult} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
