// 合流仪表（Confluence HUD）数据契约与取数（C2 前端最小版）。
//
// 契约以《贾维斯-合流仪表-方案-20260813.md》§二/§三为准：
//   GET /api/confluence?symbol=&tf=
//   条目四态 pass/warn/fail/skipped + ⚡冲突态；四组权重 方向40/结构30/微观20/环境10；
//   skipped 不计分母，可用权重 <50 → insufficient=true（分数灰显挂「证据不足」）；
//   行为（冷静期/当日单数）与成本估算不进分，仅作行动闸口/徽章。
// 后端（jarvis_confluence.py，agent-8 并行开发）落地前：404 时回退演示数据
// （mock=true，UI 整卡挂「演示数据」角标，绝不冒充真实研判——D4 裁决）。

import { api, ApiError } from "./client";

export type ConfluenceState = "pass" | "warn" | "fail" | "skipped";
export type ConfluenceDirection = "bullish" | "bearish" | "neutral";

export interface ConfluenceItem {
  /** 条目键（c1_htf/c2_ltf/c3_bos/c4_sweep/c5_fvg/c6_delta/c7_reversal/c8_event/c9_funding） */
  key: string;
  /** 中文短名（≤6 字，长解释进 note） */
  name: string;
  state: ConfluenceState;
  /** 人话证据一句（引用行保留源模块行为描述原句） */
  note: string;
  /** ⚡冲突：引用证据与 HUD 方向矛盾（显式标出，绝不静默吞掉） */
  conflict?: boolean;
}

export interface ConfluenceGroup {
  key: "direction" | "structure" | "micro" | "environment";
  /** 方向合流/结构证据/微观确认/环境健康 */
  name: string;
  /** 预登记满权重（40/30/20/10） */
  weight: number;
  /** 已得分（未归一原始分） */
  earned: number;
  /** 可用权重（skipped 剔除后；0=整组数据缺失） */
  available: number;
  items: ConfluenceItem[];
}

export interface ConfluenceResponse {
  ok: boolean;
  error?: string;
  symbol?: string;
  tf?: string;
  direction: ConfluenceDirection;
  /** 0-100（按可用权重归一）；方向中性时为 null（整体置灰「无方向共识」） */
  score: number | null;
  /** 可用权重 <50 → true：分数灰显 +「证据不足」徽章 */
  insufficient: boolean;
  /** 可用权重合计（0-100） */
  availableWeight: number;
  groups: ConfluenceGroup[];
  /** C10 过路费 fail 时的强制红标文案（不进分但必须可见），如「该 TF 典型 R 下过路费 >1.0」 */
  costWarning?: string | null;
  /** fee/R 估算徽章文本（标「估算」），如「fee/R≈0.14（估算）」 */
  costEstimate?: string | null;
  /** 冷静期截止（epoch 秒）；无冷静期为 null（C12 行为闸口） */
  cooldownUntil?: number | null;
  /** 当日已提交计划数（展开态页脚小字） */
  todayPlans?: number;
  /** C11 格子战绩一句话；n<30 时后端给「不可判定（还差 k 笔）」 */
  gridStats?: string | null;
  /** 数据生成时刻（epoch 秒，页脚新鲜度） */
  updatedAt?: number;
  /** 演示数据（后端未就绪的 404 回退）——UI 必须整卡挂角标 */
  mock?: boolean;
}

/** 后端未就绪（404）时的演示数据：形状完整、语义克制，整卡挂「演示数据」角标。 */
export function mockConfluence(symbol: string, tf: string): ConfluenceResponse {
  return {
    ok: true,
    symbol,
    tf,
    direction: "bullish",
    score: 62,
    insufficient: false,
    availableWeight: 88,
    groups: [
      {
        key: "direction",
        name: "方向合流",
        weight: 40,
        earned: 30,
        available: 40,
        items: [
          { key: "c1_htf", name: "多周期", state: "pass", note: "30m/1h 与 4h 同向偏多（2/3）" },
          { key: "c2_ltf", name: "入场时机", state: "warn", note: "5m 短线与方向相反，等回踩" },
          { key: "c1_wyckoff_ctx", name: "阶段语境", state: "pass", note: "1h 吸筹 C 阶段，与偏多一致" },
        ],
      },
      {
        key: "structure",
        name: "结构证据",
        weight: 30,
        earned: 20,
        available: 30,
        items: [
          { key: "c3_bos", name: "结构突破", state: "pass", note: "近 8 根出现 SOS 强势信号" },
          { key: "c4_sweep", name: "流动性扫单", state: "warn", note: "未检出同向扫单事件" },
          { key: "c5_fvg", name: "FVG 折溢价", state: "pass", note: "折价区内存在未回补看涨缺口" },
        ],
      },
      {
        key: "micro",
        name: "微观确认",
        weight: 20,
        earned: 12,
        available: 20,
        items: [
          { key: "c6_delta", name: "Delta", state: "pass", note: "买方吸收，强度 medium（引用）" },
          { key: "c7_reversal", name: "反转四条件", state: "warn", note: "2/4 满足：背离+扫单，分布未确认" },
        ],
      },
      {
        key: "environment",
        name: "环境健康",
        weight: 10,
        earned: 5,
        available: 8,
        items: [
          { key: "c8_event", name: "事件窗口", state: "skipped", note: "事件日历未配置" },
          { key: "c9_funding", name: "资金费", state: "pass", note: "费率 0.01%/8h，不拥挤" },
        ],
      },
    ],
    costWarning: null,
    costEstimate: "fee/R≈0.18（估算）",
    cooldownUntil: null,
    todayPlans: 0,
    gridStats: "该格子 n<30，不可判定（还差 17 笔）",
    updatedAt: Math.floor(Date.now() / 1000),
    mock: true,
  };
}

/** 取合流数据；后端 404（端点未部署）→ 演示数据；ok:false 与其余错误向上抛
 *（usePolling 保留旧 data + 置 error → 组件保持上一态并亮 ⚠ 旧值标）。
 * 契约已与后端实测对账（jarvis_confluence.assess 直调样本 2026-08-13）：
 * 顶层/组/条目字段名、c1_htf~c9_funding 条目键、conflict 布尔均一致。 */
export async function fetchConfluence(symbol: string, tf: string): Promise<ConfluenceResponse> {
  let resp: ConfluenceResponse;
  try {
    resp = await api.get<ConfluenceResponse>(
      `/confluence?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,
    );
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return mockConfluence(symbol, tf);
    throw e;
  }
  if (!resp.ok) throw new Error(resp.error || "合流评分计算失败");
  return resp;
}
