import { useCallback, useEffect, useRef, useState } from "react";
import type { ViewportState } from "./renderer";
import {
  AXIS_W,
  BASE_BAR_W,
  BASE_ROW_H,
  MAX_ZOOM,
  MIN_ZOOM,
  RIGHT_GAP_BARS_DEFAULT,
  RIGHT_GAP_BARS_MAX,
  STATS_H,
  TIME_H,
  barWOf,
  clamp,
  rowHOf,
} from "./renderer";

export interface ViewportApi {
  vpRef: React.MutableRefObject<ViewportState>;
  /** 作为 ref 回调绑定到画布容器（wheel 需 passive:false 阻止页面滚动） */
  bindTarget: (el: HTMLElement | null) => void;
  resetFollow: () => void;
  /** follow 状态镜像（仅用于「回到最新」按钮显隐，低频更新） */
  following: boolean;
  /** priceAutoFit 状态镜像（用于「自动适配」按钮高亮，低频更新） */
  priceAutoFit: boolean;
  /** vpRef.follow / priceAutoFit 被外部（如 stepViewport 惯性吸附）改动后调用，同步两个镜像 */
  syncFollow: () => void;
  /** 开启价格轴自动适配：纵向持续装下可见柱的价格范围（TV 价格轴 Auto） */
  autoFit: () => void;
}

interface GeomRef {
  chartW: number;
  maxScroll: number;
  centerPriceEff: number;
  plotH: number;
  tick: number;
  /** 可见范围价格极值（自动适配用；无数据时 hi<lo） */
  visLo: number;
  visHi: number;
  /** 当前已加载柱数（动态缩放下限「适应全部数据」用） */
  barCount: number;
}

/** 惯性衰减：每帧 ×0.94（60fps 基准，按 dt 换算）；低于 8px/s 停止 */
const FRICTION_PER_FRAME = 0.94;
const MIN_FLING_SPEED = 8;
/** 缩放平滑：每帧向目标推进 26%（60fps 基准），≈180ms 收敛 */
const ZOOM_LERP_PER_FRAME = 0.26;
/** 轴拖拽缩放灵敏度：每 px 的 zoom 指数增量 */
const AXIS_DRAG_SENS = 0.006;
/** 可读性下限：柱宽低于此像素蜡烛/足迹柱已不可辨认，禁止继续缩小 */
const MIN_READABLE_BAR_W = 5;
/** 放大上限：单柱不超过图区宽的 1/3（一屏至少看得到 3 根柱） */
const MAX_BAR_W_RATIO = 1 / 3;
/** 触控板捏合缩放灵敏度 + 单次事件倍率限幅（降噪：狂滚也不会一滚到底） */
const WHEEL_ZOOM_SENS = 0.008;
const WHEEL_STEP_MAX = 1.25;
/** Chromium 归一化后传统滚轮一档的 deltaY 像素 */
const WHEEL_NOTCH_PX = 100;
/** 一档滚轮的缩放幅度 10%：与 TradingView lightweight-charts 的
 *  `newBarSpacing = barSpacing + scale × barSpacing/10`（scale 限幅 ±1）同口径 */
const WHEEL_NOTCH_ZOOM = 0.1;
/** 同一手势内沿用上次设备判定的时间窗（防触控板滑动中途被误判成滚轮） */
const WHEEL_DEVICE_STICKY_MS = 140;
/** 图区纵向拖拽累计超过此像素才退出价格轴 Auto（横向拖拽的手抖不该顶掉自动适配） */
const AUTOFIT_EXIT_DY = 4;
/** 自动适配中心价的收敛判定（低于 2% tick 视为到位，避免每帧都判脏重绘） */
const AUTOFIT_CENTER_EPS = 0.02;

export type WheelDevice = "mouse" | "trackpad";

/** wheel 事件里用于设备判定的最小结构（便于单测直接构造） */
export interface WheelSample {
  deltaX: number;
  deltaY: number;
  deltaMode: number;
  /** Chromium 保留的旧字段；非 Chromium 内核可能没有 */
  wheelDeltaY?: number;
}

