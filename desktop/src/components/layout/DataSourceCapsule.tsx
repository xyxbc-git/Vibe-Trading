import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Database, Loader2 } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api } from "@/api/client";

/**
 * 顶栏数据源胶囊（任务 J2）：当前源 + 健康点，点开切换 auto / 币安 / OKX。
 *
 * - 健康点：绿=生效源可达；黄=策略屏蔽/降级（走缓存）；红=封禁或探测失败。
 * - 手动切换走 POST /api/datasource/switch「先探测后生效」：后端探测目标源
 *   （币安封禁期短路不真发），失败不切换——下拉内联提示原因，防止盲切导致
 *   数据更新不及时。状态 30s 轮询（隐藏页自动暂停由 usePolling 全局语义保证）。
 */

interface ProbeResult {
  ok: boolean;
  source?: string;
  latency_ms?: number;
  reason?: string;
  short_circuit?: boolean;
  at?: number;
}

interface SourceHealth {
  host: string;
  banned_until: number;
  banned_until_hms: string | null;
  last_probe?: ProbeResult;
  used_weight_1m?: { used_1m: number; age_s: number };
}

interface DsStatus {
  ok: boolean;
  mode: "auto" | "binance" | "okx";
  policy?: { policy_blocked?: string[] };
  sources?: {
    binance_futures?: SourceHealth;
    binance_spot?: SourceHealth;
    okx?: SourceHealth;
  };
}

interface SwitchResp {
  ok: boolean;
  mode?: string;
  reason?: string;
  probe?: ProbeResult;
}

const MODE_LABEL: Record<string, string> = {
  auto: "自动",
  binance: "币安",
  okx: "OKX",
};

const MODE_DESC: Record<string, string> = {
  auto: "币安主源，故障自动回退 OKX（推荐）",
  binance: "锁定币安：不自动切 OKX",
  okx: "费率/OI/价格优先 OKX；K线仍走币安",
};

export default function DataSourceCapsule() {
  const { data: st, refetch } = usePolling(
    () => api.get<DsStatus>("/datasource/status"),
    30_000,
  );
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) setMsg(null);
  }, [open]);

  const mode = st?.mode ?? "auto";
  const fut = st?.sources?.binance_futures;
  const okx = st?.sources?.okx;

  // 健康点：生效源被封禁/探测失败=红；策略屏蔽存在或生效源仅靠缓存=黄；其余=绿
  const now = Date.now() / 1000;
  const futBanned = !!fut && fut.banned_until > now;
  const okxBanned = !!okx && okx.banned_until > now;
  const activeBanned = mode === "okx" ? okxBanned : futBanned;
  const anyPolicy = (st?.policy?.policy_blocked?.length ?? 0) > 0;
  const dot = !st
    ? "bg-jarvis-text-secondary"
    : activeBanned
      ? "bg-jarvis-red"
      : anyPolicy || (mode !== "okx" && okxBanned) || (mode === "okx" && futBanned)
        ? "bg-jarvis-yellow"
        : "bg-jarvis-green";

  const doSwitch = async (m: string) => {
    if (busy) return;
    setBusy(m);
    setMsg(null);
    try {
      const res = await api.post<SwitchResp>("/datasource/switch", { mode: m });
      if (res.ok) {
        const lat = res.probe?.latency_ms;
        setMsg({
          text:
            `已切换到「${MODE_LABEL[m] ?? m}」` +
            (lat != null ? `，探测延迟 ${lat}ms` : "（自动模式，未探测）"),
          ok: true,
        });
        refetch();
      } else {
        setMsg({ text: res.reason ?? "目标源异常，已保持原源", ok: false });
      }
    } catch (e) {
      // 409=探测失败不切换（业务语义）；其余为网络/后端异常
      setMsg({
        text: e instanceof Error ? e.message : "切换失败：目标源异常，已保持原源",
        ok: false,
      });
    } finally {
      setBusy(null);
    }
  };

  const bannedTip = [
    futBanned && fut?.banned_until_hms ? `币安封禁至 ${fut.banned_until_hms}` : null,
    okxBanned && okx?.banned_until_hms ? `OKX 屏蔽至 ${okx.banned_until_hms}` : null,
  ]
    .filter(Boolean)
    .join("；");

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 bg-jarvis-bg border border-jarvis-border rounded-md pl-2.5 pr-2 py-1 text-xs font-mono text-jarvis-text hover:border-jarvis-blue focus:outline-none focus:border-jarvis-blue cursor-pointer"
        title={`行情数据源：${MODE_DESC[mode]}${bannedTip ? `（${bannedTip}）` : ""}`}
      >
        <Database size={12} className="text-jarvis-text-secondary" />
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${dot}`} />
        {MODE_LABEL[mode] ?? mode}
        <ChevronDown
          size={12}
          className={`text-jarvis-text-secondary transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1.5 w-64 bg-jarvis-card border border-jarvis-border rounded-lg shadow-2xl z-50 overflow-hidden">
          <div className="py-1">
            {(["auto", "binance", "okx"] as const).map((m) => (
              <button
                key={m}
                onClick={() => doSwitch(m)}
                disabled={busy !== null}
                className="w-full flex items-center justify-between px-3 py-1.5 text-xs text-left text-jarvis-text hover:bg-white/5 disabled:opacity-50"
              >
                <span className="flex flex-col">
                  <span className="font-mono">{MODE_LABEL[m]}</span>
                  <span className="text-[10px] text-jarvis-text-secondary">
                    {MODE_DESC[m]}
                  </span>
                </span>
                {busy === m ? (
                  <Loader2 size={12} className="animate-spin text-jarvis-blue" />
                ) : (
                  m === mode && <Check size={12} className="text-jarvis-blue" />
                )}
              </button>
            ))}
          </div>
          <div className="border-t border-jarvis-border px-3 py-2 space-y-1">
            <p className="text-[10px] leading-snug text-jarvis-text-secondary">
              币安:{" "}
              {futBanned ? `封禁至 ${fut?.banned_until_hms}` : "可达"}
              {" · "}OKX: {okxBanned ? `屏蔽至 ${okx?.banned_until_hms}` : "可达"}
            </p>
            {msg && (
              <p
                className={`text-[11px] leading-snug ${msg.ok ? "text-jarvis-green" : "text-jarvis-red"}`}
              >
                {msg.text}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
