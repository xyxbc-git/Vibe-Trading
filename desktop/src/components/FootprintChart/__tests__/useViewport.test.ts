// 足迹图视口缩放边界与物理推进测试（R9 交互丝滑化 / R10 TradingView 化）：
// 核心断言「缩不出空白、缩不到不可读、放大有上限、缩放平滑收敛」，
// 以及「滚轮与触控板分流不误判、纵向拖拽不丢位移、价格轴 Auto 收敛且不空转」
import { describe, expect, it } from "vitest";
import {
  classifyWheelDevice,
  crosshairAt,
  dragPriceStep,
  initialViewport,
  priceFitOf,
  stepViewport,
  wheelNotchZoomFactor,
  zoomBoundsOf,
  type WheelSample,
} from "../useViewport";
import { BASE_BAR_W, MAX_ZOOM, MIN_ZOOM, rowHOf } from "../renderer";

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

  it("initialViewport 默认即「跟随最新 + 默认缩放 + 价格轴 Auto」复位态", () => {
    const vp = initialViewport();
    expect(vp.follow).toBe(true);
    expect(vp.zoomX).toBe(1);
    expect(vp.zoomY).toBe(1);
    expect(vp.centerPrice).toBeNull();
    expect(vp.priceAutoFit).toBe(true);
    expect(vp.centerPriceTarget).toBeNull();
    expect(vp.crosshair).toBeNull();
  });
});

describe("crosshairAt 十字光标位置", () => {
  const rect = { left: 40, top: 20, width: 880, height: 600 };

  it("指针在画布内：换算成画布左上角起算的 CSS 像素", () => {
    expect(crosshairAt(140, 320, rect)).toEqual({ mx: 100, my: 300 });
    expect(crosshairAt(40, 20, rect)).toEqual({ mx: 0, my: 0 }); // 左上角闭区间
  });

  it("指针出画布：置 null，不留一条僵在边缘的十字线", () => {
    expect(crosshairAt(39, 320, rect)).toBeNull(); // 左侧越界
    expect(crosshairAt(140, 19, rect)).toBeNull(); // 上方越界
    expect(crosshairAt(920, 320, rect)).toBeNull(); // 右缘开区间
    expect(crosshairAt(140, 620, rect)).toBeNull(); // 下缘开区间
    // 拖拽中指针被 setPointerCapture 带出画布，同样按出界处理
    expect(crosshairAt(2000, -500, rect)).toBeNull();
  });

  it("坐标系与缩放锚点/hitTest 一致：价格轴与统计区也在画布内有值", () => {
    // 画布宽 880，价格轴占右侧 AXIS_W；轴上位置仍是合法的画布内坐标
    expect(crosshairAt(40 + 870, 20 + 590, rect)).toEqual({ mx: 870, my: 590 });
  });
});

describe("classifyWheelDevice 滚轮/触控板分流", () => {
  const wheel = (over: Partial<WheelSample> = {}): WheelSample => ({
    deltaX: 0,
    deltaY: 0,
    deltaMode: 0,
    ...over,
  });

  it("传统滚轮：Chromium 每档 deltaY=100 / wheelDeltaY=∓120，比例 1.2", () => {
    expect(classifyWheelDevice(wheel({ deltaY: 100, wheelDeltaY: -120 }), null, 0)).toBe("mouse");
    expect(classifyWheelDevice(wheel({ deltaY: -300, wheelDeltaY: 360 }), null, 0)).toBe("mouse");
  });

  it("触控板：精确滚动恒满足 wheelDeltaY = -3×deltaY（含恰好 ±120 的临界值）", () => {
    expect(classifyWheelDevice(wheel({ deltaY: 4, wheelDeltaY: -12 }), null, 0)).toBe("trackpad");
    // deltaY=40 时 wheelDeltaY 恰为 -120：按「120 的整数倍」判会误判成滚轮，比例判不会
    expect(classifyWheelDevice(wheel({ deltaY: 40, wheelDeltaY: -120 }), null, 0)).toBe("trackpad");
    expect(
      classifyWheelDevice(wheel({ deltaX: -13.5, deltaY: 2.5, wheelDeltaY: -7.5 }), null, 0),
    ).toBe("trackpad");
  });

  it("行/页滚动模式只有传统滚轮会用", () => {
    expect(classifyWheelDevice(wheel({ deltaY: 3, deltaMode: 1 }), null, 0)).toBe("mouse");
  });

  it("拿不到 wheelDeltaY（非 Chromium）时按细碎/横向分量退化判定", () => {
    expect(classifyWheelDevice(wheel({ deltaY: 100 }), null, 0)).toBe("mouse");
    expect(classifyWheelDevice(wheel({ deltaY: 17.3 }), null, 0)).toBe("trackpad");
    expect(classifyWheelDevice(wheel({ deltaX: -20 }), null, 0)).toBe("trackpad");
  });

  it("手势内粘滞：触控板滑动中途命中滚轮特征不翻脸，停手后重新判定", () => {
    const prev = { device: "trackpad" as const, t: 1000 };
    expect(classifyWheelDevice(wheel({ deltaY: 100, wheelDeltaY: -120 }), prev, 1100)).toBe(
      "trackpad",
    );
    expect(classifyWheelDevice(wheel({ deltaY: 100, wheelDeltaY: -120 }), prev, 1200)).toBe("mouse");
  });
});

