import { useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Sparkles,
  Timer,
  ThumbsUp,
  ShieldOff,
} from "lucide-react";
import {
  mentorExplainStream,
  rememberIntent,
  LIGHT_CN,
  type MentorVerdict,
  type MentorLight,
} from "@/api/mentor";

const LIGHT_COLOR: Record<MentorLight, string> = {
  green: "#3fb950",
  yellow: "#d29922",
  red: "#f85149",
};

/** 分数环（SVG 圆环，颜色随灯色） */
function ScoreRing({ score, light }: { score: number; light: MentorLight }) {
  const R = 34;
  const C = 2 * Math.PI * R;
  const pct = Math.max(0, Math.min(100, score));
  return (
    <svg width={88} height={88} viewBox="0 0 88 88" className="shrink-0">
      <circle cx={44} cy={44} r={R} fill="none" stroke="#30363d" strokeWidth={7} />
      <circle
        cx={44}
        cy={44}
        r={R}
        fill="none"
        stroke={LIGHT_COLOR[light]}
        strokeWidth={7}
        strokeLinecap="round"
        strokeDasharray={`${(pct / 100) * C} ${C}`}
        transform="rotate(-90 44 44)"
      />
      <text
        x={44}
        y={41}
        textAnchor="middle"
        className="font-mono"
        fill="#e6edf3"
        fontSize={20}
        fontWeight={700}
      >
        {pct}
      </text>
      <text x={44} y={57} textAnchor="middle" fill="#8b949e" fontSize={10}>
        分
      </text>
    </svg>
  );
}

/** 证据行图标 */
function LevelIcon({ level }: { level: "pass" | "warn" | "fail" }) {
  if (level === "pass") return <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-jarvis-green" />;
  if (level === "warn") return <AlertTriangle size={15} className="mt-0.5 shrink-0 text-jarvis-yellow" />;
  return <XCircle size={15} className="mt-0.5 shrink-0 text-jarvis-red" />;
}

