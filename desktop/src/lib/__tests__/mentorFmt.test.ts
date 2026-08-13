/**
 * R1 热修回归：裁决字段防御性格式化。
 * 崩溃根因：真实后端 verdict.items[].detail 为对象（{direction, confidence} 等），
 * 直接作为 React child 渲染抛「Objects are not valid as a React child」。
 * 样本取自 2026-08-13 真后端 POST /api/mentor/plan 实测响应（字段形态逐项覆盖）。
 */
import { describe, expect, it } from "vitest";
import {
  fmtEvidence,
  normalizeVerdict,
  normalizeStats,
  normalizeRule,
  lossStreak,
  type MentorPlanRow,
} from "@/api/mentor";

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

  it("V2：rules 区段归一化透传（契约形态 key/level）", () => {
    const v = normalizeVerdict({
      light: "green", score: 90, items: [], summary: "ok",
      rules: [
        { key: "min_rr", level: "pass", evidence: "RR 2.5 ≥ 1.5" },
        { key: "emotion_cap", level: "fail", evidence: { emo: 5 } }, // 对象证据也不崩
      ],
    });
    expect(v!.rules).toHaveLength(2);
    expect(v!.rules![0].level).toBe("pass");
    expect(typeof fmtEvidence(v!.rules![1].evidence)).toBe("string");
  });

  it("V4：rules 区段真后端实测形态（rule_id/status/title/evidence）不再整体降级 warn", () => {
    // 2026-08-13 真后端 POST /api/mentor/plan 响应的 verdict.rules 节选原样
    const v = normalizeVerdict({
      light: "green", score: 70, items: [], summary: "ok",
      rules: [
        { rule_id: "R01", title: "红灯单必须过冷静期再提交", status: "pass", evidence: "近期无红灯裁决，无需冷静期" },
        { rule_id: "R03", title: "盈亏比低于 2 不开单", status: "fail", evidence: "RR=1.20 < 2，赔率不达标" },
      ],
    });
    expect(v!.rules).toHaveLength(2);
    expect(v!.rules![0].key).toBe("R01");
    expect(v!.rules![0].level).toBe("pass");   // status → level 映射
    expect(v!.rules![1].level).toBe("fail");
    expect(fmtEvidence(v!.rules![0].detail)).toBe("红灯单必须过冷静期再提交"); // title 落 detail
  });
});

describe("normalizeRule（V4 军规行真实契约归一化）", () => {
  it("真后端形态：rule_id/title/rtype/params(dict)/enabled(int)", () => {
    // 2026-08-13 真后端 GET /api/mentor/rules 实测行原样
    const r = normalizeRule(
      {
        rule_id: "R04",
        title: "方向必须顺 30m/1h/4h 中至少 2 个周期（5m 只做入场时机）",
        rtype: "mtf_align",
        params: { tfs: ["30m", "1h", "4h"], min_agree: 2 },
        enabled: 1,
        updated_ts: 1786599966.946831,
      },
      3,
    );
    expect(r.id).toBe("R04");
    expect(r.title).toContain("方向必须顺");   // 不再是「规则 4」占位
    expect(r.rtype).toBe("mtf_align");
    expect(r.params.min_agree).toBe(2);
    expect(r.enabled).toBe(true);              // int 1 → boolean
    expect(r.builtin).toBe(true);              // R 前缀 = 服务端种子
  });

  it("自定义规则（U- 前缀）builtin=false；enabled 0 → false", () => {
    const r = normalizeRule(
      { rule_id: "U-1786600000", title: "跌破日线 MA20 不做多", rtype: "custom", params: {}, enabled: 0 },
      12,
    );
    expect(r.builtin).toBe(false);
    expect(r.enabled).toBe(false);
  });

  it("字段缺失兜底：无 title 落「规则 N」占位、params 非法归空对象", () => {
    const r = normalizeRule({ rule_id: "X1", params: "bad" }, 0);
    expect(r.title).toBe("规则 1");
    expect(r.params).toEqual({});
  });
});

describe("normalizeStats（真后端两种形态归一化）", () => {
  it("实测形态：by_light 按灯色键控对象 + followed/ignored + n/closed/win_rate 命名", () => {
    // 2026-08-13 真后端 GET /api/mentor/stats 实测响应原样
    const s = normalizeStats({
      ok: true, days: 90, total: 6,
      by_light: {
        green: { n: 1, closed: 0, win_rate: null, avg_pnl_pct: null },
        yellow: { n: 4, closed: 1, win_rate: null, avg_pnl_pct: null },
        red: { n: 1, closed: 0, win_rate: null, avg_pnl_pct: null },
      },
      followed: { n: 1, closed: 1, win_rate: null, avg_pnl_pct: null },
      ignored: { n: 0, closed: 0, win_rate: null, avg_pnl_pct: null },
      note: null,
    });
    expect(s).not.toBeNull();
    expect(s!.by_light).toHaveLength(3);
    const yellow = s!.by_light.find((b) => b.light === "yellow")!;
    expect(yellow.plans).toBe(4);
    expect(yellow.executed).toBe(1);
    expect(yellow.win_rate_pct).toBeNull();
    expect(s!.followed.trades).toBe(1);
    expect(s!.not_followed.trades).toBe(0);
  });

  it("契约形态：by_light 数组 + not_followed", () => {
    const s = normalizeStats({
      ok: true,
      by_light: [
        { light: "green", plans: 5, executed: 3, wins: 2, losses: 1, win_rate_pct: 66.7, total_pnl_pct: 4.2 },
      ],
      followed: { trades: 3, win_rate_pct: 66.7, total_pnl_pct: 4.2 },
      not_followed: { trades: 1, win_rate_pct: 0, total_pnl_pct: -2.1 },
    });
    expect(s!.by_light[0].plans).toBe(5);
    expect(s!.not_followed.total_pnl_pct).toBe(-2.1);
  });

  it("win_rate 0-1 小数自动转百分比", () => {
    const s = normalizeStats({
      ok: true,
      by_light: { green: { n: 2, closed: 2, win_rate: 0.5 }, yellow: {}, red: {} },
      followed: { n: 2, closed: 2, win_rate: 1 },
      ignored: {},
    });
    expect(s!.by_light.find((b) => b.light === "green")!.win_rate_pct).toBe(50);
    expect(s!.followed.win_rate_pct).toBe(100);
  });

  it("ok:false / 缺 by_light → null（调用方落 mock）", () => {
    expect(normalizeStats({ ok: false })).toBeNull();
    expect(normalizeStats({ ok: true })).toBeNull();
  });
});

describe("lossStreak（今日连亏计数）", () => {
  const row = (t: string, result: "win" | "loss"): MentorPlanRow =>
    ({
      id: t, created_at: `2026-08-13 ${t}`, symbol: "BTCUSDT", direction: "long",
      entry: 1, stop_loss: 0.9, take_profit: 1.2,
      outcome: { result, followed: true },
    }) as MentorPlanRow;

  it("尾部连续亏损计数，胜单打断", () => {
    expect(lossStreak([row("09:00:00", "loss"), row("10:00:00", "loss")])).toBe(2);
    expect(lossStreak([row("09:00:00", "loss"), row("10:00:00", "win"), row("11:00:00", "loss")])).toBe(1);
    expect(lossStreak([row("09:00:00", "loss"), row("10:00:00", "win")])).toBe(0);
    expect(lossStreak([])).toBe(0);
  });
});