/**
 * 区分传统滚轮与触控板双指滑动。
 * 两者派发的都是 wheel 事件，但语义必须相反——滚轮要缩放（TradingView
 * `mouse_wheel_scale` 默认开），触控板双指要平移（macOS 系统惯例，也是上一轮
 * 「不再默认缩放」修正想保住的体验）。判错任何一边都会让对应设备的用户难受。
 * 判据（Chromium 实测取值）：
 * - deltaMode≠0 的行/页模式只有传统滚轮会用；
 * - 触控板走精确滚动路径，Chromium 恒有 `wheelDeltaY === -3 × deltaY`；
 *   传统滚轮走 120 一档的量化路径（deltaY=100/档），比例是 1.2 而非 3；
 * - 拿不到 wheelDeltaY（非 Chromium）时退化为「细碎/带横向分量 = 触控板」。
 * prev 用于手势内粘滞：触控板一次滑动中偶尔命中滚轮特征，不该中途翻脸。
 */
export function classifyWheelDevice(
  e: WheelSample,
  prev: { device: WheelDevice; t: number } | null,
  now: number,
): WheelDevice {
  if (prev && now - prev.t < WHEEL_DEVICE_STICKY_MS) return prev.device;
  if (e.deltaMode !== 0) return "mouse";
  const wd = e.wheelDeltaY;
  if (typeof wd === "number" && wd !== 0) {
    return Math.abs(wd + 3 * e.deltaY) < 1e-6 ? "trackpad" : "mouse";
  }
  if (e.deltaX !== 0 || !Number.isInteger(e.deltaY)) return "trackpad";
  return Math.abs(e.deltaY) % WHEEL_NOTCH_PX === 0 ? "mouse" : "trackpad";
}

/** 传统滚轮：每档 ±10%，单次事件最多一档（TradingView 口径） */
export function wheelNotchZoomFactor(deltaY: number): number {
  return 1 + clamp(-deltaY / WHEEL_NOTCH_PX, -1, 1) * WHEEL_NOTCH_ZOOM;
}

/** 触控板捏合：连续量，指数映射 + 单次限幅 */
export function pinchZoomFactor(deltaY: number): number {
  return clamp(Math.exp(-deltaY * WHEEL_ZOOM_SENS), 1 / WHEEL_STEP_MAX, WHEEL_STEP_MAX);
}

/**
 * 指针位置 → 十字光标坐标（画布内 CSS 像素，相对画布左上角）。
 * 出界返回 null：指针在画布外时不该留着一条僵在边缘的十字线，
 * 也让绘制方拿到的坐标恒在画布内，不必自己兜底。
 */
export function crosshairAt(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
): { mx: number; my: number } | null {
  const mx = clientX - rect.left;
  const my = clientY - rect.top;
  if (mx < 0 || my < 0 || mx >= rect.width || my >= rect.height) return null;
  return { mx, my };
}

/**
 * 图区拖拽一步的纵向平移结果（价格）。
 * 基准必须取物理状态 vp.centerPrice，不能取上一帧 layout 的快照：一帧内常有
 * 多个 pointermove（120Hz 触控板 / 高回报率鼠标），用快照会让同帧内除最后一个
 * 之外的位移全部丢失，表现就是纵向拖拽跟不上手。
 */
export function dragPriceStep(vp: ViewportState, dy: number, g: GeomRef): number {
  return (vp.centerPrice ?? g.centerPriceEff) + (dy * g.tick) / rowHOf(vp.zoomY);
}

/** 价格轴自动适配的目标格高/中心价（zoomBoundsOf 的 minY 与之同口径） */
export function priceFitOf(g: GeomRef): { zoomY: number; centerPrice: number } | null {
  if (!(g.visHi > g.visLo) || g.plotH <= 0) return null;
  const span = (g.visHi - g.visLo) / Math.max(g.tick, 1e-9) + 4; // 上下各留 2 行
  const rowH = clamp(g.plotH / span, 1.2, 64);
  return {
    zoomY: clamp(rowH / BASE_ROW_H, MIN_ZOOM, MAX_ZOOM),
    centerPrice: (g.visHi + g.visLo) / 2,
  };
}

