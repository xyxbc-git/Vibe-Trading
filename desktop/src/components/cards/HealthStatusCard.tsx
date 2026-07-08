import { Activity, Bell, Database, Shield, Server, Clock } from "lucide-react";
import type { HealthStatus } from "@/api/client";

function Dot({ ok }: { ok?: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${
        ok ? "bg-jarvis-green" : "bg-jarvis-red"
      }`}
    />
  );
}

function Row({
  label,
  ok,
  detail,
  icon,
}: {
  label: string;
  ok?: boolean;
  detail?: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-2 py-1.5 border-b border-jarvis-border/50 last:border-0">
      <span className="text-jarvis-text-secondary mt-0.5">{icon}</span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <Dot ok={ok} />
          <span className="text-sm text-jarvis-text">{label}</span>
        </div>
        {detail && (
          <p className="text-xs text-jarvis-text-secondary mt-0.5 truncate" title={detail}>
            {detail}
          </p>
        )}
      </div>
    </div>
  );
}

export default function HealthStatusCard({
  health,
  loading,
  error,
}: {
  health: HealthStatus | null;
  loading?: boolean;
  error?: string | null;
}) {
  const checks = health?.checks ?? {};
  const journal = checks.journal;
  const alert = checks.price_alert;
  const daemon = checks.daemon;
  const cb = checks.circuit_breaker;
  const qd = checks.qd_gateway;

  const daemonDetail = daemon?.available
    ? `末轮 ${daemon.finished_at ?? daemon.started_at ?? "—"}`
    : String(daemon?.reason ?? "未运行");

  const alertDetail = alert?.running
    ? `监控中 · 末检 ${alert.last_run ?? "—"}`
    : alert?.last_error
      ? `异常: ${String(alert.last_error).slice(0, 80)}`
      : "监控未启动";

  const cbDetail = cb?.tripped
    ? `已熔断 · 回撤 ${cb.drawdown_pct ?? "?"}%`
    : cb?.should_halt
      ? `预警 · 回撤 ${cb.drawdown_pct ?? "?"}%`
      : `正常 · 权益 ${cb?.equity_usdt ?? "—"} U`;

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Activity size={14} />
          系统健康
        </p>
        <span className="text-xs text-jarvis-text-secondary">
          {loading ? "检测中…" : health?.ts ?? (error ? "不可用" : "—")}
        </span>
      </div>

      {error ? (
        <p className="text-sm text-jarvis-red">{error}</p>
      ) : loading && !health ? (
        <p className="text-sm text-jarvis-text-secondary">加载健康状态…</p>
      ) : (
        <>
          <div
            className={`text-sm font-medium mb-2 ${
              health?.ok ? "text-jarvis-green" : "text-jarvis-yellow"
            }`}
          >
            {health?.ok ? "核心服务正常" : "部分检查未通过"}
          </div>
          <Row
            label="决策库 SQLite"
            ok={journal?.ok}
            detail={journal?.ok ? "journal.db 可读写" : journal?.error}
            icon={<Database size={14} />}
          />
          <Row
            label="价位提醒监控"
            ok={alert?.ok}
            detail={alertDetail}
            icon={<Bell size={14} />}
          />
          <Row
            label="Daemon 心跳"
            ok={daemon?.ok}
            detail={daemonDetail}
            icon={<Clock size={14} />}
          />
          <Row
            label="组合熔断"
            ok={cb?.ok}
            detail={cbDetail}
            icon={<Shield size={14} />}
          />
          <Row
            label="QD 网关（可选）"
            ok={qd?.ok}
            detail={qd?.ok ? "在线" : String(qd?.error ?? "离线/未配置")}
            icon={<Server size={14} />}
          />
          {health?.log_buffer_size != null && (
            <p className="text-xs text-jarvis-text-secondary mt-2">
              日志缓冲 {health.log_buffer_size} 行
            </p>
          )}
        </>
      )}
    </div>
  );
}
