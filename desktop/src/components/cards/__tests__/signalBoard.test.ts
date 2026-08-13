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
