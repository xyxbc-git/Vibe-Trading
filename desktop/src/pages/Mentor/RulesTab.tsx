import { useEffect, useMemo, useState } from "react";
import { clsx } from "clsx";
import { ShieldCheck, Plus, Trash2, Save, RotateCcw, RefreshCw } from "lucide-react";
import {
  mentorApi,
  DEFAULT_RULES,
  RULE_PARAM_CN,
  type MentorRule,
  type MentorRulePatch,
} from "@/api/mentor";

/**
 * V2/V4「我的军规」管理页——对齐后端真实契约：
 * GET /mentor/rules?all=1 → {rule_id, title, rtype, params(dict), enabled(int)}；
 * 保存走单条 POST upsert（diff 出改动行逐条提交，成功后重拉验证）。
 * 后端不可达时落本地种子 8 条（mock 模式，联调后自动切真）。
 */
export default function RulesTab() {
  const [rules, setRules] = useState<MentorRule[]>([]);
  /** 服务端原始快照（diff 用；id → 行） */
  const [baseline, setBaseline] = useState<Map<string, MentorRule>>(new Map());
  const [loading, setLoading] = useState(true);
  const [mock, setMock] = useState(false);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [newText, setNewText] = useState("");

  async function fetchRules() {
    setLoading(true);
    const res = await mentorApi.rules();
    setRules(res.rules);
    setBaseline(new Map(res.rules.map((r) => [r.id, JSON.parse(JSON.stringify(r)) as MentorRule])));
    setMock(Boolean(res.mock));
    setLoading(false);
  }

  useEffect(() => {
    void fetchRules();
  }, []);

  /** 改动行集合（enabled/params/title 任一变化） */
  const dirtyIds = useMemo(() => {
    const out: string[] = [];
    for (const r of rules) {
      const b = baseline.get(r.id);
      if (!b) {
        out.push(r.id); // 新增行（本地暂存，保存时走无 rule_id 的 upsert）
        continue;
      }
      if (
        b.enabled !== r.enabled ||
        b.title !== r.title ||
        JSON.stringify(b.params) !== JSON.stringify(r.params)
      ) {
        out.push(r.id);
      }
    }
    return out;
  }, [rules, baseline]);

  function patchRow(id: string, p: Partial<MentorRule>) {
    setRules((prev) => prev.map((r) => (r.id === id ? { ...r, ...p } : r)));
    setNote(null);
  }

  function patchParam(id: string, key: string, value: number) {
    setRules((prev) =>
      prev.map((r) => (r.id === id ? { ...r, params: { ...r.params, [key]: value } } : r)),
    );
    setNote(null);
  }

  function addCustom() {
    const text = newText.trim();
    if (!text) return;
    setRules((prev) => [
      ...prev,
      {
        id: `new-${Date.now()}`, // 临时 id；保存后由后端分配 U-<ts>
        title: text,
        rtype: "custom",
        params: {},
        enabled: true,
        builtin: false,
        local: mock,
      },
    ]);
    setNewText("");
    setNote(null);
  }

  function removeLocal(id: string) {
    // 后端无删除接口：仅未保存的新增行与 mock 本地行可移除
    setRules((prev) => prev.filter((r) => r.id !== id));
    setNote(null);
  }

  async function save() {
    if (!dirtyIds.length) return;
    setSaving(true);
    setNote(null);
    const fails: string[] = [];
    for (const id of dirtyIds) {
      const r = rules.find((x) => x.id === id);
      if (!r) continue;
      const isNew = !baseline.has(id);
      const patch: MentorRulePatch = isNew
        ? { title: r.title, params: r.params, enabled: r.enabled }
        : { rule_id: r.id, title: r.builtin ? undefined : r.title, params: r.params, enabled: r.enabled };
      const res = await mentorApi.upsertRule(patch);
      if (!res.ok) fails.push(`${r.title}：${res.error ?? "保存失败"}`);
    }
    // 保存后重拉验证（真后端回读确认改动落库；mock 模式回读本地副本）
    await fetchRules();
    setSaving(false);
    setNote(
      fails.length
        ? { kind: "err", text: `部分保存失败：${fails.join("；")}` }
        : { kind: "ok", text: mock ? "已保存到本地（军规接口待联调，届时自动同步）" : "已保存并回读验证" },
    );
  }

  function resetDefaults() {
    if (mock) {
      const seed = DEFAULT_RULES.map((r) => ({ ...r, params: { ...r.params } }));
      mentorApi.saveRulesLocal(seed);
      setRules(seed);
      setBaseline(new Map(seed.map((r) => [r.id, JSON.parse(JSON.stringify(r)) as MentorRule])));
      setNote({ kind: "ok", text: "已恢复本地默认 8 条" });
    } else {
      // 服务端模式：把内置行参数改回后端种子语义属后端职责，这里仅重拉放弃本地未保存改动
      void fetchRules();
      setNote({ kind: "ok", text: "已放弃未保存改动，重新拉取服务端军规" });
    }
  }

  return (
    <div className="card">
      <div className="mb-1 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-base font-semibold text-jarvis-text">
          <ShieldCheck size={18} className="text-jarvis-blue" />
          我的军规 · 你给自己定的下单规矩
        </h2>
        {mock && (
          <span className="rounded-full border border-jarvis-yellow/50 px-2 py-0.5 text-[10px] text-jarvis-yellow">
            本地数据 · 待后端联调
          </span>
        )}
      </div>
      <p className="mb-4 text-xs text-jarvis-text-secondary">
        每次提交计划，导师都会拿这份军规逐条核对（裁决卡「军规核对」区）。参数可改、不想要的可停用——但规矩是你自己定的，破了账也记你头上。
      </p>

      {loading ? (
        <p className="py-10 text-center text-sm text-jarvis-text-secondary">加载中…</p>
      ) : (
        <div className="space-y-2">
          {rules.map((r, i) => {
            const paramKeys = Object.keys(r.params).filter(
              (k) => typeof r.params[k] === "number",
            );
            const roKeys = Object.keys(r.params).filter(
              (k) => typeof r.params[k] !== "number",
            );
            const removable = !baseline.has(r.id) || (mock && !r.builtin);
            return (
              <div
                key={r.id}
                className={clsx(
                  "flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-2.5",
                  r.enabled
                    ? "border-jarvis-border bg-jarvis-bg/60"
                    : "border-jarvis-border/40 bg-jarvis-bg/30 opacity-60",
                )}
              >
                <span className="w-8 shrink-0 text-center font-mono text-[10px] text-jarvis-text-secondary">
                  {/^R\d/i.test(r.id) ? r.id : i + 1}
                </span>
                <div className="min-w-0 flex-1 basis-48">
                  <p className="text-sm text-jarvis-text">
                    {r.title}
                    {!r.builtin && (
                      <span className="ml-2 rounded border border-jarvis-border px-1 text-[10px] text-jarvis-text-secondary">
                        自定义
                      </span>
                    )}
                  </p>
                  {roKeys.length > 0 && (
                    <p className="mt-0.5 text-[10px] text-jarvis-text-secondary">
                      {roKeys
                        .map((k) => `${RULE_PARAM_CN[k] ?? k}: ${JSON.stringify(r.params[k])}`)
                        .join(" · ")}
                    </p>
                  )}
                </div>
                {/* 数值参数：按 params 键动态生成输入框 */}
                {paramKeys.map((k) => (
                  <label key={k} className="flex shrink-0 items-center gap-1">
                    <span className="text-[10px] text-jarvis-text-secondary">
                      {RULE_PARAM_CN[k] ?? k}
                    </span>
                    <input
                      type="number"
                      step="any"
                      value={Number(r.params[k])}
                      onChange={(e) => {
                        const v = Number(e.target.value);
                        if (Number.isFinite(v)) patchParam(r.id, k, v);
                      }}
                      className="w-16 rounded-md border border-jarvis-border bg-jarvis-bg px-2 py-1 text-right font-mono text-xs text-jarvis-text outline-none focus:border-jarvis-blue"
                    />
                  </label>
                ))}
                {/* 启用开关 */}
                <button
                  type="button"
                  onClick={() => patchRow(r.id, { enabled: !r.enabled })}
                  className={clsx(
                    "relative h-5 w-9 shrink-0 rounded-full transition-colors",
                    r.enabled ? "bg-jarvis-green" : "bg-jarvis-border",
                  )}
                  aria-label={r.enabled ? "停用" : "启用"}
                >
                  <span
                    className={clsx(
                      "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all",
                      r.enabled ? "left-[18px]" : "left-0.5",
                    )}
                  />
                </button>
                {removable ? (
                  <button
                    type="button"
                    onClick={() => removeLocal(r.id)}
                    className="shrink-0 text-jarvis-text-secondary transition-colors hover:text-jarvis-red"
                    aria-label="移除"
                  >
                    <Trash2 size={14} />
                  </button>
                ) : (
                  <span className="w-[14px] shrink-0" title="服务端军规不可删除，可停用" />
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 新增自定义军规（文本型：裁决卡以提醒形式展示；保存后由后端分配 U-id） */}
      <div className="mt-3 flex items-center gap-2">
        <input
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addCustom()}
          placeholder="写一条自己的军规，如：跌破日线 MA20 不做多"
          className="flex-1 rounded-lg border border-jarvis-border bg-jarvis-bg px-3 py-2 text-sm text-jarvis-text outline-none focus:border-jarvis-blue"
        />
        <button
          type="button"
          onClick={addCustom}
          disabled={!newText.trim()}
          className={clsx(
            "flex items-center gap-1 rounded-lg border px-3 py-2 text-sm transition-colors",
            newText.trim()
              ? "border-jarvis-blue/50 text-jarvis-blue hover:bg-jarvis-blue/10"
              : "cursor-not-allowed border-jarvis-border text-jarvis-text-secondary",
          )}
        >
          <Plus size={14} /> 新增
        </button>
      </div>
      <p className="mt-1 text-[11px] text-jarvis-text-secondary">
        自定义军规为文本提醒（裁决时原文展示）；数值判定类军规由后端引擎按参数核对。服务端军规无删除接口，不需要的请停用。
      </p>

      {/* 保存 / 重置 */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={!dirtyIds.length || saving}
          className={clsx(
            "flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
            dirtyIds.length && !saving
              ? "bg-jarvis-blue text-jarvis-accent-fg hover:bg-jarvis-blue/80"
              : "cursor-not-allowed bg-jarvis-border/60 text-jarvis-text-secondary",
          )}
        >
          <Save size={14} /> {saving ? "逐条保存中…" : `保存军规${dirtyIds.length ? `（${dirtyIds.length} 条改动）` : ""}`}
        </button>
        <button
          type="button"
          onClick={resetDefaults}
          className="flex items-center gap-1.5 rounded-lg border border-jarvis-border px-3 py-2 text-sm text-jarvis-text-secondary transition-colors hover:text-jarvis-text"
        >
          {mock ? <RotateCcw size={13} /> : <RefreshCw size={13} />}
          {mock ? "恢复默认 8 条" : "放弃改动重新拉取"}
        </button>
        {note && (
          <span className={clsx("text-xs", note.kind === "ok" ? "text-jarvis-green" : "text-jarvis-red")}>
            {note.text}
          </span>
        )}
        {dirtyIds.length > 0 && !note && (
          <span className="text-xs text-jarvis-yellow">有 {dirtyIds.length} 条未保存的修改</span>
        )}
      </div>
    </div>
  );
}
