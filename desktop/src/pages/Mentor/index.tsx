import { useState } from "react";
import { clsx } from "clsx";
import { GraduationCap, NotebookPen, BookOpenCheck, ShieldCheck } from "lucide-react";
import ErrorBoundary from "@/components/common/ErrorBoundary";
import PlanForm from "./PlanForm";
import VerdictCard from "./VerdictCard";
import LedgerPage from "./LedgerPage";
import RulesTab from "./RulesTab";
import { mentorApi, type MentorPlanInput, type MentorPlanResponse } from "@/api/mentor";

type Tab = "plan" | "ledger" | "rules";

/**
 * 情绪风控导师（任务 O）：
 * 下单前写计划 → 系统证据裁决红黄绿灯 → AI 小白话解释 → 事后复盘建立信任。
 */
export default function Mentor() {
  const [tab, setTab] = useState<Tab>("plan");
  const [submitting, setSubmitting] = useState(false);
  const [current, setCurrent] = useState<MentorPlanResponse | null>(null);
  const [submitErr, setSubmitErr] = useState<string | null>(null);

  async function handleSubmit(input: MentorPlanInput) {
    setSubmitting(true);
    setSubmitErr(null);
    try {
      const res = await mentorApi.submitPlan(input);
      setCurrent(res);
    } catch (e) {
      setSubmitErr(e instanceof Error ? e.message : "裁决失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <GraduationCap size={22} />
        情绪风控导师
      </h1>

      {/* 页内标签切换 */}
      <div className="mb-4 flex gap-1 rounded-lg border border-jarvis-border bg-jarvis-card p-1">
        {(
          [
            { key: "plan", label: "开单前 · 写计划", icon: <NotebookPen size={15} /> },
            { key: "ledger", label: "复盘 · 信任看板", icon: <BookOpenCheck size={15} /> },
            { key: "rules", label: "我的军规", icon: <ShieldCheck size={15} /> },
          ] as { key: Tab; label: string; icon: React.ReactNode }[]
        ).map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={clsx(
              "flex flex-1 items-center justify-center gap-1.5 rounded-md py-2 text-sm font-medium transition-colors",
              tab === t.key
                ? "bg-jarvis-blue/15 text-jarvis-blue"
                : "text-jarvis-text-secondary hover:text-jarvis-text",
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {tab === "plan" ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <PlanForm submitting={submitting} onSubmit={handleSubmit} />
          <div>
            {submitErr && (
              <div className="card mb-3 border-jarvis-red/40 text-sm text-jarvis-red">{submitErr}</div>
            )}
            {current ? (
              /* R1 热修：卡片级错误边界——后端字段形态漂移导致的渲染异常
                 只碎裁决卡（可点重新加载），不再拖崩整个导师页 */
              <ErrorBoundary fallbackTitle="裁决卡片渲染异常">
                <VerdictCard
                  planId={current.plan_id}
                  verdict={current.verdict}
                  mock={current.mock}
                  rulesLocal={current.rules_local}
                />
              </ErrorBoundary>
            ) : (
              <div className="card flex h-full min-h-[280px] flex-col items-center justify-center text-center">
                <GraduationCap size={40} className="mb-3 text-jarvis-text-secondary/50" />
                <p className="text-sm text-jarvis-text-secondary">
                  左边写完计划提交，导师的红黄绿灯裁决会出现在这里。
                </p>
                <p className="mt-1.5 max-w-[320px] text-xs text-jarvis-text-secondary/70">
                  绿灯=计划合格可执行；黄灯=有瑕疵谨慎；红灯=大概率情绪单，给你 15 分钟冷静期。
                  不锁你的手，只记你的账。
                </p>
              </div>
            )}
          </div>
        </div>
      ) : tab === "ledger" ? (
        <ErrorBoundary fallbackTitle="复盘台账渲染异常">
          <LedgerPage />
        </ErrorBoundary>
      ) : (
        <ErrorBoundary fallbackTitle="军规管理渲染异常">
          <RulesTab />
        </ErrorBoundary>
      )}
    </div>
  );
}
