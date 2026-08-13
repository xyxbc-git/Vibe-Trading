import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { TrendingUp, TrendingDown, Send, Wind } from "lucide-react";
import { useSymbol } from "@/hooks/useSymbol";
import {
  calcRR,
  MIN_RR,
  REASON_TAGS,
  type MentorDirection,
  type MentorPlanInput,
} from "@/api/mentor";

/** 数字输入行（受控字符串，容忍中间态如 "0." / 空串） */
function NumField({
  label,
  value,
  onChange,
  placeholder,
  suffix,
  invalid,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  suffix?: string;
  invalid?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs text-jarvis-text-secondary">{label}</span>
      <div
        className={clsx(
          "mt-1 flex items-center rounded-lg border bg-jarvis-bg px-3 py-2",
          invalid ? "border-jarvis-red" : "border-jarvis-border focus-within:border-jarvis-blue",
        )}
      >
        <input
          type="number"
          inputMode="decimal"
          step="any"
          className="w-full bg-transparent font-mono text-sm text-jarvis-text outline-none"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
        {suffix && <span className="ml-1 text-xs text-jarvis-text-secondary">{suffix}</span>}
      </div>
    </label>
  );
}

/** 情绪自评文案（1 平静 → 5 极度冲动） */
const EMOTION_CN = ["非常平静", "比较平静", "一般", "有点冲动", "极度冲动"];

