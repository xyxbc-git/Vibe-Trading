import type { FootprintBar } from "@/types/footprint";
import { fmtK, fmtPrice, isOhlcOnly } from "../renderer";
import { detectSignals } from "./signals";
import type { SysConsensusLite } from "./systemSignal";
import type { VolumeProfile } from "../profile";

/** 智能解读条目：把最近 N 根柱的订单流数据翻译成人话 */
export interface Insight {
  id: string;
  category: "bias" | "level" | "risk" | "system" | "profile";
  /** 结论（一句人话） */
  text: string;
  /** 置信度 0-1（规则强度粗估，非统计学概率） */
  confidence: number;
  /** 点开可见：触发这条结论的数据依据 */
  basis: string;
  tone: "bull" | "bear" | "warn" | "neutral";
}

const sum = (xs: number[]): number => xs.reduce((a, b) => a + b, 0);
const mean = (xs: number[]): number => (xs.length === 0 ? 0 : sum(xs) / xs.length);

/**
 * 纯前端规则引擎：读最近 window 根柱，产出「当前画面解读」。
 * 覆盖：多空力量（delta/cumDelta 趋势）、关键价位（POC 支撑压力）、
 * 风险提示（卖压衰竭 / 失衡不涨提防出货 / 放量滞涨等）。
 */
