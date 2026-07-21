// AI 点位/风控推荐链路：上下文组装 → LLM（api.ask）→ 结构化解析 → 本地规则降级。
//
// 设计取舍：
//   - 复用既有 /api/ask 通道（AIChat 同款，engine 字段区分 llm/rule），不新增后端接口。
//   - AI 负责方向/点位/理由；杠杆与仓位比例的数值推导优先信本地公式（riskCalc），
//     LLM 的算术不可靠，返回值仅做参考并夹紧到合法区间。
//   - LLM 未配置（engine=rule）/超时/JSON 解析失败 → localAdvice 用 12 系统共识计划
//     生成同构建议，标注 source="local-rule"，链路永不空手而归。

import { api, type TwelveConsensus } from "../api/client";
import {
  marginPctForRisk,
  maxSafeLeverage,
  RISK_PRESETS,
  type RiskPreset,
} from "./riskCalc";

/** 可勾选的参考信号源（AI 上下文与降级依据） */
export const SIGNAL_SOURCES = [
  { key: "consensus", label: "12系统共识", desc: "十二信号系统多周期共识与交易计划" },
  { key: "sentiment", label: "供需情绪", desc: "多空比/资金费率/OI/恐贪四因子综合" },
  { key: "intel", label: "市场情报", desc: "资金费率、持仓量、恐贪指数原始值" },
  { key: "tape", label: "盘口主体", desc: "散户/机构/做市商成交流画像（需 WS 就绪）" },
  { key: "reversal", label: "反转评分", desc: "高胜率反转四条件叠加（15m）" },
] as const;

export type SignalSourceKey = (typeof SIGNAL_SOURCES)[number]["key"];

export interface AiTradeAdvice {
  side: "long" | "short" | "wait";
  entryLow: number | null;
  entryHigh: number | null;
  stopLoss: number | null;
  takeProfit1: number | null;
  takeProfit2: number | null;
  /** 建议杠杆（整数，已夹紧 1~125） */
  leverage: number | null;
  /** 建议下单比例（保证金占本金 %） */
  positionPct: number | null;
  riskLevel: "low" | "medium" | "high";
  /** 中文大白话理由 */
  reason: string;
  source: "llm" | "local-rule";
  /** 附注（降级原因等） */
  note?: string;
}

export interface AiAdviceParams {
  symbol: string;
  /** 现价（无价格时传 null，AI 上下文缺价、降级链路要求共识计划存在） */
  price: number | null;
  capital: number;
  /** 用户当前拟用下单比例 % */
  pctOfCapital: number;
  /** 用户当前拟用杠杆 */
  leverage: number;
  riskPreset: RiskPreset;
  /** 勾选的信号源 */
  sources: SignalSourceKey[];
  /** 页面已轮询的共识缓存（避免重复拉取；未勾选 consensus 也可用于降级） */
  consensusCache?: TwelveConsensus | null;
}

const toNum = (v: unknown): number | null => {
  const n = typeof v === "string" ? parseFloat(v) : (v as number);
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? n : null;
};

const clampLev = (v: number | null): number | null =>
  v == null ? null : Math.max(1, Math.min(125, Math.round(v)));

const clampPct = (v: number | null): number | null =>
  v == null ? null : Math.max(0.1, Math.min(100, Math.round(v * 100) / 100));

const DIR_CN: Record<string, string> = {
  bullish: "看涨",
  bearish: "看跌",
  neutral: "中性",
};

