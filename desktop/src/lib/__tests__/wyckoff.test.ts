import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import {
  buildWyckoffBand,
  buildWyckoffMarks,
  WYCKOFF_PHASE_ALPHA,
  WYCKOFF_SIDE_COLORS,
  type WyckoffResponse,
} from "../wyckoff";
import type { TrapAnchorBar } from "../trapSignals";

/**
 * /api/wyckoff 契约锁（威科夫阶段带/事件标记消费，契约冻结于开发计划 §T2.5）：
 * URL 拼接、成功体字段透传、ok:false 降级体、HTTP 错误抛 ApiError；
 * buildWyckoffBand / buildWyckoffMarks 的窗口裁剪、吸附与容错。
 * 后端未上线（红灯基线预期 404），全部用 mock 数据按冻结契约测试。
 */

/** 100 根 15m bar：开盘时间 1000, 1900, 2800, …（步长 900 秒） */
const BARS: TrapAnchorBar[] = Array.from({ length: 100 }, (_, i) => ({
  timeSec: 1000 + i * 900,
  high: 110 + i,
  low: 90 + i,
}));
const FIRST_TS = BARS[0].timeSec;
const LAST_TS = BARS[BARS.length - 1].timeSec;

const OK_BODY: WyckoffResponse = {
  ok: true,
  symbol: "BTCUSDT",
  interval: "1h",
  range: { high: 65800, low: 63200, start_ts: 10_000 },
  state: { side: "acc", phase: "C", since_ts: 12_000 },
  events: [
    {
      type: "spring",
      ts: 19_000,
      price: 63150,
      confidence: 0.78,
      sd_bias: "accumulation",
      reasons: ["破区间低点后 2 根收回", "破位量 0.6× 均量（无供给跟随）"],
    },
  ],
  verdict_hint: "Phase C 弹簧已确认 + 吸筹证据链支持：等待 SOS/LPS 入场结构",
};

function mockFetchOnce(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Not Found",
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api.wyckoff 契约", () => {
  it("URL 正确拼接且透传 range/state/events/verdict_hint 完整体", async () => {
    const fn = mockFetchOnce(200, OK_BODY);
    const res = await api.wyckoff("BTCUSDT", "1h");
    expect(fn).toHaveBeenCalledTimes(1);
    const url = String(fn.mock.calls[0][0]);
    expect(url).toContain("/api/wyckoff?symbol=BTCUSDT&interval=1h");
    expect(res.range?.high).toBe(65800);
    expect(res.state?.side).toBe("acc");
    expect(res.state?.phase).toBe("C");
    expect(res.events).toHaveLength(1);
    expect(res.events?.[0].type).toBe("spring");
    expect(res.events?.[0].sd_bias).toBe("accumulation");
    expect(res.verdict_hint).toContain("Phase C");
  });

  it("range=null（趋势段）与 events 空数组按契约原样透传", async () => {
    mockFetchOnce(200, {
      ok: true,
      symbol: "ETHUSDT",
      interval: "15m",
      range: null,
      state: { side: "trend" },
      events: [],
      verdict_hint: null,
    });
    const res = await api.wyckoff("ETHUSDT", "15m");
    expect(res.ok).toBe(true);
    expect(res.range).toBeNull();
    expect(res.events).toEqual([]);
  });

  it("后端降级体（HTTP 200 + ok:false）原样返回，调用方按 error 展示", async () => {
    mockFetchOnce(200, { ok: false, symbol: "SNDKUSDT", interval: "15m", error: "K线拉取失败" });
    const res = await api.wyckoff("SNDKUSDT", "15m");
    expect(res.ok).toBe(false);
    expect(res.error).toContain("K线拉取失败");
  });

  it("HTTP 非 2xx（后端未部署 404）抛 ApiError", async () => {
    mockFetchOnce(404, { detail: "Not Found" });
    await expect(api.wyckoff("BTCUSDT", "1h")).rejects.toThrowError(ApiError);
  });
});

