// 指标开关持久化层测试（R6）：round-trip、未知 key 容错、损坏数据回退、
// 白名单过滤。node 环境无 localStorage，用最小 mock 挂到 globalThis。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  enumOr,
  listOr,
  loadChartToggles,
  saveChartToggles,
  toggleOr,
} from "../chartToggles";

const KEY = "jarvis.chart.toggles.v1";

function installStorage(): Map<string, string> {
  const store = new Map<string, string>();
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
  return store;
}

let store: Map<string, string>;
beforeEach(() => {
  store = installStorage();
});
afterEach(() => {
  delete (globalThis as Record<string, unknown>).localStorage;
});

describe("chartToggles 持久化层", () => {
  it("round-trip：写入后读回一致，合并写不丢其它字段", () => {
    saveChartToggles({ macdOn: true, tf: "1h" });
    saveChartToggles({ deltaOn: false, draws: ["trend", "sr"] });
    const t = loadChartToggles();
    expect(t.macdOn).toBe(true);
    expect(t.tf).toBe("1h");
    expect(t.deltaOn).toBe(false);
    expect(t.draws).toEqual(["trend", "sr"]);
  });

  it("未知 key 容错：读不崩、合并写回保留（向前兼容）", () => {
    store.set(KEY, JSON.stringify({ futureSwitch: 42, macdOn: true }));
    const t = loadChartToggles();
    expect(t.macdOn).toBe(true);
    saveChartToggles({ liqOn: true });
    const round = JSON.parse(store.get(KEY)!);
    expect(round.futureSwitch).toBe(42); // 未知字段不被吞
    expect(round.liqOn).toBe(true);
  });

  it("损坏 JSON / 非对象 / storage 缺失一律回退空对象", () => {
    store.set(KEY, "{oops");
    expect(loadChartToggles()).toEqual({});
    store.set(KEY, JSON.stringify([1, 2]));
    expect(loadChartToggles()).toEqual({});
    delete (globalThis as Record<string, unknown>).localStorage;
    expect(loadChartToggles()).toEqual({});
    expect(() => saveChartToggles({ macdOn: true })).not.toThrow();
  });

  it("toggleOr/enumOr/listOr：类型与白名单校验，非法值回退默认", () => {
    store.set(
      KEY,
      JSON.stringify({ macdOn: "yes", tf: "2h", draws: ["trend", "hax", 7] }),
    );
    const t = loadChartToggles();
    expect(toggleOr(t, "macdOn", false)).toBe(false); // 非布尔回退
    expect(toggleOr(t, "missing", true)).toBe(true);
    const TFS = ["1m", "15m", "1h"] as const;
    expect(enumOr(t, "tf", TFS, "15m")).toBe("15m"); // 白名单外回退
    const MODES = ["trend", "sr", "fib"] as const;
    expect(listOr(t, "draws", MODES)).toEqual(["trend"]); // 过滤非法项
    expect(listOr(t, "missing", MODES)).toEqual([]);
  });
});