export interface ZoomBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/**
 * 动态缩放边界（防「缩到不可读 + 整屏空白」）：
 * - X 下限 = max(全部数据恰好填满图区的倍率, 可读柱宽下限)，且封顶 1（默认倍率永远可达）。
 *   数据少时缩到下限正好「适应全部数据」，不会继续缩出空白；数据多时止步于可读柱宽。
 * - X 上限 = 单柱 ≤ 图区宽 1/3（与 MAX_ZOOM 取小）。
 * - Y 下限 = 可见价格范围恰好装满绘图区的倍率（与 autoFit 同口径），封死纵向缩出空白。
 */
export function zoomBoundsOf(g: GeomRef): ZoomBounds {
  let minX = MIN_ZOOM;
  if (g.barCount > 0 && g.chartW > 0) {
    const fitX = g.chartW / (g.barCount * BASE_BAR_W);
    minX = clamp(Math.max(fitX, MIN_READABLE_BAR_W / BASE_BAR_W), MIN_ZOOM, 1);
  }
  const maxX = Math.max(
    minX,
    Math.min(MAX_ZOOM, (g.chartW * MAX_BAR_W_RATIO) / BASE_BAR_W),
  );

  const fit = priceFitOf(g);
  const minY = fit ? clamp(fit.zoomY, MIN_ZOOM, 1) : MIN_ZOOM;
  return { minX, maxX, minY, maxY: Math.max(minY, MAX_ZOOM) };
}

export function initialViewport(): ViewportState {
  return {
    zoomX: 1,
    zoomY: 1,
    zoomTargetX: 1,
    zoomTargetY: 1,
    anchor: null,
    crosshair: null,
    scrollX: 0,
    velX: 0,
    centerPrice: null,
    centerPriceTarget: null,
    priceAutoFit: true,
    follow: true,
    dragging: false,
    rightGapBars: RIGHT_GAP_BARS_DEFAULT,
  };
}

/**
 * TradingView 手感的视口物理引擎。
 * 状态全存 ref，由主组件的 rAF 循环每帧调用 step(dt) 推进：
 * - 拖拽跟手（指针事件直接写 scrollX/centerPrice 物理状态，无 React setState）
 * - 松手惯性滑行（velocity 采样 + 指数衰减，可按住打断）
 * - 输入按设备分流（见 classifyWheelDevice）：传统滚轮 = 缩时间轴（TV
 *   mouse_wheel_scale，每档 10%，右端不动）；触控板双指 = 平移；捏合 / ⌘+滚轮 =
 *   光标锚点两轴同缩（TV focused zoom）；Shift+滚轮 = 左右平移（兼作判定兜底）
 * - 缩放向 zoomTarget 平滑插值，范围受 zoomBoundsOf 动态边界约束（不可缩出空白）
 * - 价格轴 Auto（priceAutoFit）：纵向持续装下可见柱价格范围，横向缩放不会把行情
 *   甩出屏幕；纵向拖图区 / 拖缩价格轴退出，双击轴或「自动适配」恢复
 * - 图区右拖越过留白锚点时物化为更大的 rightGapBars（TV 式可调留白）
 * - 价格轴上下拖 = 纵向缩放；时间轴左右拖 = 横向缩放
 * - 双击图区 = 回到最新 + 默认缩放 + 回 Auto（一键复位）
 * - 维护 crosshair（画布内 CSS 像素，出界为 null），供绘制层画十字光标
 */