/** 拉取勾选信号源并生成一行摘要；单源失败不阻断（注明不可用） */
async function buildSignalDigest(
  symbol: string,
  keys: SignalSourceKey[],
  consensusCache?: TwelveConsensus | null,
): Promise<{ lines: string[]; consensus: TwelveConsensus | null }> {
  let consensus: TwelveConsensus | null = consensusCache ?? null;
  const tasks = keys.map(async (key): Promise<string> => {
    try {
      switch (key) {
        case "consensus": {
          if (!consensus) {
            const r = await api.twelveConsensus(symbol);
            consensus = r.ok ? (r.consensus ?? null) : null;
          }
          if (!consensus) return "12系统共识：暂无数据";
          const p = consensus.trade_plan;
          const planTxt = p
            ? `计划：${p.side === "short" ? "做空" : "做多"} 入场${p.entry_zone?.[0]}~${p.entry_zone?.[1]} 止损${p.stop_loss} 止盈${p.take_profit_1}${p.take_profit_2 ? `/${p.take_profit_2}` : ""} RR${p.rr ?? "—"}`
            : `无可执行计划（${consensus.plan_status?.reason ?? "中性/分歧"}）`;
          return `12系统共识：${DIR_CN[consensus.direction] ?? consensus.direction}（置信度 ${Math.round((consensus.confidence ?? 0) * 100)}%，投票 多${consensus.votes?.bullish ?? "?"}/空${consensus.votes?.bearish ?? "?"}/中性${consensus.votes?.neutral ?? "?"}）；${planTxt}`;
        }
        case "sentiment": {
          const r = await api.sentiment(symbol);
          if (!r.ok) return "供需情绪：暂无数据";
          return `供需情绪：${r.headline ?? `${DIR_CN[r.bias ?? "neutral"]} 评分 ${r.score}`}${r.warnings?.length ? `；警示：${r.warnings.join("；")}` : ""}`;
        }
        case "intel": {
          const r = await api.marketIntel();
          if (!r.ok) return "市场情报：暂无数据";
          const fr = r.funding_rate?.[symbol];
          const parts = [
            fr != null ? `资金费率 ${(fr * 100).toFixed(4)}%` : null,
            r.oi ? `OI ${r.oi.change_pct != null ? `${r.oi.change_pct > 0 ? "+" : ""}${r.oi.change_pct}%` : r.oi.value}` : null,
            r.fng ? `恐贪 ${r.fng.value}（${r.fng.classification}）` : null,
            r.long_short ? `多空比 ${r.long_short.ratio}` : null,
          ].filter(Boolean);
          return `市场情报：${parts.length ? parts.join("，") : "各源暂无数据"}`;
        }
        case "tape": {
          const r = await api.tapeFlow(symbol, 15);
          if (!r.ok || !r.active || !r.verdict) return "盘口主体：WS 数据未就绪（不可用）";
          return `盘口主体（15m）：${r.verdict.dominant_cn}主导，${r.verdict.action}；${r.verdict.note}`;
        }
        case "reversal": {
          const r = await api.reversalScore(symbol, "15m");
          if (!r.ok) return "反转评分：暂无数据";
          return `反转评分（15m）：${r.satisfied}/${r.maxScore} ${r.verdict}${r.direction !== "none" ? `（${DIR_CN[r.direction]}）` : ""}；${r.note}`;
        }
      }
    } catch {
      const label = SIGNAL_SOURCES.find((s) => s.key === key)?.label ?? key;
      return `${label}：拉取失败（不可用）`;
    }
  });
  const lines = await Promise.all(tasks);
  return { lines, consensus };
}

function buildPrompt(p: AiAdviceParams, digestLines: string[]): string {
  const preset = RISK_PRESETS[p.riskPreset];
  const marginUsdt = ((p.capital * p.pctOfCapital) / 100).toFixed(2);
  return [
    "你是合约交易风控助手。基于下列市况为用户产出一份可执行的合约开仓建议。",
    "只输出一个 JSON 对象——不要 markdown 代码块、不要任何解释文字。",
    "",
    `【用户参数】币种 ${p.symbol}；本金 ${p.capital} USDT；拟用下单比例 ${p.pctOfCapital}%（保证金约 ${marginUsdt} USDT）；拟用杠杆 ${p.leverage}x；风险偏好：${preset.label}（单笔亏损不超过本金 ${preset.riskPct}%）。`,
    `【市况】现价 ${p.price ?? "未知"}。`,
    ...digestLines.map((l) => `- ${l}`),
    "",
    'JSON 字段模板：{"side":"long|short|wait","entry_low":0,"entry_high":0,"stop_loss":0,"take_profit_1":0,"take_profit_2":0,"leverage":10,"position_pct":10,"risk_level":"low|medium|high","reason":"120字内中文大白话"}',
    "要求：1) 止损必须设在估算强平价内侧（强平距离≈100/杠杆%）；2) 建议杠杆和仓位要让止损触发时亏损不超过用户风险偏好；3) 信号分歧大或不宜开仓时 side 用 wait 并在 reason 说明；4) 点位使用绝对价格。",
  ].join("\n");
}

