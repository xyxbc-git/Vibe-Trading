// 贾维斯系统信号接入（12 系统共识）：给足迹图解读面板提供「共振/分歧」参照。
// 只读 GET /api/twelve/consensus；后端未连接/接口异常时返回 null，调用方静默隐藏。
import { api, type SignalDirection, type TwelveConsensus } from "@/api/client";

/** 解读面板需要的共识精简视图 */
export interface SysConsensusLite {
  direction: SignalDirection;
  /** 0-100 */
  confidence: number;
  votes: { bullish: number; bearish: number; neutral: number };
}

export async function fetchSystemConsensus(
  symbol: string,
): Promise<SysConsensusLite | null> {
  try {
    const res = await api.twelveConsensus(symbol);
    const c: TwelveConsensus | null | undefined = res.consensus;
    if (!res.ok || !c) return null;
    return {
      direction: c.direction,
      confidence: c.confidence,
      votes: {
        bullish: c.votes?.bullish ?? 0,
        bearish: c.votes?.bearish ?? 0,
        neutral: c.votes?.neutral ?? 0,
      },
    };
  } catch {
    return null; // 后端不可用：静默隐藏该条解读，不打扰用户
  }
}
