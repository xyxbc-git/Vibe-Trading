// 信号矩阵纯函数测试：planTriggerState 计划触发状态（任务S追加）。
// 背景（用户反馈原场景）：现价 1888.20，卡片推荐入场 1882.26（做空）——
// 结构化入场是挂单语义（等价格来找你），但价格越过后旧点位必须标失效。
import { describe, expect, it } from "vitest";
import { planTriggerState } from "../SignalBoard";

const plan = (entry: number, tp: number) => ({ entry, take_profit: tp });

describe("planTriggerState 计划触发状态", () => {
  it("用户反馈原场景：空单 entry 1882.26 / 现价 1888.20 → 等待触发（挂单未到位）", () => {
    const st = planTriggerState(plan(1882.26, 1860), "short", 1888.2);
    expect(st?.kind).toBe("waiting");
    expect(st!.distPct).toBeCloseTo(0.3156, 3); // 现价高于入场 0.32%
  });

  it("接近触发：距入场 ≤0.15%", () => {
    expect(planTriggerState(plan(1890, 1860), "short", 1888.2)?.kind).toBe("near");
    expect(planTriggerState(plan(1887, 1910), "long", 1888.2)?.kind).toBe("near");
  });

  it("已越过：现价朝止盈方向偏离入场 ≥1%（挂单追不回来）", () => {
    // 空单：价格已跌破入场下方 1%+，没给入场机会
    expect(planTriggerState(plan(1910, 1850), "short", 1888.2)?.kind).toBe("passed");
    // 多单：价格已涨过入场上方 1%+
    expect(planTriggerState(plan(1868, 1930), "long", 1888.2)?.kind).toBe("passed");
  });

  it("已越过：现价已达/越过止盈位（无论离入场多近）", () => {
    expect(planTriggerState(plan(1888, 1888.5), "long", 1889)?.kind).toBe("passed");
    expect(planTriggerState(plan(1889, 1888.5), "short", 1888)?.kind).toBe("passed");
  });

  it("等待触发：多单等回踩（现价在入场上方 0.15%~1% 之间）", () => {
    const st = planTriggerState(plan(1880, 1920), "long", 1888.2);
    expect(st?.kind).toBe("waiting");
    expect(st!.distPct).toBeGreaterThan(0);
  });

  it("边界防御：方向/价格缺失返回 null，不砸渲染", () => {
    expect(planTriggerState(plan(1880, 1920), null, 1888)).toBeNull();
    expect(planTriggerState(plan(1880, 1920), "long", null)).toBeNull();
    expect(planTriggerState(plan(1880, 1920), "long", 0)).toBeNull();
    expect(planTriggerState({ entry: NaN, take_profit: 1900 }, "long", 1888)).toBeNull();
    // TP 缺失/非法：不判 tpReached，仍按距离判定
    expect(planTriggerState({ entry: 1888 }, "long", 1888.5)?.kind).toBe("near");
  });
});

// [S3 蓄势雷达] 中性卡触发位一行话（用户场景：ETHUSDT 4h 全中性，
// 卡片必须亮出「涨破 1937.67 转看涨 · 距 2.3%」这类可盯价位）
import { triggerLine } from "../SignalBoard";

describe("triggerLine 蓄势雷达触发位文案", () => {
  it("看涨触发：涨到 + 转看涨 + 距离百分比", () => {
    const s = triggerLine({
      side: "bullish", price: 1937.67, dist_pct: 2.34,
      desc: "突破20日高 1937.67 转看涨（顺势做多入场）",
    });
    expect(s).toContain("涨到");
    expect(s).toContain("转看涨");
    expect(s).toContain("距 2.34%");
  });

  it("看跌触发：跌到 + 转看跌", () => {
    const s = triggerLine({
      side: "bearish", price: 1853.22, dist_pct: 1.5,
      desc: "跌破20日低 1853.22 转看跌（顺势做空入场）",
    });
    expect(s).toContain("跌到");
    expect(s).toContain("转看跌");
    expect(s).toContain("距 1.50%");
  });

  it("dist_pct 缺失时省略距离段，不砸渲染", () => {
    const s = triggerLine({ side: "bullish", price: 100, dist_pct: null, desc: "x" });
    expect(s).toContain("转看涨");
    expect(s).not.toContain("距");
  });
});