/** 从 LLM 回复中提取并校验 JSON；失败抛错（调用方走降级） */
export function parseAdviceJson(text: string): Omit<AiTradeAdvice, "source"> {
  // 容错：剥 ```json fence，截取首个 { 到最后一个 }
  const stripped = text.replace(/```(?:json)?/gi, "");
  const start = stripped.indexOf("{");
  const end = stripped.lastIndexOf("}");
  if (start < 0 || end <= start) throw new Error("回复中未找到 JSON");
  const obj = JSON.parse(stripped.slice(start, end + 1)) as Record<string, unknown>;

  const side = obj.side;
  if (side !== "long" && side !== "short" && side !== "wait") {
    throw new Error(`side 字段非法：${String(side)}`);
  }
  const stopLoss = toNum(obj.stop_loss);
  const entryLow = toNum(obj.entry_low);
  const entryHigh = toNum(obj.entry_high);
  if (side !== "wait" && (stopLoss == null || (entryLow == null && entryHigh == null))) {
    throw new Error("非观望建议缺少入场/止损点位");
  }
  const riskLevel =
    obj.risk_level === "low" || obj.risk_level === "medium" || obj.risk_level === "high"
      ? obj.risk_level
      : "medium";
  return {
    side,
    entryLow,
    entryHigh: entryHigh ?? entryLow,
    stopLoss,
    takeProfit1: toNum(obj.take_profit_1),
    takeProfit2: toNum(obj.take_profit_2),
    leverage: clampLev(toNum(obj.leverage)),
    positionPct: clampPct(toNum(obj.position_pct)),
    riskLevel,
    reason: typeof obj.reason === "string" && obj.reason.trim() ? obj.reason.trim() : "（AI 未给出理由）",
  };
}

/** 本地规则降级：12 系统共识计划 + 风控公式，产出与 LLM 同构的建议 */
export function localAdvice(
  consensus: TwelveConsensus | null,
  price: number | null,
  p: Pick<AiAdviceParams, "leverage" | "riskPreset">,
): Omit<AiTradeAdvice, "source" | "note"> {
  const plan = consensus?.trade_plan;
  const planState = consensus?.plan_status?.state;
  if (!consensus || !plan || planState === "neutral" || !toNum(plan.stop_loss)) {
    return {
      side: "wait",
      entryLow: null,
      entryHigh: null,
      stopLoss: null,
      takeProfit1: null,
      takeProfit2: null,
      leverage: null,
      positionPct: null,
      riskLevel: "medium",
      reason: consensus
        ? `12系统共识${DIR_CN[consensus.direction] ?? "中性"}但无可执行计划（${consensus.plan_status?.reason ?? "中性/分歧"}），建议观望等待明确信号。`
        : "共识数据不可用，无法生成本地建议，请稍后重试或检查后端。",
    };
  }

  const side: "long" | "short" =
    plan.side ?? (consensus.direction === "bearish" ? "short" : "long");
  const zone = Array.isArray(plan.entry_zone) ? plan.entry_zone.filter((v) => toNum(v)) : [];
  const entryLow = zone.length ? Math.min(...zone) : (price ?? null);
  const entryHigh = zone.length ? Math.max(...zone) : (price ?? null);
  const entryMid =
    entryLow != null && entryHigh != null ? (entryLow + entryHigh) / 2 : (price ?? 0);
  const sl = plan.stop_loss;
  const slDistPct = entryMid > 0 ? (Math.abs(entryMid - sl) / entryMid) * 100 : 0;

  // 杠杆：不超过止损距离决定的安全上限，也不放大用户拟用值
  const safeLev = maxSafeLeverage(slDistPct);
  const lev = Math.max(1, Math.min(p.leverage, safeLev));
  const preset = RISK_PRESETS[p.riskPreset];
  const positionPct = marginPctForRisk(preset.riskPct, lev, slDistPct);

  const conf = consensus.confidence ?? 0;
  const riskLevel: AiTradeAdvice["riskLevel"] =
    planState === "watch" ? "high" : conf >= 0.7 ? "low" : conf >= 0.5 ? "medium" : "high";

  const reasonParts = [
    `12系统共识${DIR_CN[consensus.direction] ?? consensus.direction}（置信度 ${Math.round(conf * 100)}%${plan.source_tf ? `，计划依据 ${plan.source_tf}` : ""}）`,
    `止损距入场约 ${slDistPct.toFixed(2)}%，安全杠杆上限 ${safeLev}x，按${preset.label}偏好（单笔风险 ${preset.riskPct}%）反推下单比例`,
    planState === "watch" ? `注意：计划状态为观望级（${consensus.plan_status?.reason ?? "RR/结构不达标"}）` : null,
  ].filter(Boolean);

  return {
    side,
    entryLow,
    entryHigh,
    stopLoss: sl,
    takeProfit1: toNum(plan.take_profit_1),
    takeProfit2: toNum(plan.take_profit_2),
    leverage: lev,
    positionPct,
    riskLevel,
    reason: `${reasonParts.join("；")}。`,
  };
}

/** AI 推荐主入口：LLM 优先，任何失败回落本地规则（永不抛错） */
export async function runAiTradeAdvice(p: AiAdviceParams): Promise<AiTradeAdvice> {
  const { lines, consensus } = await buildSignalDigest(
    p.symbol,
    p.sources,
    p.consensusCache,
  );
  try {
    const prompt = buildPrompt(p, lines);
    const res = await api.ask(prompt, p.symbol);
    if (res.engine !== "llm") {
      throw new Error("后端 LLM 未配置（规则引擎无法输出结构化建议）");
    }
    return { ...parseAdviceJson(res.answer), source: "llm" };
  } catch (e) {
    const msg = e instanceof Error ? e.message : "未知错误";
    return {
      ...localAdvice(consensus, p.price, p),
      source: "local-rule",
      note: `AI 不可用（${msg}），已用本地规则生成`,
    };
  }
}
