import { useState } from "react";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";
import { COLORS } from "./renderer";
import type { Insight } from "./insight/rules";

const TONE_STYLE: Record<Insight["tone"], { dot: string; text: string; label: string }> = {
  bull: { dot: "#3b82f6", text: "#93c5fd", label: "偏多" },
  bear: { dot: "#ef4444", text: "#fda4af", label: "偏空" },
  warn: { dot: "#eab308", text: "#fde047", label: "注意" },
  neutral: { dot: "#64748b", text: "#cbd5e1", label: "中性" },
};

const CATEGORY_LABEL: Record<Insight["category"], string> = {
  bias: "多空力量",
  level: "关键价位",
  risk: "风险提示",
  system: "系统信号",
  profile: "成交分布",
};

function InsightItem({ ins }: { ins: Insight }) {
  const [open, setOpen] = useState(false);
  const tone = TONE_STYLE[ins.tone];
  return (
    <div
      className="rounded-lg border px-2.5 py-2"
      style={{ borderColor: COLORS.border, background: "rgba(255,255,255,0.02)" }}
    >
      <div className="flex items-center gap-1.5 text-[10px]" style={{ color: COLORS.dim }}>
        <i className="h-1.5 w-1.5 rounded-full" style={{ background: tone.dot }} />
        <span>{CATEGORY_LABEL[ins.category]}</span>
        <span
          className="rounded px-1 py-px"
          style={{ background: "rgba(255,255,255,0.06)", color: tone.text }}
        >
          {tone.label}
        </span>
        <span className="ml-auto" title="规则强度粗估，非统计学胜率">
          置信 {(ins.confidence * 100).toFixed(0)}%
        </span>
      </div>
      <p className="mt-1 text-xs leading-5" style={{ color: COLORS.text }}>
        {ins.text}
      </p>
      <button
        onClick={() => setOpen(!open)}
        className="mt-1 flex items-center gap-0.5 text-[10px] transition-colors hover:text-slate-300"
        style={{ color: COLORS.dim }}
      >
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        触发依据
      </button>
      {open && (
        <p
          className="mt-1 rounded border-l-2 pl-2 text-[10px] leading-4"
          style={{ borderColor: tone.dot, color: COLORS.dim }}
        >
          {ins.basis}
        </p>
      )}
    </div>
  );
}

/** 实时「当前画面解读」：把最近 N 根柱的订单流数据翻成人话（规则引擎产出） */
export default function InsightPanel({ insights }: { insights: Insight[] }) {
  return (
    <div
      className="flex w-60 shrink-0 flex-col border-l"
      style={{ borderColor: COLORS.border, background: COLORS.panel }}
    >
      <div
        className="flex shrink-0 items-center gap-1.5 border-b px-3 py-2"
        style={{ borderColor: COLORS.border }}
      >
        <Sparkles size={13} style={{ color: COLORS.poc }} />
        <span className="text-xs font-semibold" style={{ color: COLORS.text }}>
          当前画面解读
        </span>
        <span
          className="ml-auto flex items-center gap-1 text-[9px]"
          style={{ color: COLORS.dim }}
        >
          <i className="h-1 w-1 animate-pulse rounded-full bg-emerald-400" />
          随新柱更新
        </span>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-2">
        {insights.length === 0 ? (
          <p className="px-1 pt-2 text-[11px]" style={{ color: COLORS.dim }}>
            数据积累中，稍候自动生成解读…
          </p>
        ) : (
          insights.map((ins) => <InsightItem key={ins.id} ins={ins} />)
        )}
      </div>
    </div>
  );
}
