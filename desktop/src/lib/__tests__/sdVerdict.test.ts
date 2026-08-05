import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type SdVerdictResponse } from "../../api/client";

/**
 * /api/sd-verdict 契约锁（主力底牌卡消费）：
 * URL 拼接、成功体字段透传、后端 ok:false 降级体、HTTP 错误抛 ApiError。
 */

const OK_BODY: SdVerdictResponse = {
  ok: true,
  symbol: "BTCUSDT",
  interval: "15m",
  ts: 1785920000,
  bias: "accumulation",
  score: 0.42,
  confidence: 0.35,
  coverage: 0.833,
  breakout_check: {
    active: true,
    direction: "up",
    verdict: "suspect",
    reasons: ["价格破位但 CVD 未创同向极值（量价背离）"],
  },
  evidence_chain: [
    { source: "trap", signal: null, direction: 0, weight: 0.3, detail: "近 10 根无陷阱信号（中性）" },
    { source: "whale", signal: null, direction: 0, weight: 0, detail: "证据缺失（WS 离线或无大单数据）" },
  ],
  stale: false,
};

function mockFetchOnce(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Internal Server Error",
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api.sdVerdict 契约", () => {
  it("URL 正确拼接且透传完整裁决体", async () => {
    const fn = mockFetchOnce(200, OK_BODY);
    const res = await api.sdVerdict("BTCUSDT", "15m");
    expect(fn).toHaveBeenCalledTimes(1);
    const url = String(fn.mock.calls[0][0]);
    expect(url).toContain("/api/sd-verdict?symbol=BTCUSDT&interval=15m");
    expect(res.bias).toBe("accumulation");
    expect(res.breakout_check?.verdict).toBe("suspect");
    expect(res.evidence_chain).toHaveLength(2);
    // 缺失路 weight=0（前端据此灰化展示）
    expect(res.evidence_chain?.find((e) => e.source === "whale")?.weight).toBe(0);
  });

  it("后端降级体（HTTP 200 + ok:false）原样返回，调用方按 error 展示", async () => {
    mockFetchOnce(200, { ok: false, symbol: "SNDKUSDT", interval: "15m", error: "K线拉取失败" });
    const res = await api.sdVerdict("SNDKUSDT", "15m");
    expect(res.ok).toBe(false);
    expect(res.error).toContain("K线拉取失败");
  });

  it("HTTP 非 2xx 抛 ApiError（保留响应体）", async () => {
    mockFetchOnce(500, { detail: "internal" });
    await expect(api.sdVerdict("BTCUSDT", "1h")).rejects.toThrowError(ApiError);
  });
});
