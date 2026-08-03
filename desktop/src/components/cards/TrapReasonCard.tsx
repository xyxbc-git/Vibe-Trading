// 诱多/诱空陷阱原因卡片：点击 K 线上的三角警示牌弹出，浮在图表容器右上角。
// 三段式解读（参考 FootprintChart insight 的 what/meaning/risk 结构）：
//   发生了什么  信号类型 + 时间 + 价位 + 置信度
//   为什么     后端/规则引擎给出的中文逐条证据
//   怎么办     操作建议（别追多/别追空 + 止损收紧参考位）
// 数据来自 mock 时带「演示数据」角标，避免规则演示被误当真实研判。

import { AlertTriangle, X } from "lucide-react";
import { clsx } from "clsx";
import { TRAP_LABELS, fmtTrapTime, type TrapMark } from "@/lib/trapSignals";

/** 按类型固定的「发生了什么」一句话（what 段） */
const TRAP_WHAT: Record<TrapMark["signal"]["type"], string> = {
  bull_trap:
    "价格冲破近期高点后被迅速打回，收出长上影——典型「假突破」形态，突破买盘大概率被套。",
  bear_trap:
    "价格跌破近期低点后被迅速拉回，收出长下影——典型「假破位」形态，杀跌卖盘大概率卖在坑底。",
};

const TONE = {
  bull_trap: {
    text: "text-jarvis-red",
    border: "border-jarvis-red/40",
    bg: "bg-jarvis-red/10",
    bar: "#f85149",
  },
  bear_trap: {
    text: "text-jarvis-green",
    border: "border-jarvis-green/40",
    bg: "bg-jarvis-green/10",
    bar: "#3fb950",
  },
} as const;

function fmtPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

interface TrapReasonCardProps {
  mark: TrapMark;
  onClose: () => void;
}

/** 浮层卡片：父容器需 relative；固定右上角避免遮挡点击处的 K 线形态 */
export default function TrapReasonCard({ mark, onClose }: TrapReasonCardProps) {
  const s = mark.signal;
  const tone = TONE[s.type];
  const confidence = Math.max(0, Math.min(1, s.confidence));

  return (
    <div
      className={clsx(
        "absolute top-3 right-3 z-20 w-80 max-h-[85%] overflow-y-auto",
        "rounded-lg border bg-jarvis-card/95 shadow-xl backdrop-blur-sm",
        tone.border,
      )}
    >
      {/* 头部：类型 + 演示角标 + 关闭 */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-jarvis-border/60">
        <AlertTriangle size={15} className={clsx("shrink-0", tone.text)} />
        <span className={clsx("text-sm font-semibold", tone.text)}>
          {TRAP_LABELS[s.type]}
        </span>
        {mark.mock && (
          <span
            className="px-1.5 py-px rounded text-[10px] bg-jarvis-yellow/15 text-jarvis-yellow"
            title="陷阱识别引擎未接入，当前为本地规则识别的演示数据"
          >
            演示数据
          </span>
        )}
        <button
          onClick={onClose}
          title="关闭"
          className="ml-auto p-1 rounded text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
        >
          <X size={14} />
        </button>
      </div>

      <div className="px-3 py-2.5 space-y-2.5">
        {/* 元信息：时间 · 价位 · 置信度条 */}
        <div className="flex items-center gap-2 text-xs text-jarvis-text-secondary font-mono flex-wrap">
          <span>{fmtTrapTime(s.ts)}</span>
          <span>@ {fmtPrice(s.price)}</span>
          <span className="ml-auto flex items-center gap-1.5">
            置信 {(confidence * 100).toFixed(0)}%
            <span className="inline-block w-14 h-1.5 rounded-full bg-jarvis-border/60 overflow-hidden">
              <span
                className="block h-full rounded-full"
                style={{ width: `${confidence * 100}%`, background: tone.bar }}
              />
            </span>
          </span>
        </div>

        {/* what · 发生了什么 */}
        <div>
          <p className="text-[10px] text-jarvis-text-secondary uppercase tracking-wider mb-1">
            发生了什么
          </p>
          <p className="text-xs leading-5 text-jarvis-text">{TRAP_WHAT[s.type]}</p>
        </div>

        {/* meaning · 为什么这么判断（逐条证据） */}
        {s.reasons.length > 0 && (
          <div>
            <p className="text-[10px] text-jarvis-text-secondary uppercase tracking-wider mb-1">
              为什么这么判断
            </p>
            <ul className="space-y-1">
              {s.reasons.map((r, i) => (
                <li key={i} className="flex items-start gap-1.5 text-xs leading-5 text-jarvis-text">
                  <i
                    className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ background: tone.bar }}
                  />
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* risk/action · 操作建议 */}
        <div>
          <p className="text-[10px] text-jarvis-text-secondary uppercase tracking-wider mb-1">
            操作建议
          </p>
          <p
            className={clsx("rounded border-l-2 pl-2 py-1 text-xs leading-5", tone.bg)}
            style={{ borderColor: tone.bar }}
          >
            <span className="text-jarvis-text">{s.suggestion}</span>
          </p>
        </div>

        <p className="text-[10px] text-jarvis-text-secondary/80 pt-1 border-t border-jarvis-border/50">
          {mark.mock
            ? "本地规则识别（假突破形态），识别引擎接入后自动切换为真实信号。仅供参考，不构成投资建议。"
            : "信号由陷阱识别引擎产出。仅供参考，不构成投资建议。"}
        </p>
      </div>
    </div>
  );
}