export function buildInsights(
  bars: FootprintBar[],
  tick: number,
  window = 10,
  sysCons: SysConsensusLite | null = null,
  profile: VolumeProfile | null = null,
): Insight[] {
  const n = bars.length;
  if (n < 5) return [];
  // OHLC-only 柱（大周期远期回填）无逐笔 delta 口径，剔除防统计失真
  const win = bars.slice(Math.max(0, n - window)).filter((b) => !isOhlcOnly(b));
  if (win.length < 3) return [];
  const last = win[win.length - 1];
  const out: Insight[] = [];

  // ---------- 1. 多空力量：窗口 delta 合计 + 方向一致性 ----------
  const deltas = win.map((b) => b.delta);
  const dSum = sum(deltas);
  const posRatio = deltas.filter((d) => d > 0).length / win.length;
  const volSum = Math.max(1, sum(win.map((b) => b.totalVol)));
  const dStrength = Math.abs(dSum) / volSum; // 净主动量占总量比

  if (dStrength >= 0.04) {
    const bull = dSum > 0;
    out.push({
      id: "bias",
      category: "bias",
      tone: bull ? "bull" : "bear",
      text: bull
        ? `最近 ${win.length} 根柱主动买累计 +${fmtK(dSum)}，买方占优，短线偏多。`
        : `最近 ${win.length} 根柱主动卖累计 ${fmtK(dSum)}，卖方占优，短线偏空。`,
      confidence: Math.min(0.9, 0.4 + dStrength * 4 + Math.abs(posRatio - 0.5)),
      basis: `窗口 Delta 合计 ${fmtK(dSum)}（占总成交 ${(dStrength * 100).toFixed(1)}%）；${Math.round(posRatio * win.length)}/${win.length} 根柱 Delta 为正；累计Δ 从 ${fmtK(win[0].cumDelta)} 到 ${fmtK(last.cumDelta)}。`,
    });
  } else {
    out.push({
      id: "bias",
      category: "bias",
      tone: "neutral",
      text: `最近 ${win.length} 根柱买卖力量接近（净差仅占总量 ${(dStrength * 100).toFixed(1)}%），方向不明，观望为主。`,
      confidence: 0.6,
      basis: `窗口 Delta 合计 ${fmtK(dSum)}，总成交 ${fmtK(volSum)}；多空柱数 ${Math.round(posRatio * win.length)}:${win.length - Math.round(posRatio * win.length)}。`,
    });
  }

  // ---------- 1.5 系统信号共振/分歧（12 系统共识 × 足迹多空） ----------
  // 接口不可用（sysCons=null）时静默跳过；共识中性且足迹也无方向时无信息量，不显示
  if (sysCons && sysCons.direction !== "neutral") {
    const fpBull = dStrength >= 0.04 ? dSum > 0 : null; // null = 足迹无明确方向
    const sysBull = sysCons.direction === "bullish";
    const sysLabel = sysBull ? "看多" : "看空";
    const voteStr = `${sysCons.votes.bullish}多/${sysCons.votes.bearish}空/${sysCons.votes.neutral}中`;
    if (fpBull === null) {
      out.push({
        id: "system",
        category: "system",
        tone: "neutral",
        text: `12 系统共识${sysLabel}（信心 ${Math.round(sysCons.confidence)}%），但足迹端买卖力量还没跟上——等订单流同向确认再动手更稳。`,
        confidence: 0.5,
        basis: `系统共识 ${sysLabel}，投票 ${voteStr}；足迹窗口净 Delta 仅占总量 ${(dStrength * 100).toFixed(1)}%（<4% 视为无方向）。`,
      });
    } else if (fpBull === sysBull) {
      out.push({
        id: "system",
        category: "system",
        tone: sysBull ? "bull" : "bear",
        text: `信号共振：足迹订单流偏${sysBull ? "多" : "空"} + 12 系统共识${sysLabel}（信心 ${Math.round(sysCons.confidence)}%），两套独立证据同向，信号可信度提升。`,
        confidence: Math.min(0.9, 0.55 + sysCons.confidence / 200),
        basis: `足迹窗口 Delta 合计 ${fmtK(dSum)}（${dSum > 0 ? "买" : "卖"}方占优）；系统共识投票 ${voteStr}。两者独立计算、方向一致。`,
      });
    } else {
      out.push({
        id: "system",
        category: "system",
        tone: "warn",
        text: `信号分歧：足迹订单流偏${fpBull ? "多" : "空"}，但 12 系统共识${sysLabel}——短线资金与系统面打架，此时进场容易两头挨打，建议观望或轻仓。`,
        confidence: 0.6,
        basis: `足迹窗口 Delta 合计 ${fmtK(dSum)} 偏${fpBull ? "多" : "空"}；系统共识 ${sysLabel}（投票 ${voteStr}，信心 ${Math.round(sysCons.confidence)}%）。方向相反 = 分歧。`,
      });
    }
  }

  // ---------- 2. 关键价位：近期高量 POC 与当前价的关系 ----------
  // 取窗口内成交量最大的那根柱的 POC 作为「近期主战场」
  const keyBar = win.reduce((a, b) => (b.totalVol > a.totalVol ? b : a));
  const poc = keyBar.poc;
  const dist = last.close - poc;
  const distTicks = Math.abs(dist) / Math.max(tick, 1e-9);
  if (distTicks <= 2) {
    out.push({
      id: "level",
      category: "level",
      tone: "neutral",
      text: `当前价 ${fmtPrice(last.close, tick)} 正处于近期成交最密集的价位（POC ${fmtPrice(poc, tick)}）附近，多空正在此争夺，突破方向未定。`,
      confidence: 0.65,
      basis: `窗口内量最大柱（${fmtK(keyBar.totalVol)}）的 POC=${fmtPrice(poc, tick)}；当前价距离 ${distTicks.toFixed(1)} 个价位档。`,
    });
  } else {
    const above = dist > 0;
    out.push({
      id: "level",
      category: "level",
      tone: above ? "bull" : "bear",
      text: above
        ? `当前价在近期密集成交区（POC ${fmtPrice(poc, tick)}）上方，该价位回踩时可视作第一道支撑。`
        : `当前价在近期密集成交区（POC ${fmtPrice(poc, tick)}）下方，该价位反弹时可视作第一道压力。`,
      confidence: 0.6,
      basis: `POC 来自窗口内量最大柱（Vol ${fmtK(keyBar.totalVol)}）；当前价 ${fmtPrice(last.close, tick)}，偏离 ${fmtK(Math.abs(dist))}（${distTicks.toFixed(0)} 档）。`,
    });
  }

  // ---------- 2.5 成交量分布（Volume Profile 开启时） ----------
  if (profile) {
    const px = last.close;
    const shapeText =
      profile.shape === "bell"
        ? "分布呈对称钟形——多空在价值区内充分换手，属于平衡市，价格倾向围绕 POC 来回摆动，追单不如高抛低吸。"
        : profile.shape === "double"
          ? `分布呈双峰（次峰 ${fmtPrice(profile.secondPeak ?? profile.poc, tick)}）——市场在两个价区各形成过一次共识，常见于趋势换挡/尾声，价格站稳哪个峰区就往哪边走。`
          : "分布头重脚轻（偏态）——单边换手更密集，价格离开密集区后容易加速。";
    let posText: string;
    let tone: Insight["tone"] = "neutral";
    if (px > profile.vah) {
      posText = `当前价 ${fmtPrice(px, tick)} 在价值区上方（VAH ${fmtPrice(profile.vah, tick)}）——多方掌控，回踩 VAH 不破可视作支撑；若跌回价值区内，警惕向 POC ${fmtPrice(profile.poc, tick)} 回归。`;
      tone = "bull";
    } else if (px < profile.val) {
      posText = `当前价 ${fmtPrice(px, tick)} 在价值区下方（VAL ${fmtPrice(profile.val, tick)}）——空方掌控，反弹到 VAL 遇阻可视作压力；若收回价值区内，可能向 POC ${fmtPrice(profile.poc, tick)} 磁吸。`;
      tone = "bear";
    } else {
      posText = `当前价 ${fmtPrice(px, tick)} 在价值区内（${fmtPrice(profile.val, tick)} ~ ${fmtPrice(profile.vah, tick)}）——处于「公允价格」地带，方向未选择，POC ${fmtPrice(profile.poc, tick)} 是多空争夺的锚。`;
    }
    out.push({
      id: "profile",
      category: "profile",
      tone,
      text: `${posText} ${shapeText}`,
      confidence: 0.6,
      basis: `分布聚合 ${profile.coveredBars} 根柱（跳过 ${profile.skippedBars} 根无逐笔柱），总量 ${fmtK(profile.totalVol)}；POC ${fmtPrice(profile.poc, tick)}，价值区 70% = ${fmtPrice(profile.val, tick)} ~ ${fmtPrice(profile.vah, tick)}${profile.hvns.length > 0 ? `；高量节点 ${profile.hvns.map((p) => fmtPrice(p, tick)).join("/")}` : ""}${profile.lvns.length > 0 ? `；低量真空 ${profile.lvns.map((p) => fmtPrice(p, tick)).join("/")}` : ""}。`,
    });
  }

  // ---------- 3. 风险提示（多条规则，命中即加入） ----------
  const half = Math.floor(win.length / 2);
  const firstHalf = win.slice(0, half);
  const secondHalf = win.slice(half);

  // 3a. 下跌但卖压衰竭
  const falling = last.close < win[0].open;
  const sellEarly = sum(firstHalf.map((b) => Math.min(0, b.delta)));
  const sellLate = sum(secondHalf.map((b) => Math.min(0, b.delta)));
  if (falling && sellLate > sellEarly * 0.55 && sellEarly < 0) {
    out.push({
      id: "risk-exhaust",
      category: "risk",
      tone: "warn",
      text: "价格仍在下跌，但卖压明显衰竭（后半段主动卖量大幅缩小），继续追空的风险偏高。",
      confidence: 0.55,
      basis: `前半窗负 Delta 合计 ${fmtK(sellEarly)}，后半窗 ${fmtK(sellLate)}（衰竭 ${Math.round((1 - sellLate / Math.min(sellEarly, -1)) * 100)}%）；期间价格 ${fmtPrice(win[0].open, tick)} → ${fmtPrice(last.close, tick)}。`,
    });
  }

  // 3b. 持续买入但价不涨 → 警惕出货
  const buySum = sum(win.map((b) => Math.max(0, b.delta)));
  const rising = last.close > win[0].open;
  if (dSum > 0 && buySum > 0 && !rising && dStrength >= 0.03) {
    out.push({
      id: "risk-absorb",
      category: "risk",
      tone: "warn",
      text: "主动买单持续进场但价格不涨（买单被上方大卖单吸收），警惕主力借买盘出货，追多需谨慎。",
      confidence: 0.55,
      basis: `窗口净 Delta +${fmtK(dSum)} 为正，但收盘 ${fmtPrice(last.close, tick)} 未高于起点 ${fmtPrice(win[0].open, tick)}——买量没换来涨幅，说明有大体量对手方在被动出货。`,
    });
  }

  // 3c. 放量滞涨/滞跌（最后一根柱）
  const avgVol = mean(win.slice(0, -1).map((b) => b.totalVol));
  const range = Math.abs(last.close - last.open) / Math.max(tick, 1e-9);
  if (last.totalVol >= avgVol * 2 && range <= 2 && avgVol > 0) {
    out.push({
      id: "risk-churn",
      category: "risk",
      tone: "warn",
      text: "最新一根柱放出大量但价格几乎没动（高换手滞涨），多空在此激烈换手，往往是变盘前兆。",
      confidence: 0.5,
      basis: `最新柱 Vol ${fmtK(last.totalVol)} ≈ 均量的 ${(last.totalVol / avgVol).toFixed(1)} 倍，但实体仅 ${range.toFixed(0)} 档。`,
    });
  }

  // 3d. 复用信号检测：把窗口内最强徽标信号提示进面板
  const sigs = detectSignals(bars, window).sort((a, b) => b.strength - a.strength);
  if (sigs.length > 0) {
    const s = sigs[0];
    out.push({
      id: `risk-signal-${s.type}`,
      category: "risk",
      tone: s.side === "buy" ? "bull" : "bear",
      text: `图上检测到「${s.title}」信号（见柱上徽标），点击徽标查看含义与风险。`,
      confidence: Math.min(0.8, s.strength),
      basis: s.what,
    });
  }

  // 全局免责（始终在最后）
  out.push({
    id: "disclaimer",
    category: "risk",
    tone: "neutral",
    text: "以上为规则引擎自动解读，信号会骗人、历史规律会失效，不构成投资建议。",
    confidence: 1,
    basis: "固定风险提示：任何单一图表信号的历史胜率都远低于 100%，请结合仓位管理与止损纪律使用。",
  });

  return out;
}
