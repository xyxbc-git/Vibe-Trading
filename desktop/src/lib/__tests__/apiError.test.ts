import { describe, it, expect } from "vitest";
import { ApiError } from "../../api/client";

/**
 * P1-2 回归锁：非 2xx 响应体不能再丢。
 * 423 冷静期拦截的 reason（剩余分钟/触发原因）与 cooldown 结构化字段
 * 必须能从 ApiError 上取到；无 body 时 message 回退旧「API <status>: <statusText>」格式。
 */
describe("ApiError", () => {
  it("423 冷静期：message 用业务 reason，body 保留 cooldown 结构化字段", () => {
    const body = {
      ok: false,
      reason: "冷静期锁单中（剩余 42 分钟，触发原因: 组合回撤 21%）；提前解锁需在面板二次确认",
      cooldown: { active: true, remaining_s: 2520, expired: false },
    };
    const e = new ApiError(423, "Locked", body);
    expect(e.status).toBe(423);
    expect(e.message).toBe(`API 423: ${body.reason}`);
    expect(e.reason).toContain("冷静期锁单中");
    expect((e.body?.cooldown as { remaining_s: number }).remaining_s).toBe(2520);
  });

  it("无 JSON body：message 回退 statusText（旧格式兼容，既有 catch 不受影响）", () => {
    const e = new ApiError(500, "Internal Server Error", null);
    expect(e.message).toBe("API 500: Internal Server Error");
    expect(e.reason).toBeNull();
    expect(e.body).toBeNull();
  });

  it("FastAPI 默认错误封套的 detail 字段也能作为 reason 取到", () => {
    const e = new ApiError(422, "Unprocessable Entity", { detail: "参数校验失败" });
    expect(e.message).toBe("API 422: 参数校验失败");
    expect(e.reason).toBe("参数校验失败");
  });

  it("reason 为空串时视为无业务原因，回退 statusText", () => {
    const e = new ApiError(400, "Bad Request", { ok: false, reason: "" });
    expect(e.message).toBe("API 400: Bad Request");
    expect(e.reason).toBeNull();
  });
});
