import { describe, it, expect } from "vitest";
import { isValidEmail } from "../../components/cards/OrderNotifyDialog";

// 内联邮件提醒（PositionAdvisor）与配置弹窗共用的邮箱宽校验，
// 口径对齐后端 jarvis_order_notify._valid_email（有 @ 且域名带点）。
describe("isValidEmail", () => {
  it("accepts common addresses", () => {
    expect(isValidEmail("you@example.com")).toBe(true);
    expect(isValidEmail("  a.b+tag@sub.domain.org  ")).toBe(true);
  });

  it("rejects malformed addresses", () => {
    expect(isValidEmail("")).toBe(false);
    expect(isValidEmail("plainaddress")).toBe(false);
    expect(isValidEmail("no-at.com")).toBe(false);
    expect(isValidEmail("a@b")).toBe(false); // 域名无点
    expect(isValidEmail("a@")).toBe(false);
  });
});