describe("wheelNotchZoomFactor 滚轮缩放步长", () => {
  it("一档 = ±10%：与 TradingView barSpacing ± barSpacing/10 同口径", () => {
    expect(wheelNotchZoomFactor(-100)).toBeCloseTo(1.1, 10);
    expect(wheelNotchZoomFactor(100)).toBeCloseTo(0.9, 10);
  });

  it("狂滚限幅：单次事件最多一档，不会一滚到底", () => {
    expect(wheelNotchZoomFactor(-2000)).toBeCloseTo(1.1, 10);
    expect(wheelNotchZoomFactor(2000)).toBeCloseTo(0.9, 10);
  });

  it("半档按比例，细腻滚轮不丢分辨率", () => {
    expect(wheelNotchZoomFactor(-50)).toBeCloseTo(1.05, 10);
    expect(wheelNotchZoomFactor(0)).toBe(1);
  });
});

describe("dragPriceStep 纵向拖拽跟手", () => {
  const g = geom({ tick: 0.1, centerPriceEff: 100 });

  it("同帧多个 pointermove 逐个累加，等价于一次等量位移（不丢位移）", () => {
    const many = initialViewport();
    for (let i = 0; i < 3; i++) many.centerPrice = dragPriceStep(many, -6, g);
    const once = initialViewport();
    once.centerPrice = dragPriceStep(once, -18, g);
    expect(many.centerPrice).toBeCloseTo(once.centerPrice as number, 12);
    expect(many.centerPrice).toBeCloseTo(100 - (18 * 0.1) / rowHOf(1), 12);
  });

  it("回归：以上一帧 layout 快照为基准会丢掉同帧内除最后一次外的所有位移", () => {
    const stale = g.centerPriceEff + (-6 * g.tick) / rowHOf(1); // 旧实现同帧只剩最后一个事件
    const vp = initialViewport();
    for (let i = 0; i < 3; i++) vp.centerPrice = dragPriceStep(vp, -6, g);
    expect(vp.centerPrice).not.toBeCloseTo(stale, 6);
  });
});

describe("priceAutoFit 价格轴自动适配（TradingView 价格轴 Auto）", () => {
  const fitGeom = geom({
    barCount: 50,
    visLo: 90,
    visHi: 110,
    tick: 0.1,
    plotH: 600,
    centerPriceEff: 105,
  });
  const settle = (vp: ReturnType<typeof initialViewport>) => {
    let frames = 0;
    while (frames < 400 && stepViewport(vp, fitGeom, 1 / 60)) frames++;
    return frames;
  };

  it("Auto 态：纵向缩放与中心价平滑收敛到「装下可见价格范围」", () => {
    const vp = initialViewport();
    const frames = settle(vp);
    const fit = priceFitOf(fitGeom);
    expect(fit).not.toBeNull();
    expect(vp.zoomY).toBeCloseTo(fit!.zoomY, 6);
    expect(vp.centerPrice).toBeCloseTo(100, 6);
    expect(frames).toBeGreaterThan(3); // 平滑过渡而非一步到位
    // 可见范围上下缘都落在绘图区内（这正是「横向缩放不会把行情甩出屏幕」）
    const halfSpanPx = (((110 - 90) / 2) / 0.1) * rowHOf(vp.zoomY);
    expect(halfSpanPx).toBeLessThanOrEqual(fitGeom.plotH / 2);
  });

  it("收敛后不再判脏：不会每帧空转重绘", () => {
    const vp = initialViewport();
    settle(vp);
    expect(stepViewport(vp, fitGeom, 1 / 60)).toBe(false);
  });

  it("Auto 态但可见范围无数据（切币空窗期）：丢掉旧目标，不追回上一个标的的价格", () => {
    const vp = initialViewport();
    settle(vp);
    expect(vp.centerPrice).toBeCloseTo(100, 6);
    // 数据层清空 + 布局回到自动定心后，Auto 不该再把 100 拉回来
    vp.centerPrice = null;
    const empty = geom({ barCount: 0, plotH: 600, tick: 0.1, centerPriceEff: 0 });
    stepViewport(vp, empty, 1 / 60);
    expect(vp.centerPriceTarget).toBeNull();
    expect(vp.centerPrice).toBeNull();
  });

  it("退出 Auto 后不再接管中心价：手动拖拽的结果不会被拽回", () => {
    const vp = initialViewport();
    vp.priceAutoFit = false;
    vp.centerPriceTarget = null;
    vp.centerPrice = 123;
    expect(stepViewport(vp, fitGeom, 1 / 60)).toBe(false);
    expect(vp.centerPrice).toBe(123);
  });
});
