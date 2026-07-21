import { clsx } from "clsx";
import { AlertTriangle, ShieldCheck, ShieldAlert, Skull } from "lucide-react";
import { formatPrice } from "@/api/client";
import type { RiskCalcResult } from "@/lib/riskCalc";

/** 单指标格 */
function Cell({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "red" | "green" | "blue" | "yellow";
}) {
  return (
    <div className="bg-jarvis-bg rounded-lg p-2">
      <p className="text-jarvis-text-secondary text-[9px]">{label}</p>
      <p
        className={clsx(
          "text-sm font-mono",
          tone === "red" && "text-jarvis-red",
          tone === "green" && "text-jarvis-green",
          tone === "blue" && "text-jarvis-blue",
          tone === "yellow" && "text-jarvis-yellow",
          !tone && "text-jarvis-text",
        )}
      >
        {value}
      </p>
      {sub && (
        <p className="text-jarvis-text-secondary text-[9px] mt-0.5">{sub}</p>
      )}
    </div>
  );
}

/**
 * 实时风控预览：输入（本金/比例/杠杆/止损止盈）变化即重算，不依赖 AI/后端。
 * result 为 null（输入不完整/非法）时渲染引导态。
 */
export default function RiskPreviewCard({
  result,
  direction,
}: {
  result: RiskCalcResult | null;
  direction: "long" | "short";
}) {
  if (!result) {
    return (
      <p className="text-[11px] text-jarvis-text-secondary py-2 text-center">
        填写本金、下单比例、杠杆与止损/止盈后实时显示风控指标
      </p>
    );
  }
  const r = result;
  const liqSafe = r.slLiqGapPct > 0;
  const liqTone: "green" | "yellow" | "red" =
    !liqSafe ? "red" : r.slLiqGapPct < r.liqDistPct * 0.3 ? "yellow" : "green";

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
        <Cell
          label="保证金（下单金额）"
          value={`${r.marginUsdt.toLocaleString()} U`}
          sub={`名义仓位 ${r.notionalUsdt.toLocaleString()} U`}
        />
        <Cell
          label="估算强平价"
          value={formatPrice(r.liqPrice)}
          sub={`距入场 ${r.liqDistPct}% · 距止损差 ${r.slLiqGapPct}%`}
          tone={liqTone}
        />
        <Cell
          label="止损触发亏损"
          value={`-${r.slLossUsdt.toLocaleString()} U`}
          sub={`占本金 ${r.slLossPctOfCapital}%`}
          tone="red"
        />
        <Cell
          label="止盈落袋"
          value={`+${r.tpProfitUsdt.toLocaleString()} U`}
          sub={`盈亏比 ${r.rr != null ? `1:${r.rr}` : "—"} · 往返手续费≈${r.estFeeUsdt} U`}
          tone="green"
        />
      </div>

      <div className="flex items-center gap-2 flex-wrap text-[10px]">
        <span
          className={clsx(
            "inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium",
            liqSafe
              ? "bg-jarvis-green/15 text-jarvis-green"
              : "bg-jarvis-red/20 text-jarvis-red",
          )}
        >
          {liqSafe ? <ShieldCheck size={11} /> : <Skull size={11} />}
          {liqSafe
            ? `止损在强平内侧（${direction === "long" ? "跌" : "涨"}穿止损先于强平）`
            : "先强平后止损"}
        </span>
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-jarvis-bg text-jarvis-text-secondary">
          <ShieldAlert size={11} />
          当前止损距离下杠杆安全上限{" "}
          <span className="font-mono text-jarvis-text">{r.maxSafeLeverage}x</span>
        </span>
      </div>

      {r.dangers.length > 0 && (
        <div className="space-y-1">
          {r.dangers.map((d) => (
            <p
              key={d.kind}
              role="alert"
              className="flex items-start gap-1.5 text-[11px] leading-relaxed rounded px-2 py-1 bg-jarvis-red/15 text-jarvis-red font-medium"
            >
              <AlertTriangle size={12} className="flex-shrink-0 mt-0.5" />
              <span>{d.message}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