export function useViewport(getGeom: () => GeomRef): ViewportApi {
  const vpRef = useRef<ViewportState>(initialViewport());

  const [following, setFollowing] = useState(true);
  const [priceAutoFit, setPriceAutoFit] = useState(true);
  const [el, setEl] = useState<HTMLElement | null>(null);
  const getGeomRef = useRef(getGeom);
  getGeomRef.current = getGeom;

  const syncFollow = useCallback(() => {
    setFollowing(vpRef.current.follow);
    setPriceAutoFit(vpRef.current.priceAutoFit);
  }, []);

  /** 一键复位：回到最新 + 默认缩放 + 价格轴回 Auto（迷失后一步找回） */
  const resetFollow = useCallback(() => {
    const vp = vpRef.current;
    vp.follow = true;
    vp.centerPrice = null;
    vp.centerPriceTarget = null;
    vp.priceAutoFit = true;
    vp.velX = 0;
    vp.rightGapBars = RIGHT_GAP_BARS_DEFAULT;
    vp.zoomTargetX = 1;
    vp.zoomTargetY = 1;
    vp.anchor = null;
    syncFollow();
  }, [syncFollow]);

  /** 开启价格轴自动适配：具体格高/中心价由 stepViewport 每帧续算并平滑趋近 */
  const autoFit = useCallback(() => {
    const vp = vpRef.current;
    vp.priceAutoFit = true;
    vp.centerPriceTarget = null;
    vp.anchor = null;
    vp.velX = 0;
    syncFollow();
  }, [syncFollow]);

  const bindTarget = useCallback((node: HTMLElement | null) => {
    setEl(node);
  }, []);

  useEffect(() => {
    if (!el) return;

    // 拖拽速度采样窗（最近 ~80ms）
    let samples: { t: number; x: number }[] = [];
    let drag: {
      x: number;
      y: number;
      pointerId: number;
      /** chart=图区平移 priceAxis=纵向缩放 timeAxis=横向缩放 */
      kind: "chart" | "priceAxis" | "timeAxis";
      /** 图区拖拽累计纵向位移（判定是否真的想动价格轴，见 AUTOFIT_EXIT_DY） */
      dyTotal: number;
    } | null = null;

    /** 判定按下位置落在哪个交互区（价格轴 / 时间+统计轴 / 图区） */
    const zoneOf = (px: number, py: number): "chart" | "priceAxis" | "timeAxis" => {
      const r = el.getBoundingClientRect();
      const x = px - r.left;
      const y = py - r.top;
      const chartW = r.width - AXIS_W;
      const plotH = r.height - TIME_H - STATS_H;
      if (x >= chartW) return "priceAxis";
      if (y >= plotH && y < plotH + TIME_H) return "timeAxis";
      return "chart";
    };

    /** 上一次 wheel 的设备判定（手势内粘滞用） */
    let wheelDev: { device: WheelDevice; t: number } | null = null;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const g = getGeomRef.current();
      const vp = vpRef.current;
      const now = performance.now();
      const device = classifyWheelDevice(e, wheelDev, now);
      wheelDev = { device, t: now };
      const zone = zoneOf(e.clientX, e.clientY);
      // ⌘/Ctrl+滚轮 = TV 的「focused zoom」；触控板捏合在 Chromium 里同样带 ctrlKey
      const focused = e.ctrlKey || e.metaKey;
      const horizontal = Math.abs(e.deltaX) > Math.abs(e.deltaY);

      // 平移：触控板双指（macOS 惯例）/ Shift+滚轮（TV「左右移动图表」）/ 横向滚轮。
      // Shift 同时是设备判定万一判错时的确定性兜底：滚轮用户想平移永远有路可走
      if (!focused && zone === "chart" && (device === "trackpad" || e.shiftKey || horizontal)) {
        const delta = horizontal ? e.deltaX : e.deltaY;
        const eff = vp.follow ? g.maxScroll : vp.scrollX;
        const nx = eff + delta;
        vp.scrollX = nx;
        vp.velX = 0;
        vp.follow = nx >= g.maxScroll - 2;
        syncFollow();
        return;
      }

      // 缩放。单次事件倍率限幅 + 动态边界 clamp（缩到下限即「适应全部数据」，不再缩出空白）
      const factor = device === "mouse" ? wheelNotchZoomFactor(e.deltaY) : pinchZoomFactor(e.deltaY);
      const zb = zoomBoundsOf(g);
      // 图区滚轮只缩时间轴（价格轴交给 Auto 跟随，等于 TV 的 mouse_wheel_scale）；
      // 捏合/⌘ 两轴同缩；光标停在某条轴上时只缩那条轴（TV 在刻度上滚动的行为）
      const scaleX = zone !== "priceAxis";
      const scaleY = zone === "priceAxis" || (zone === "chart" && focused);
      if (scaleX) vp.zoomTargetX = clamp(vp.zoomTargetX * factor, zb.minX, zb.maxX);
      if (scaleY) {
        // 手动改了价格轴 → 退出 Auto（TV：拖/缩价格刻度即取消自动适配）
        vp.priceAutoFit = false;
        vp.centerPriceTarget = null;
        vp.zoomTargetY = clamp(vp.zoomTargetY * factor, zb.minY, zb.maxY);
      }
      const rect = el.getBoundingClientRect();
      vp.anchor =
        zone !== "chart"
          ? { mx: g.chartW / 2, my: g.plotH / 2 } // 轴上滚动：锚定图区中心，与拖轴一致
          : focused
            ? { mx: e.clientX - rect.left, my: e.clientY - rect.top }
            : { mx: g.chartW, my: g.plotH / 2 }; // TV right_bar_stays_on_scroll：右端不动
      vp.velX = 0;
      syncFollow();
    };

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const kind = zoneOf(e.clientX, e.clientY);
      drag = { x: e.clientX, y: e.clientY, pointerId: e.pointerId, kind, dyTotal: 0 };
      samples = [{ t: performance.now(), x: e.clientX }];
      el.setPointerCapture(e.pointerId);
      const vp = vpRef.current;
      if (kind === "chart") {
        vp.dragging = true;
        vp.velX = 0; // 按住即打断惯性
      }
    };

    const onPointerMove = (e: PointerEvent) => {
      const vp = vpRef.current;
      // 十字光标：拖拽期间同样持续跟随（与 TradingView 一致，按住拖动时十字线不断、
      // 轴上刻度跟着走），指针出画布即置 null
      vp.crosshair = crosshairAt(e.clientX, e.clientY, el.getBoundingClientRect());

      if (!drag || drag.pointerId !== e.pointerId) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      drag.x = e.clientX;
      drag.y = e.clientY;

      const g = getGeomRef.current();

      if (drag.kind === "priceAxis") {
        // 价格轴上下拖：纵向缩放（向下拖 = 拉伸格高，与 TV 一致），锚定图区中心价；
        // 手动改价格刻度即退出 Auto（TV 同款）
        const zb = zoomBoundsOf(g);
        vp.priceAutoFit = false;
        vp.centerPriceTarget = null;
        vp.zoomTargetY = clamp(vp.zoomTargetY * Math.exp(dy * AXIS_DRAG_SENS), zb.minY, zb.maxY);
        vp.anchor = { mx: g.chartW / 2, my: g.plotH / 2 };
        syncFollow();
        return;
      }
      if (drag.kind === "timeAxis") {
        // 时间轴左右拖：横向缩放，锚定图区水平中点
        const zb = zoomBoundsOf(g);
        vp.zoomTargetX = clamp(vp.zoomTargetX * Math.exp(dx * AXIS_DRAG_SENS), zb.minX, zb.maxX);
        vp.anchor = { mx: g.chartW / 2, my: g.plotH / 2 };
        return;
      }

      const now = performance.now();
      samples.push({ t: now, x: e.clientX });
      while (samples.length > 2 && now - samples[0].t > 80) samples.shift();

      const barW = barWOf(vp.zoomX);
      const eff = vp.follow ? g.maxScroll : vp.scrollX;
      vp.scrollX = eff - dx;
      // 纵向拖拽真的想动价格轴时才退出 Auto（横向拖拽的手抖不该顶掉自动适配）
      drag.dyTotal += dy;
      if (vp.priceAutoFit && Math.abs(drag.dyTotal) > AUTOFIT_EXIT_DY) {
        vp.priceAutoFit = false;
        vp.centerPriceTarget = null;
      }
      if (!vp.priceAutoFit) vp.centerPrice = dragPriceStep(vp, dy, g);
      if (vp.scrollX > g.maxScroll + 1) {
        // 越过留白锚点继续右拖：物化为更大的右侧留白（TV 手感）
        vp.rightGapBars = clamp(
          vp.rightGapBars + (vp.scrollX - g.maxScroll) / barW,
          0,
          RIGHT_GAP_BARS_MAX,
        );
        vp.scrollX = g.maxScroll;
        vp.follow = true;
      } else {
        vp.follow = dx < 0 && vp.scrollX >= g.maxScroll - 2;
      }
      syncFollow();
    };

    const endDrag = (e: PointerEvent) => {
      if (drag?.pointerId !== e.pointerId) return;
      const wasChart = drag.kind === "chart";
      drag = null;
      const vp = vpRef.current;
      vp.dragging = false;
      if (!wasChart) return;

      // 松手速度 = 采样窗内平均速度（px/s），交给 step 做惯性滑行
      const now = performance.now();
      samples.push({ t: now, x: e.clientX });
      const first = samples[0];
      const dt = (now - first.t) / 1000;
      if (dt > 0.016) {
        const v = (e.clientX - first.x) / dt;
        if (Math.abs(v) > 60 && !vp.follow) vp.velX = -v;
      }
      samples = [];
    };

    /** 指针离开画布（含 pointercancel）：撤掉十字光标 */
    const clearCrosshair = () => {
      vpRef.current.crosshair = null;
    };

    const onDblClick = (e: MouseEvent) => {
      const kind = zoneOf(e.clientX, e.clientY);
      const vp = vpRef.current;
      if (kind === "priceAxis" || kind === "timeAxis") {
        // 双击轴：价格轴回 Auto（TV 行为）；时间轴另加恢复默认柱宽
        vp.priceAutoFit = true;
        vp.centerPriceTarget = null;
        if (kind === "timeAxis") vp.zoomTargetX = 1;
        vp.anchor = null;
        vp.velX = 0;
        syncFollow();
        return;
      }
      // 双击图区：回到最新 + 默认缩放 + 价格轴回 Auto（一键复位，迷失后一步找回）
      resetFollow();
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", endDrag);
    el.addEventListener("pointercancel", endDrag);
    el.addEventListener("pointerleave", clearCrosshair);
    el.addEventListener("pointercancel", clearCrosshair);
    el.addEventListener("dblclick", onDblClick);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointermove", onPointerMove);
      el.removeEventListener("pointerup", endDrag);
      el.removeEventListener("pointercancel", endDrag);
      el.removeEventListener("pointerleave", clearCrosshair);
      el.removeEventListener("pointercancel", clearCrosshair);
      el.removeEventListener("dblclick", onDblClick);
    };
  }, [el, syncFollow, resetFollow]);

  return { vpRef, bindTarget, resetFollow, following, priceAutoFit, syncFollow, autoFit };
}

