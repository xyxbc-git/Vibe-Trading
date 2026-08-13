/**
 * R1 热修回归：裁决字段防御性格式化。
 * 崩溃根因：真实后端 verdict.items[].detail 为对象（{direction, confidence} 等），
 * 直接作为 React child 渲染抛「Objects are not valid as a React child」。
 * 样本取自 2026-08-13 真后端 POST /api/mentor/plan 实测响应（字段形态逐项覆盖）。
 */
import { describe, expect, it } from "vitest";
import { fmtEvidence, normalizeVerdict } from "@/api/mentor";

/** 真后端实测响应 verdict 段（节选自 plan_id=4 的原始 JSON） */
const REAL_VERDICT = {
  light: "green",
  score: 70.0,
  items: [
    {
      key: "trend",
      level: "warn",
      weight: 30,
      evidence: "多周期共识看涨（加权分 +0.320，置信度 49%），方向证据还不扎实",
      detail: { direction: "bullish", confidence: 0.493 }, // ← 崩溃现场的对象形态
    },
    {
      key: "risk",
      level: "pass",
      weight: 25,
      evidence: "RR=2.50（配置门槛 2.0）",
      detail: {
        available: true, rr: 2.5, sl_dist_pct: 2.0, tp_dist_pct: 5.0,
        toll_ratio: 0.05, fee_pct: 0.05, plan_min_rr: 2.0,
      },
    },
    {
      key: "levels",
      level: "warn",
      weight: 20,
      evidence: "SL 在关键位内侧",
      detail: { warns: ["SL 在最近关键位内侧"], passes: ["路径无强磁吸位阻挡"] },
    },
    { key: "structure", level: "pass", weight: 15, evidence: "顺势计划", detail: { against: false, reversal: 0 } },
    { key: "micro", level: "warn", weight: 10, evidence: "Delta 中性", detail: {} },
  ],
  summary: "绿灯（70 分）：证据结构成立",
  cooldown_min: 0,
  vetoes: [],
  as_of: 1786593116.0286012,
};

describe("fmtEvidence（任意后端形态 → 可渲染字符串）", () => {
  it("对象 detail（崩溃样本 {direction, confidence}）转人话", () => {
    const s = fmtEvidence({ direction: "bullish", confidence: 0.493 });
    expect(typeof s).toBe("string");
    expect(s).toContain("方向 看涨");
    expect(s).toContain("置信度 49%");
  });

  it("数值对象保留可读精度", () => {
    const s = fmtEvidence({ rr: 2.5, sl_dist_pct: 2.0, toll_ratio: 0.05 });
    expect(s).toContain("盈亏比 2.5");
    expect(s).toContain("止损距离% 2");
    expect(s).toContain("过路费占比 0.05");
  });

  it("嵌套数组（warns/passes）分号连接", () => {
    const s = fmtEvidence({ warns: ["A", "B"], passes: ["C"] });
    expect(s).toContain("警示 A；B");
    expect(s).toContain("通过 C");
  });

  it("空对象/null/undefined/空串 → —", () => {
    expect(fmtEvidence({})).toBe("—");
    expect(fmtEvidence(null)).toBe("—");
    expect(fmtEvidence(undefined)).toBe("—");
    expect(fmtEvidence("  ")).toBe("—");
  });

  it("string/number/boolean 直出", () => {
    expect(fmtEvidence("正常文案")).toBe("正常文案");
    expect(fmtEvidence(70)).toBe("70");
    expect(fmtEvidence(0.4934)).toBe("0.4934");
    expect(fmtEvidence(true)).toBe("是");
    expect(fmtEvidence(false)).toBe("否");
  });
});

describe("normalizeVerdict（后端 verdict 入口归一化）", () => {
  it("真后端实测响应归一化后所有渲染字段均为安全类型", () => {
    const v = normalizeVerdict(REAL_VERDICT);
    expect(v).not.toBeNull();
    expect(v!.light).toBe("green");
    expect(v!.score).toBe(70);
    expect(typeof v!.summary).toBe("string");
    expect(v!.cooldown_min).toBeUndefined(); // 0 分钟 = 无冷静期
    expect(v!.items).toHaveLength(5);
    // 每条 evidence/detail 过 fmtEvidence 后都是字符串（渲染层不再收到对象）
    for (const it of v!.items) {
      expect(typeof fmtEvidence(it.evidence)).toBe("string");
      expect(typeof fmtEvidence(it.detail)).toBe("string");
    }
  });

  it("light 非法值回退 yellow，score 非数字回退 0，items 缺失回退空数组", () => {
    const v = normalizeVerdict({ light: "purple", score: "abc", summary: { a: 1 } });
    expect(v!.light).toBe("yellow");
    expect(v!.score).toBe(0);
    expect(v!.items).toEqual([]);
    expect(typeof v!.summary).toBe("string");
  });

  it("红灯冷静期保留分钟数", () => {
    const v = normalizeVerdict({ light: "red", score: 20, items: [], summary: "x", cooldown_min: 15 });
    expect(v!.cooldown_min).toBe(15);
  });

  it("非对象输入返回 null（调用方走 mock 兜底）", () => {
    expect(normalizeVerdict(null)).toBeNull();
    expect(normalizeVerdict("bad")).toBeNull();
  });
});
