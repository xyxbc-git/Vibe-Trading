import { describe, it, expect } from "vitest";
import { parseAdviceJson, localAdvice } from "../aiTradeAdvice";
import type { TwelveConsensus } from "../../api/client";

// AI 建议解析 + 本地规则降级的结构锁定。
describe("parseAdviceJson", () => {
  it("解析纯 JSON 与带 markdown fence 的回复", () => {
    const body =
      '{"side":"long","entry_low":49800,"entry_high":50200,"stop_loss":49000,"take_profit_1":51500,"take_profit_2":52800,"leverage":15,"position_pct":8,"risk_level":"medium","reason":"多周期共振向上"}';
    for (const text of [body, `\`\`\`json\n${body}\n\`\`\``, `前置说明\n${body}\n后置`]) {
      const a = parseAdviceJson(text);
      expect(a.side).toBe("long");
      expect(a.stopLoss).toBe(49000);
      expect(a.leverage).toBe(15);
      expect(a.positionPct).toBe(8);
      expect(a.reason).toContain("共振");
    }
  });

  it("杠杆/比例夹紧到合法区间", () => {
    const a = parseAdviceJson(
      '{"side":"short","entry_low":100,"stop_loss":103,"leverage":500,"position_pct":250,"risk_level":"high","reason":"x"}',
    );
    expect(a.leverage).toBe(125);
    expect(a.positionPct).toBe(100);
  });

  it("wait 建议允许缺点位；非 wait 缺止损抛错", () => {
    const w = parseAdviceJson('{"side":"wait","risk_level":"low","reason":"分歧大"}');
    expect(w.side).toBe("wait");
    expect(() =>
      parseAdviceJson('{"side":"long","entry_low":100,"risk_level":"low","reason":"x"}'),
    ).toThrow();
  });

  it("side 非法或无 JSON 抛错", () => {
    expect(() => parseAdviceJson('{"side":"hold","reason":"x"}')).toThrow();
    expect(() => parseAdviceJson("纯文字回复")).toThrow();
  });
});

function mkConsensus(overrides?: Partial<TwelveConsensus>): TwelveConsensus {
  return {
    direction: "bullish",
    confidence: 0.72,
    votes: { bullish: 8, bearish: 2, neutral: 2 },
    trade_plan: {
      side: "long",
      entry_zone: [49_800, 50_200],
      stop_loss: 49_000,
      take_profit_1: 51_500,
      take_profit_2: 52_800,
      rr: 2.1,
      source_tf: "4h",
    },
    plan_status: { state: "ok", reason: null },
    ...overrides,
  };
}

describe("localAdvice（本地规则降级）", () => {
  it("有共识计划：产出同构建议，杠杆不超安全上限", () => {
    const a = localAdvice(mkConsensus(), 50_000, {
      leverage: 100,
      riskPreset: "balanced",
    });
    expect(a.side).toBe("long");
    expect(a.stopLoss).toBe(49_000);
    expect(a.takeProfit1).toBe(51_500);
    // 入场中价 50000，止损距离 2% → 安全上限 floor(100/(2×1.5+0.5)) = 28x
    expect(a.leverage).toBe(28);
    // 稳健档 2% 风险：比例 = 2×100/(28×2) ≈ 3.57%
    expect(a.positionPct).toBeCloseTo(3.57, 1);
    expect(a.riskLevel).toBe("low");
    expect(a.reason).toContain("12系统共识");
  });

  it("无计划/中性：建议观望", () => {
    const a = localAdvice(
      mkConsensus({ trade_plan: null, plan_status: { state: "neutral", reason: "分歧" } }),
      50_000,
      { leverage: 10, riskPreset: "conservative" },
    );
    expect(a.side).toBe("wait");
    expect(a.stopLoss).toBeNull();
  });

  it("共识不可用：观望并提示数据缺失", () => {
    const a = localAdvice(null, null, { leverage: 10, riskPreset: "aggressive" });
    expect(a.side).toBe("wait");
    expect(a.reason).toContain("不可用");
  });

  it("watch 状态计划：风险等级标高", () => {
    const a = localAdvice(
      mkConsensus({ plan_status: { state: "watch", reason: "RR 不达标" } }),
      50_000,
      { leverage: 10, riskPreset: "balanced" },
    );
    expect(a.riskLevel).toBe("high");
  });
});