export default function VerdictCard({
  planId,
  verdict,
  mock,
  onDecided,
}: {
  planId: number | string;
  verdict: MentorVerdict;
  /** true = 本地演示裁决（后端待联调） */
  mock?: boolean;
  /** 用户做出执行/放弃决定后的回调（刷新台账） */
  onDecided?: (action: "executed" | "skipped") => void;
}) {
  const { light, score, items, summary } = verdict;

  // ─── 红灯冷静期倒计时 ───
  const cooldownSec = light === "red" ? Math.max(0, Math.round((verdict.cooldown_min ?? 0) * 60)) : 0;
  const [remain, setRemain] = useState(cooldownSec);
  const [skippedCooldown, setSkippedCooldown] = useState(false);
  const [decided, setDecided] = useState<"executed" | "skipped" | null>(null);

  useEffect(() => {
    setRemain(cooldownSec);
    setSkippedCooldown(false);
    setDecided(null);
  }, [planId, cooldownSec]);

  useEffect(() => {
    if (remain <= 0) return;
    const t = setInterval(() => setRemain((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(t);
  }, [remain > 0]); // eslint-disable-line react-hooks/exhaustive-deps -- 只需在有/无倒计时间切换时重建

  const cooldownActive = light === "red" && remain > 0 && !skippedCooldown;
  const mm = String(Math.floor(remain / 60)).padStart(2, "0");
  const ss = String(remain % 60).padStart(2, "0");

  // ─── AI 导师详解（SSE 流式） ───
  const [aiText, setAiText] = useState("");
  const [aiStreaming, setAiStreaming] = useState(false);
  const [aiNote, setAiNote] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    // 切换到新计划时重置解读区并中断旧流
    abortRef.current?.abort();
    setAiText("");
    setAiNote(null);
    setAiStreaming(false);
  }, [planId]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function startExplain() {
    if (aiStreaming) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setAiText("");
    setAiNote(null);
    setAiStreaming(true);
    mentorExplainStream(
      planId,
      {
        onDelta: (t) => setAiText((prev) => prev + t),
        onDone: () => setAiStreaming(false),
        onNotConfigured: (msg) => {
          setAiNote(msg);
          setAiStreaming(false);
        },
        onUnavailable: (msg) => {
          setAiNote(msg);
          setAiStreaming(false);
        },
      },
      controller.signal,
    ).catch((e: unknown) => {
      setAiNote(e instanceof Error ? e.message : "评语输出异常");
      setAiStreaming(false);
    });
  }

  // ─── 执行 / 放弃 ───
  /** followed 口径：红灯仍执行（含跳过冷静期）= 未听导师；其余 = 听了 */
  function decide(action: "executed" | "skipped") {
    const followed = action === "skipped" ? true : light !== "red";
    rememberIntent(planId, { action, followed, ts: Date.now() });
    setDecided(action);
    onDecided?.(action);
  }

  return (
    <div className="card" style={{ borderColor: `${LIGHT_COLOR[light]}55` }}>
      <div className="mb-1 flex items-center justify-between">
        <h2 className="text-base font-semibold text-jarvis-text">导师裁决</h2>
        {mock && (
          <span className="rounded-full border border-jarvis-yellow/50 px-2 py-0.5 text-[10px] text-jarvis-yellow">
            演示数据 · 待后端联调
          </span>
        )}
      </div>

      {/* 大灯 + 分数环 + 一句话 */}
      <div className="mb-4 flex items-center gap-4">
        <div className="relative flex h-[72px] w-[72px] shrink-0 items-center justify-center">
          <span
            className="absolute inset-0 rounded-full opacity-20"
            style={{ background: LIGHT_COLOR[light] }}
          />
          <span
            className="h-11 w-11 rounded-full shadow-lg"
            style={{ background: LIGHT_COLOR[light], boxShadow: `0 0 24px ${LIGHT_COLOR[light]}88` }}
          />
        </div>
        <ScoreRing score={score} light={light} />
        <div className="min-w-0">
          <p className="text-lg font-bold" style={{ color: LIGHT_COLOR[light] }}>
            {LIGHT_CN[light]}
          </p>
          <p className="mt-0.5 text-sm text-jarvis-text-secondary">{summary}</p>
        </div>
      </div>

      {/* 逐条证据 */}
      <div className="mb-4 space-y-2">
        {items.map((it) => (
          <div
            key={it.key}
            className="flex items-start gap-2 rounded-lg border border-jarvis-border/60 bg-jarvis-bg/60 px-3 py-2"
          >
            <LevelIcon level={it.level} />
            <div className="min-w-0">
              <p className="text-sm text-jarvis-text">{it.evidence}</p>
              {it.detail && <p className="mt-0.5 text-xs text-jarvis-text-secondary">{it.detail}</p>}
            </div>
            <span className="ml-auto shrink-0 font-mono text-[10px] text-jarvis-text-secondary">
              权重{it.weight}
            </span>
          </div>
        ))}
      </div>

      {/* 红灯冷静期 */}
      {light === "red" && cooldownActive && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-jarvis-red/40 bg-jarvis-red/10 px-3 py-2.5">
          <Timer size={16} className="shrink-0 text-jarvis-red" />
          <p className="text-sm text-jarvis-red">
            冷静期 <span className="font-mono font-bold">{mm}:{ss}</span>
            ——先离开屏幕喝口水，行情不会跑，冲动单的亏损才跑不掉。
          </p>
        </div>
      )}

      {/* 执行 / 放弃 */}
      {decided ? (
        <div
          className={clsx(
            "mb-3 rounded-lg border px-3 py-2.5 text-sm",
            decided === "skipped"
              ? "border-jarvis-green/40 bg-jarvis-green/10 text-jarvis-green"
              : "border-jarvis-yellow/40 bg-jarvis-yellow/10 text-jarvis-yellow",
          )}
        >
          {decided === "skipped"
            ? "已记录：听导师，放弃这单。等平仓周期后到复盘台账看这类单的对照战绩。"
            : "已记录执行意向。平仓后记得到「复盘台账」回填结果，信任看板才能积累证据。"}
        </div>
      ) : (
        <div className="mb-1 grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => decide("skipped")}
            className="flex items-center justify-center gap-1.5 rounded-lg bg-jarvis-green py-2.5 text-sm font-medium text-white transition-colors hover:bg-jarvis-green/80"
          >
            <ThumbsUp size={15} />
            {light === "green" ? "先不做，继续等" : "听导师，放弃这单"}
          </button>
          <button
            type="button"
            disabled={cooldownActive}
            onClick={() => decide("executed")}
            className={clsx(
              "flex items-center justify-center gap-1.5 rounded-lg py-2.5 text-sm font-medium transition-colors",
              cooldownActive
                ? "cursor-not-allowed bg-jarvis-border/60 text-jarvis-text-secondary"
                : light === "green"
                  ? "bg-jarvis-blue text-jarvis-accent-fg hover:bg-jarvis-blue/80"
                  : "bg-jarvis-red text-white hover:bg-jarvis-red/80",
            )}
          >
            {light === "green" ? "按计划执行" : "我仍要执行"}
          </button>
        </div>
      )}
      {light === "red" && cooldownActive && !decided && (
        <button
          type="button"
          onClick={() => setSkippedCooldown(true)}
          className="mb-1 flex items-center gap-1 text-[11px] text-jarvis-text-secondary underline-offset-2 hover:text-jarvis-red hover:underline"
        >
          <ShieldOff size={11} />
          跳过冷静期（会记录为「未听导师」，不锁你，但账会记下）
        </button>
      )}

      {/* AI 导师详解 */}
      <div className="mt-3 border-t border-jarvis-border/60 pt-3">
        <button
          type="button"
          onClick={startExplain}
          disabled={aiStreaming}
          className={clsx(
            "flex items-center gap-1.5 rounded-lg border border-jarvis-blue/50 px-3 py-1.5 text-sm text-jarvis-blue transition-colors",
            aiStreaming ? "cursor-wait opacity-60" : "hover:bg-jarvis-blue/10",
          )}
        >
          <Sparkles size={14} />
          {aiStreaming ? "导师正在写评语…" : "AI 导师详解（小白话）"}
        </button>
        {aiNote && <p className="mt-2 text-xs text-jarvis-yellow">{aiNote}</p>}
        {aiText && (
          <div className="mt-2 whitespace-pre-wrap rounded-lg border border-jarvis-border/60 bg-jarvis-bg/60 px-3 py-2.5 text-sm leading-relaxed text-jarvis-text">
            {aiText}
            {aiStreaming && <span className="animate-pulse">▍</span>}
          </div>
        )}
      </div>
    </div>
  );
}
