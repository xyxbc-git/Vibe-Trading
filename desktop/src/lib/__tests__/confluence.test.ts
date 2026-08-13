// 合流仪表纯函数测试（C2）：分数滞回 / 分段色（禁红）/ 折叠态四勾抽取 /
// 冷静期倒计时 / 新鲜度 / 方向措辞（禁 BUY/SELL）/ 窄容器 / 旧响应守卫。
import { describe, expect, it } from "vitest";
import {
  COLLAPSED_CHECK_KEYS,
  collapsedChecks,
  cooldownRemaining,
  dirMeta,
  fmtCountdown,
  fmtFreshness,
  holdDisplayScore,
  isNarrowContainer,
  matchesScope,
  SCORE_HYSTERESIS,
  scoreTone,
} from "../confluence";
import { mockConfluence, type ConfluenceResponse } from "@/api/confluence";

describe("holdDisplayScore 分数滞回", () => {
  it("变化小于阈值保持旧显示值（防心电图效应）", () => {
    expect(holdDisplayScore(70, 72)).toBe(70);
    expect(holdDisplayScore(70, 74)).toBe(70);
    expect(holdDisplayScore(70, 66)).toBe(70);
  });
  it("变化达到阈值才更新", () => {
    expect(holdDisplayScore(70, 75)).toBe(75);
    expect(holdDisplayScore(70, 63)).toBe(63);
    expect(SCORE_HYSTERESIS).toBe(5);
  });
  it("首值/中性直通", () => {
    expect(holdDisplayScore(null, 62)).toBe(62);
    expect(holdDisplayScore(62, null)).toBeNull();
    expect(holdDisplayScore(null, null)).toBeNull();
  });
});

describe("scoreTone 分段色（对齐 ReversalScorePanel 惯例，禁红）", () => {
  it("≥70 金 / 40-69 蓝 / <40 灰", () => {
    expect(scoreTone(75).band).toBe("gold");
    expect(scoreTone(70).band).toBe("gold");
    expect(scoreTone(69).band).toBe("blue");
    expect(scoreTone(40).band).toBe("blue");
    expect(scoreTone(39).band).toBe("gray");
    expect(scoreTone(0).band).toBe("gray");
  });
  it("任何档位不产出红色类（红=空方向/风险语义，低分用红会被误读）", () => {
    for (const s of [0, 20, 39, 40, 55, 70, 100]) {
      expect(scoreTone(s).textCls).not.toContain("red");
    }
  });
  it("证据不足/演示数据灰显（dimmed）", () => {
    expect(scoreTone(80, { insufficient: true }).dimmed).toBe(true);
    expect(scoreTone(80, { mock: true }).dimmed).toBe(true);
    expect(scoreTone(80).dimmed).toBe(false);
    expect(scoreTone(null).dimmed).toBe(true);
  });
});

describe("dirMeta 方向措辞（方案 §五.4：禁 BUY/SELL）", () => {
  it("环境偏多/偏空/无方向共识", () => {
    expect(dirMeta("bullish").label).toBe("环境偏多");
    expect(dirMeta("bearish").label).toBe("环境偏空");
    expect(dirMeta("neutral").label).toBe("无方向共识");
  });
  it("不出现 BUY/SELL 字样", () => {
    for (const d of ["bullish", "bearish", "neutral"] as const) {
      const m = dirMeta(d);
      expect(`${m.label}${m.arrow}`).not.toMatch(/buy|sell/i);
    }
  });
});

describe("collapsedChecks 折叠态四勾（CP1 对齐四大项）", () => {
  it("从 mock 响应抽取 HTF/BOS/扫单/FVG 四态", () => {
    const resp = mockConfluence("BTCUSDT", "4h");
    const checks = collapsedChecks(resp);
    expect(checks).toHaveLength(4);
    expect(COLLAPSED_CHECK_KEYS).toEqual(["c1_htf", "c3_bos", "c4_sweep", "c5_fvg"]);
    expect(checks[0]).toBe("pass"); // c1_htf
    expect(checks[1]).toBe("pass"); // c3_bos
    expect(checks[2]).toBe("warn"); // c4_sweep
    expect(checks[3]).toBe("pass"); // c5_fvg
  });
  it("条目缺失按 skipped 兜底；空响应全 skipped", () => {
    const resp = mockConfluence("BTCUSDT", "4h");
    resp.groups = resp.groups.filter((g) => g.key !== "structure");
    const checks = collapsedChecks(resp);
    expect(checks[1]).toBe("skipped"); // c3_bos 随组消失
    expect(collapsedChecks(null)).toEqual(["skipped", "skipped", "skipped", "skipped"]);
  });
});

describe("cooldownRemaining / fmtCountdown 冷静期闸口", () => {
  it("未设置/已过期为 0，未来取整秒", () => {
    expect(cooldownRemaining(null, 1000)).toBe(0);
    expect(cooldownRemaining(undefined, 1000)).toBe(0);
    expect(cooldownRemaining(900, 1000)).toBe(0);
    expect(cooldownRemaining(1090.6, 1000)).toBe(90);
  });
  it("m:ss 与 h:mm:ss 两档", () => {
    expect(fmtCountdown(0)).toBe("0:00");
    expect(fmtCountdown(65)).toBe("1:05");
    expect(fmtCountdown(3600)).toBe("1:00:00");
    expect(fmtCountdown(3725)).toBe("1:02:05");
  });
});

describe("fmtFreshness 数据新鲜度", () => {
  it("秒/分钟/小时三档；缺失为空串", () => {
    expect(fmtFreshness(970, 1000)).toBe("30 秒前");
    expect(fmtFreshness(1000 - 120, 1000)).toBe("2 分钟前");
    expect(fmtFreshness(1000 - 7200, 1000)).toBe("2 小时前");
    expect(fmtFreshness(null)).toBe("");
    expect(fmtFreshness(undefined)).toBe("");
  });
});

describe("isNarrowContainer 窄容器降级（按 container 非 viewport）", () => {
  it("<768 判窄；未知宽度不降级", () => {
    expect(isNarrowContainer(767)).toBe(true);
    expect(isNarrowContainer(768)).toBe(false);
    expect(isNarrowContainer(1200)).toBe(false);
    expect(isNarrowContainer(null)).toBe(false);
    expect(isNarrowContainer(0)).toBe(false);
  });
});

describe("matchesScope 旧响应守卫", () => {
  it("旧币种/旧周期的慢响应不得展示到当前口径", () => {
    const resp = mockConfluence("BTCUSDT", "4h");
    expect(matchesScope(resp, "BTCUSDT", "4h")).toBe(true);
    expect(matchesScope(resp, "ETHUSDT", "4h")).toBe(false);
    expect(matchesScope(resp, "BTCUSDT", "1h")).toBe(false);
    expect(matchesScope(null, "BTCUSDT", "4h")).toBe(false);
  });
  it("后端未回填 symbol/tf 时按匹配处理（向前兼容）", () => {
    const resp = { ...mockConfluence("BTCUSDT", "4h"), symbol: undefined, tf: undefined } as ConfluenceResponse;
    expect(matchesScope(resp, "ETHUSDT", "1h")).toBe(true);
  });
});

describe("mock 演示数据纪律（D4）", () => {
  it("mock=true 恒置且形状完整（组权重 40/30/20/10）", () => {
    const resp = mockConfluence("BTCUSDT", "15m");
    expect(resp.mock).toBe(true);
    expect(resp.groups.map((g) => g.weight)).toEqual([40, 30, 20, 10]);
    expect(resp.symbol).toBe("BTCUSDT");
    expect(resp.tf).toBe("15m");
  });
});