/**
 * 每帧物理推进（由主组件 rAF 调用）。返回 true 表示状态有变化需要重绘。
 * dt 单位秒。
 */
export function stepViewport(
  vp: ViewportState,
  g: GeomRef,
  dt: number,
  onFollowChange?: () => void,
): boolean {
  let changed = false;
  const frames = Math.max(0.25, dt * 60); // 换算为 60fps 基准帧数
  const k = 1 - Math.pow(1 - ZOOM_LERP_PER_FRAME, frames);

  // 1. 价格轴自动适配（TV 的价格轴 Auto）：每帧按可见范围续算纵向目标，
  //    横向缩放/滚动时价格轴自动跟上，不会把行情甩出屏幕
  if (vp.priceAutoFit) {
    const fit = priceFitOf(g);
    if (fit) {
      vp.zoomTargetY = fit.zoomY;
      vp.centerPriceTarget = fit.centerPrice;
    } else {
      // 可见范围内暂时无数据（切币/切周期清空的空窗期）：丢掉旧目标，
      // 否则会把上一个标的的价格追回来
      vp.centerPriceTarget = null;
    }
  }

  // 2. 缩放插值
  const needX = Math.abs(vp.zoomTargetX - vp.zoomX) > 1e-4;
  const needY = Math.abs(vp.zoomTargetY - vp.zoomY) > 1e-4;
  if (needX || needY) {
    const mx = vp.anchor?.mx ?? g.chartW / 2;
    const my = vp.anchor?.my ?? g.plotH / 2;

    if (needX) {
      let next = vp.zoomX + (vp.zoomTargetX - vp.zoomX) * k;
      if (Math.abs(vp.zoomTargetX - next) < 1e-4) next = vp.zoomTargetX;
      const bw0 = barWOf(vp.zoomX);
      const bw1 = barWOf(next);
      // 横向：锚定光标下的柱索引（follow 时贴右不动，布局层自动处理）
      if (!vp.follow) {
        const worldBar = (vp.scrollX + mx) / bw0;
        vp.scrollX = worldBar * bw1 - mx;
      }
      vp.zoomX = next;
    }
    if (needY) {
      let next = vp.zoomY + (vp.zoomTargetY - vp.zoomY) * k;
      if (Math.abs(vp.zoomTargetY - next) < 1e-4) next = vp.zoomTargetY;
      // 纵向锚点补偿只在手动态做：Auto 态的中心价由 centerPriceTarget 接管，
      // 两边同时写 centerPrice 会互相打架
      if (!vp.priceAutoFit) {
        const rh0 = rowHOf(vp.zoomY);
        const rh1 = rowHOf(next);
        // 锚定光标下的价格。基准中心优先用物理状态 vp.centerPrice——
        // layout 是上一帧的快照，外部改写后用它会把新中心覆盖回旧值
        const baseCenter = vp.centerPrice ?? g.centerPriceEff;
        const anchorPrice = baseCenter - ((my - g.plotH / 2) * g.tick) / rh0;
        vp.centerPrice = anchorPrice + ((my - g.plotH / 2) * g.tick) / rh1;
      }
      vp.zoomY = next;
    }
    if (vp.zoomX === vp.zoomTargetX && vp.zoomY === vp.zoomTargetY) vp.anchor = null;
    changed = true;
  }

  // 3. 中心价向自动适配目标平滑趋近（手动态 centerPriceTarget 恒为 null，此段不生效）
  if (vp.centerPriceTarget !== null) {
    const cur = vp.centerPrice ?? g.centerPriceEff;
    const diff = vp.centerPriceTarget - cur;
    if (Math.abs(diff) > g.tick * AUTOFIT_CENTER_EPS) {
      vp.centerPrice = cur + diff * k;
      changed = true;
    } else {
      vp.centerPrice = vp.centerPriceTarget;
    }
  }

  // 4. 惯性滑行
  if (!vp.dragging && Math.abs(vp.velX) > MIN_FLING_SPEED) {
    vp.scrollX += vp.velX * dt;
    vp.velX *= Math.pow(FRICTION_PER_FRAME, frames);

    // 边界：滑到留白锚点吸附 follow；滑出左界急停
    if (vp.scrollX >= g.maxScroll) {
      vp.scrollX = g.maxScroll;
      if (vp.velX > 0) {
        vp.velX = 0;
        if (!vp.follow) {
          vp.follow = true;
          onFollowChange?.();
        }
      }
    }
    const leftLimit = Math.min(g.maxScroll, 0) - g.chartW * 0.25;
    if (vp.scrollX <= leftLimit) {
      vp.scrollX = leftLimit;
      vp.velX = 0;
    }
    if (Math.abs(vp.velX) <= MIN_FLING_SPEED) vp.velX = 0;
    changed = true;
  }

  return changed;
}
