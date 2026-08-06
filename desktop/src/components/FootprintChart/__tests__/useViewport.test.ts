// 足迹图视口缩放边界与物理推进测试（R9 交互丝滑化）：
// 核心断言「缩不出空白、缩不到不可读、放大有上限、缩放平滑收敛」
import { describe, expect, it } from "vitest";
import { initialViewport, stepViewport, zoomBoundsOf } from "../useViewport";
import { BASE_BAR_W, MAX_ZOOM, MIN_ZOOM } from "../renderer";

const geom = (over: Partial<Parameters<typeof zoomBoundsOf>[0]> = {}) => ({
  chartW: 880,
  maxScroll: 0,
  centerPriceEff: 100,
  plotH: 600,
  tick: 0.1,
  visLo: Infinity,
  visHi: -Infinity,
  barCount: 0,
  ...over,
});

describe("zoomBoundsOf 动态缩放边界", () => {
  it("海量数据：X 下限止步于可读柱宽（5px），不会缩到不可辨认", () => {
    const zb = zoomBoundsOf(geom({ barCount: 5000 }));
    expect(zb.minX).toBeCloseTo(5 / BASE_BAR_W, 6);
    expect(zb.minX).toBeGreaterThan(MIN_ZOOM);
  });

  it("中等数据：缩到 X 下限时全部数据恰好填满图区（不留整屏空白）", () => {
    const g = geom({ barCount: 20, chartW: 880 });
    const zb = zoomBoundsOf(g);
    // fitX = 880 / (20*88) = 0.5，高于可读下限 → 采用 fit-all
    expect(zb.minX).toBeCloseTo(0.5, 6);
    expect(g.barCount * BASE_BAR_W * zb.minX).toBeCloseTo(g.chartW, 3);
  });

  it("极少数据：X 下限封顶 1（默认倍率），不强制放大也不允许缩小", () => {
    const zb = zoomBoundsOf(geom({ barCount: 5 }));
    expect(zb.minX).toBe(1);
  });

  it("无数据：退回绝对下限 MIN_ZOOM", () => {
    const zb = zoomBoundsOf(geom({ barCount: 0 }));
    expect(zb.minX).toBe(MIN_ZOOM);
    expect(zb.minY).toBe(MIN_ZOOM);
  });

  it("X 上限：单柱不超图区宽 1/3，且不超过 MAX_ZOOM", () => {
    const zb = zoomBoundsOf(geom({ barCount: 100, chartW: 880 }));
    expect(zb.maxX).toBeCloseTo(880 / 3 / BASE_BAR_W, 6);
    const wide = zoomBoundsOf(geom({ barCount: 100, chartW: 2640 }));
    expect(wide.maxX).toBe(MAX_ZOOM);
  });

  it("Y 下限：可见价格范围恰好装满绘图区（与 autoFit 同口径），封死纵向空白", () => {
    // 2000 行的大范围：fitRowH 触底 1.2px → minY = 1.2/17
    const big = zoomBoundsOf(geom({ barCount: 50, visLo: 90, visHi: 110, tick: 0.01 }));
    expect(big.minY).toBeCloseTo(1.2 / 17, 6);
    // 小范围：fit 倍率 >1 时封顶 1
    const small = zoomBoundsOf(geom({ barCount: 50, visLo: 99, visHi: 101, tick: 0.1 }));
    expect(small.minY).toBe(1);
  });

  it("边界自洽：max 恒 ≥ min", () => {
    for (const g of [geom(), geom({ barCount: 3, chartW: 200 }), geom({ barCount: 9999 })]) {
      const zb = zoomBoundsOf(g);
      expect(zb.maxX).toBeGreaterThanOrEqual(zb.minX);
      expect(zb.maxY).toBeGreaterThanOrEqual(zb.minY);
    }
  });
});

describe("stepViewport 缩放平滑收敛", () => {
  it("zoomX 按帧向 zoomTarget 插值并最终精确收敛（无跳变）", () => {
    const vp = initialViewport();
    vp.zoomTargetX = 2;
    const g = geom({ barCount: 100 });
    let prev = vp.zoomX;
    let frames = 0;
    while (vp.zoomX !== vp.zoomTargetX && frames < 300) {
      stepViewport(vp, g, 1 / 60);
      // 单调逼近目标，且单帧步长有限（平滑而非跳变）
      expect(vp.zoomX).toBeGreaterThanOrEqual(prev);
      expect(vp.zoomX - prev).toBeLessThan(0.5);
      prev = vp.zoomX;
      frames++;
    }
    expect(vp.zoomX).toBe(2);
    expect(frames).toBeGreaterThan(3); // 至少数帧过渡，非一步到位
  });

  it("initialViewport 默认即「跟随最新 + 默认缩放」复位态", () => {
    const vp = initialViewport();
    expect(vp.follow).toBe(true);
    expect(vp.zoomX).toBe(1);
    expect(vp.zoomY).toBe(1);
    expect(vp.centerPrice).toBeNull();
  });
});
