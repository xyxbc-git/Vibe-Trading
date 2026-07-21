// 订单流足迹图（Footprint Chart）公共类型契约 · v2（多币种 + 6 周期）。
//
// 本文件是数据层（src/lib/footprint/**）与图表渲染层（src/components/**）
// 两侧的共同契约，接口定义经主控锁定，双方不得单方面变更字段。
// v2 变更：Timeframe 扩为 6 档；FootprintBar 增加 symbol；Service 两方法
// 首参增加 symbol（旧签名由 dataService 提供兼容重载，缺 symbol 默认
// BTCUSDT 并 console.warn，过渡期后移除）。
// v2.1 变更（真实行情接入）：FootprintBar 增加可选 ohlcOnly——真实源的
// 远期历史仅有 OHLCV（无逐笔），levels 为空、该标记为 true，UI 退化画蜡烛。
//
// 口径说明：
//   bidVol = 主动卖量（打在 bid 上的市价卖单，足迹格左列）
//   askVol = 主动买量（打在 ask 上的市价买单，足迹格右列）
//   delta  = ΣaskVol − ΣbidVol（正 = 买方主导）
//   cumDelta = 同 (symbol,timeframe) 序列内跨柱累计 delta
//   poc    = Point of Control，成交量（bidVol+askVol）最大的价位

/** 逐笔成交，time 毫秒 */
export interface Trade {
  time: number;
  price: number;
  size: number;
  side: 'buy' | 'sell';
}

/** 单个价位的买卖量分层。bidVol=主动卖量(左列) askVol=主动买量(右列) */
export interface PriceLevel {
  price: number;
  bidVol: number;
  askVol: number;
}

export interface FootprintBar {
  symbol: string;           // 交易对，如 BTCUSDT
  time: number;             // 柱开始时间戳(毫秒)
  timeframe: '1m' | '5m' | '15m' | '30m' | '4h' | '1d';
  open: number;
  high: number;
  low: number;
  close: number;
  levels: PriceLevel[];     // 按 price 降序
  totalVol: number;         // Σ(bidVol+askVol)
  delta: number;            // ΣaskVol - ΣbidVol
  cumDelta: number;         // 会话累计 delta
  poc: number;              // 成交量最大的价位
  /**
   * 仅 OHLCV 无逐笔明细（真实源大周期远期历史，来自 K 线接口）：
   * levels 为空数组、poc 取 close、delta 用 taker 买卖量反推。
   * UI 侧遇到该标记退化为蜡烛画法。缺省（undefined）= 完整足迹柱。
   */
  ohlcOnly?: boolean;
}

export interface FootprintDataService {
  getBars(
    symbol: string,
    timeframe: FootprintBar['timeframe'],
    from: number,
    to: number,
  ): Promise<FootprintBar[]>;
  /** 返回退订函数；同柱更新以 time 相同判定 */
  subscribe(
    symbol: string,
    timeframe: FootprintBar['timeframe'],
    cb: (bar: FootprintBar) => void,
  ): () => void;
}

/** 时间周期别名（辅助类型，等价于 FootprintBar['timeframe']） */
export type Timeframe = FootprintBar['timeframe'];
