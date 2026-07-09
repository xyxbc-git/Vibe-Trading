import type { StrategyGenResult } from "@/api/client";

/**
 * AI 生成策略的大白话方案摘要：解释 + 用到的信号 + 方向/进场/止盈止损 + 自动修正记录。
 * AI 策略工坊页与回测页「AI 帮我写」面板共用。
 */
export default function AIStrategySummary({ result }: { result: StrategyGenResult }) {
  return (
    <div>
      {result.explain && (
        <p className="text-sm text-jarvis-text bg-jarvis-bg rounded-md p-3 mb-3 leading-relaxed">
          {result.explain}
        </p>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <div className="bg-jarvis-bg rounded-md p-3">
          <p className="text-xs text-jarvis-text-secondary mb-2">用到的信号</p>
          <div className="space-y-1.5">
            {result.summary?.factors.map((f) => (
              <div key={f.id} className="text-sm">
                <span className="text-jarvis-text font-medium">{f.name}</span>
                <span className="text-xs text-jarvis-text-secondary ml-2">
                  {f.description}
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="bg-jarvis-bg rounded-md p-3 text-sm space-y-1.5">
          <div className="flex justify-between">
            <span className="text-jarvis-text-secondary text-xs">交易方向</span>
            <span className="text-jarvis-text">{result.summary?.direction}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-jarvis-text-secondary text-xs">进场条件</span>
            <span className="text-jarvis-text">{result.summary?.logic}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-jarvis-text-secondary text-xs">止损</span>
            <span className="text-jarvis-text">{result.summary?.stop_loss}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-jarvis-text-secondary text-xs">止盈</span>
            <span className="text-jarvis-text">{result.summary?.take_profit}</span>
          </div>
        </div>
      </div>

      {(result.issues?.length ?? 0) > 0 && (
        <p className="text-xs text-jarvis-yellow mt-2">
          自动修正：{result.issues?.join("；")}
        </p>
      )}
    </div>
  );
}