describe("buildWyckoffBand 阶段带换算", () => {
  const resp = (over: Partial<WyckoffResponse>): WyckoffResponse => ({
    ok: true,
    symbol: "BTCUSDT",
    interval: "1h",
    range: { high: 120, low: 95, start_ts: FIRST_TS + 10 * 900 },
    state: { side: "acc", phase: "C" },
    events: [],
    ...over,
  });

  it("acc 侧出绿带：from 吸附区间起点 bar、to 恒为最新 bar，透明度按 phase", () => {
    const band = buildWyckoffBand(resp({}), BARS);
    expect(band).not.toBeNull();
    expect(band?.fromSec).toBe(FIRST_TS + 10 * 900);
    expect(band?.toSec).toBe(LAST_TS);
    expect(band?.high).toBe(120);
    expect(band?.low).toBe(95);
    // #3fb950 + Phase C 透明度（0.12 → 0x1f）
    const alphaHex = Math.round(WYCKOFF_PHASE_ALPHA.C * 255)
      .toString(16)
      .padStart(2, "0");
    expect(band?.fill).toBe(`${WYCKOFF_SIDE_COLORS.acc}${alphaHex}`);
    expect(band?.label).toContain("吸筹");
    expect(band?.label).toContain("Phase C");
  });

  it("dist 侧出红带；phase E 比 phase A 更实（透明度递进）", () => {
    const e = buildWyckoffBand(
      resp({ state: { side: "dist", phase: "E" } }),
      BARS,
    );
    const a = buildWyckoffBand(
      resp({ state: { side: "dist", phase: "A" } }),
      BARS,
    );
    expect(e?.fill.startsWith(WYCKOFF_SIDE_COLORS.dist)).toBe(true);
    const alphaOf = (fill: string) => parseInt(fill.slice(7, 9), 16);
    expect(alphaOf(e!.fill)).toBeGreaterThan(alphaOf(a!.fill));
    expect(e?.label).toContain("派发");
  });

  it("range=null（趋势段）→ 不画带", () => {
    expect(buildWyckoffBand(resp({ range: null }), BARS)).toBeNull();
  });

  it("side 非 acc/dist（trend/unknown）→ 不画带", () => {
    expect(buildWyckoffBand(resp({ state: { side: "trend" } }), BARS)).toBeNull();
    expect(buildWyckoffBand(resp({ state: { side: "unknown" } }), BARS)).toBeNull();
  });

  it("容错：resp=null / ok:false / bars 空 / 区间几何非法 / 起点在窗口右侧 → null", () => {
    expect(buildWyckoffBand(null, BARS)).toBeNull();
    expect(buildWyckoffBand(resp({ ok: false }), BARS)).toBeNull();
    expect(buildWyckoffBand(resp({}), [])).toBeNull();
    expect(
      buildWyckoffBand(resp({ range: { high: 95, low: 120, start_ts: FIRST_TS } }), BARS),
    ).toBeNull();
    expect(
      buildWyckoffBand(resp({ range: { high: 120, low: 95, start_ts: LAST_TS + 900 } }), BARS),
    ).toBeNull();
  });

  it("起点早于窗口首根（懒加载前的历史）→ 夹到窗口首根", () => {
    const band = buildWyckoffBand(
      resp({ range: { high: 120, low: 95, start_ts: FIRST_TS - 50_000 } }),
      BARS,
    );
    expect(band?.fromSec).toBe(FIRST_TS);
  });
});

describe("buildWyckoffMarks 事件标记换算", () => {
  const resp = (events: WyckoffResponse["events"]): WyckoffResponse => ({
    ok: true,
    symbol: "BTCUSDT",
    interval: "1h",
    range: null,
    state: { side: "acc", phase: "B" },
    events,
  });

  it("12 事件契约 type 全部可映射：吸附 bar、方位/配色按事件侧", () => {
    const types = [
      "sc", "ar", "st", "spring", "test", "sos",
      "lps", "bc", "ut", "utad", "sow", "lpsy",
    ] as const;
    const events = types.map((t, i) => ({
      type: t,
      ts: BARS[i * 5].timeSec,
      price: 100,
      confidence: 0.5,
    }));
    const marks = buildWyckoffMarks(resp(events), BARS);
    expect(marks).toHaveLength(12);
    const byType = new Map(marks.map((m) => [m.event.type, m]));
    // 低点事件挂下方、绿徽章（吸筹侧）
    expect(byType.get("spring")?.position).toBe("below");
    expect(byType.get("spring")?.color).toBe(WYCKOFF_SIDE_COLORS.acc);
    expect(byType.get("spring")?.abbr).toBe("SPR");
    // 高点事件挂上方、红徽章（派发侧）
    expect(byType.get("utad")?.position).toBe("above");
    expect(byType.get("utad")?.color).toBe(WYCKOFF_SIDE_COLORS.dist);
    expect(byType.get("utad")?.abbr).toBe("UTAD");
    // 按时间升序
    for (let i = 1; i < marks.length; i++) {
      expect(marks[i].timeSec).toBeGreaterThan(marks[i - 1].timeSec);
    }
  });

  it("事件 ts 吸附到最近 bar，锚定该 bar 高低点，tooltip 带置信度", () => {
    const ts = BARS[3].timeSec + 100; // 偏移 100s，仍应吸附到 bars[3]
    const marks = buildWyckoffMarks(
      resp([{ type: "sc", ts, price: 93, confidence: 0.66 }]),
      BARS,
    );
    expect(marks).toHaveLength(1);
    expect(marks[0].timeSec).toBe(BARS[3].timeSec);
    expect(marks[0].anchorHigh).toBe(BARS[3].high);
    expect(marks[0].anchorLow).toBe(BARS[3].low);
    expect(marks[0].tooltip).toContain("恐慌抛售");
    expect(marks[0].tooltip).toContain("66%");
  });

  it("events 空数组 / 缺失 → 空标记（容错不抛错）", () => {
    expect(buildWyckoffMarks(resp([]), BARS)).toEqual([]);
    expect(buildWyckoffMarks(resp(undefined), BARS)).toEqual([]);
    expect(buildWyckoffMarks(null, BARS)).toEqual([]);
  });

  it("未知事件 type / 非法 ts·price / 窗口外事件丢弃", () => {
    const marks = buildWyckoffMarks(
      resp([
        // @ts-expect-error 后端新增未知事件类型时前端安全丢弃
        { type: "phase_shift", ts: BARS[1].timeSec, price: 100 },
        { type: "sc", ts: Number.NaN, price: 100 },
        { type: "sc", ts: BARS[1].timeSec, price: Number.NaN },
        { type: "sc", ts: FIRST_TS - 900, price: 100 },
        { type: "sc", ts: LAST_TS + 900, price: 100 },
        { type: "spring", ts: BARS[2].timeSec, price: 100, confidence: 0.5 },
      ]),
      BARS,
    );
    expect(marks).toHaveLength(1);
    expect(marks[0].event.type).toBe("spring");
  });
});