export default function PlanForm({
  submitting,
  onSubmit,
}: {
  submitting: boolean;
  onSubmit: (input: MentorPlanInput) => void;
}) {
  const { symbol: globalSymbol, supported } = useSymbol();
  const [symbol, setSymbol] = useState(globalSymbol);
  const [direction, setDirection] = useState<MentorDirection>("long");
  const [entry, setEntry] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [principal, setPrincipal] = useState("");
  const [positionPct, setPositionPct] = useState("");
  const [leverage, setLeverage] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [emotion, setEmotion] = useState(3);
  const [reasonText, setReasonText] = useState("");

  const entryN = Number(entry);
  const slN = Number(stopLoss);
  const tpN = Number(takeProfit);
  const rr = useMemo(
    () => calcRR(direction, entryN, slN, tpN),
    [direction, entryN, slN, tpN],
  );
  const pointsFilled = entry !== "" && stopLoss !== "" && takeProfit !== "";
  /** 点位齐了但方向不自洽（多单 SL≥入场 等） */
  const incoherent = pointsFilled && rr == null;
  const rrTooLow = rr != null && rr < MIN_RR;

  // R1：本金风险速览——名义价值 = 本金×杠杆；预计最大亏损 ≈ 名义价值×止损距离%
  const principalN = Number(principal);
  const levN = leverage !== "" ? Number(leverage) : 1;
  const slDistPct =
    entryN > 0 && slN > 0 && !incoherent ? (Math.abs(entryN - slN) / entryN) * 100 : null;
  const notional =
    principal !== "" && principalN > 0 && levN > 0 ? principalN * levN : null;
  const maxLoss = notional != null && slDistPct != null ? (notional * slDistPct) / 100 : null;

  const canSubmit =
    !submitting && symbol && entryN > 0 && slN > 0 && tpN > 0 && !incoherent;

  function toggleTag(t: string) {
    setTags((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]));
  }

  function handleSubmit() {
    if (!canSubmit) return;
    onSubmit({
      symbol,
      direction,
      entry: entryN,
      stop_loss: slN,
      take_profit: tpN,
      principal: principal !== "" && principalN > 0 ? principalN : undefined,
      position_pct: positionPct !== "" ? Number(positionPct) : undefined,
      leverage: leverage !== "" ? Number(leverage) : undefined,
      reason_tags: tags,
      emotion_score: emotion,
      reason_text: reasonText.trim() || undefined,
    });
  }

  return (
    <div className="card">
      <h2 className="mb-1 text-base font-semibold text-jarvis-text">开单前 · 写下你的计划</h2>
      <p className="mb-4 text-xs text-jarvis-text-secondary">
        先写计划再下单——导师用系统实时证据裁决这单该不该做，事后一起复盘验证。
      </p>

      {/* 币种 + 方向 */}
      <div className="mb-4 grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-jarvis-text-secondary">币种</span>
          <select
            className="mt-1 w-full rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            {supported.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <div>
          <span className="text-xs text-jarvis-text-secondary">方向</span>
          <div className="mt-1 grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setDirection("long")}
              className={clsx(
                "flex items-center justify-center gap-1.5 rounded-lg border py-2 text-sm font-semibold transition-colors",
                direction === "long"
                  ? "border-jarvis-green bg-jarvis-green/15 text-jarvis-green"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              <TrendingUp size={16} /> 做多
            </button>
            <button
              type="button"
              onClick={() => setDirection("short")}
              className={clsx(
                "flex items-center justify-center gap-1.5 rounded-lg border py-2 text-sm font-semibold transition-colors",
                direction === "short"
                  ? "border-jarvis-red bg-jarvis-red/15 text-jarvis-red"
                  : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              <TrendingDown size={16} /> 做空
            </button>
          </div>
        </div>
      </div>

      {/* 点位 + 实时 RR */}
      <div className="mb-1 grid grid-cols-3 gap-3">
        <NumField label="入场价" value={entry} onChange={setEntry} placeholder="0.00" invalid={incoherent} />
        <NumField label="止损价" value={stopLoss} onChange={setStopLoss} placeholder="0.00" invalid={incoherent} />
        <NumField label="止盈价" value={takeProfit} onChange={setTakeProfit} placeholder="0.00" invalid={incoherent || rrTooLow} />
      </div>
      <div className="mb-4 min-h-[20px] text-xs">
        {incoherent && (
          <span className="text-jarvis-red">
            点位与方向不自洽：{direction === "long" ? "多单要求 止损 < 入场 < 止盈" : "空单要求 止盈 < 入场 < 止损"}
          </span>
        )}
        {!incoherent && rr != null && (
          <span className={clsx("font-mono", rrTooLow ? "text-jarvis-red" : "text-jarvis-green")}>
            盈亏比 RR = {rr.toFixed(2)}
            {rrTooLow && `（低于引擎门禁 ${MIN_RR}，赢一次不够亏一次）`}
          </span>
        )}
        {!incoherent && rr == null && (
          <span className="text-jarvis-text-secondary">填齐三个点位后实时计算盈亏比</span>
        )}
      </div>

      {/* 本金 / 仓位 / 杠杆（可选） */}
      <div className="mb-1 grid grid-cols-3 gap-3">
        <NumField label="本金（USDT，可选）" value={principal} onChange={setPrincipal} placeholder="100" suffix="U" />
        <NumField label="仓位（可选）" value={positionPct} onChange={setPositionPct} placeholder="10" suffix="%" />
        <NumField label="杠杆（可选）" value={leverage} onChange={setLeverage} placeholder="3" suffix="x" />
      </div>
      {/* 本金风险速览：填了本金才显示——下单前先看到「这单最多亏多少」 */}
      <div className="mb-4 min-h-[18px] text-xs">
        {notional != null && (
          <span className="font-mono text-jarvis-text-secondary">
            名义价值 <span className="text-jarvis-text">{notional.toFixed(2)}U</span>
            {leverage === "" && "（未填杠杆按 1x 算）"}
            {maxLoss != null && (
              <>
                {" · "}打到止损预计亏 <span className="text-jarvis-red">-{maxLoss.toFixed(2)}U</span>
                （占本金 {principalN > 0 ? ((maxLoss / principalN) * 100).toFixed(1) : "—"}%）
              </>
            )}
            {maxLoss == null && " · 填齐入场/止损后估算最大亏损"}
          </span>
        )}
      </div>

      {/* 理由标签 */}
      <div className="mb-4">
        <span className="text-xs text-jarvis-text-secondary">开单理由（多选）</span>
        <div className="mt-1.5 flex flex-wrap gap-2">
          {REASON_TAGS.map((t) => {
            const active = tags.includes(t);
            const isGut = t === "凭感觉";
            return (
              <button
                key={t}
                type="button"
                onClick={() => toggleTag(t)}
                className={clsx(
                  "rounded-full border px-3 py-1 text-xs transition-colors",
                  active
                    ? isGut
                      ? "border-jarvis-red bg-jarvis-red/15 text-jarvis-red"
                      : "border-jarvis-blue bg-jarvis-blue/15 text-jarvis-blue"
                    : "border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                {t}
              </button>
            );
          })}
        </div>
        {tags.includes("凭感觉") && (
          <p className="mt-1.5 text-xs text-jarvis-red">
            「凭感觉」会被导师重点盘问——最好再补一条可验证的依据。
          </p>
        )}
      </div>

      {/* 情绪自评滑块 */}
      <div className="mb-4">
        <div className="flex items-center justify-between">
          <span className="text-xs text-jarvis-text-secondary">现在的情绪状态</span>
          <span
            className={clsx(
              "text-xs font-semibold",
              emotion >= 4 ? "text-jarvis-red" : "text-jarvis-text",
            )}
          >
            {emotion}/5 · {EMOTION_CN[emotion - 1]}
          </span>
        </div>
        <input
          type="range"
          min={1}
          max={5}
          step={1}
          value={emotion}
          onChange={(e) => setEmotion(Number(e.target.value))}
          className={clsx("mt-2 w-full", emotion >= 4 ? "accent-jarvis-red" : "accent-jarvis-blue")}
          style={emotion >= 4 ? { accentColor: "#f85149" } : undefined}
        />
        {emotion >= 4 && (
          <p className="mt-1 flex items-center gap-1 text-xs text-jarvis-red">
            <Wind size={13} /> 先深呼吸——情绪分高的时候，最容易把「想赢」当成「有信号」。
          </p>
        )}
      </div>

      {/* 自由文本 */}
      <label className="mb-4 block">
        <span className="text-xs text-jarvis-text-secondary">开单逻辑（写给一周后复盘的自己）</span>
        <textarea
          rows={3}
          className="mt-1 w-full resize-none rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
          placeholder="例：4h 回踩 FVG 上沿缩量企稳，1h CVD 转正，止损放在结构低点下方…"
          value={reasonText}
          onChange={(e) => setReasonText(e.target.value)}
        />
      </label>

      <button
        type="button"
        disabled={!canSubmit}
        onClick={handleSubmit}
        className={clsx(
          "flex w-full items-center justify-center gap-2 rounded-lg py-2.5 font-medium transition-colors",
          canSubmit
            ? "bg-jarvis-blue text-jarvis-accent-fg hover:bg-jarvis-blue/80"
            : "cursor-not-allowed bg-jarvis-border/60 text-jarvis-text-secondary",
        )}
      >
        <Send size={16} />
        {submitting ? "导师裁决中…" : "提交给导师裁决"}
      </button>
    </div>
  );
}
