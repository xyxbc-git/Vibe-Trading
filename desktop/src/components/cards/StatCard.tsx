import { clsx } from "clsx";
import type { ReactNode } from "react";

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: ReactNode;
  trend?: "up" | "down" | "neutral";
  subtitle?: string;
}

export default function StatCard({
  label,
  value,
  icon,
  trend,
  subtitle,
}: StatCardProps) {
  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="stat-label">{label}</span>
        {icon && <span className="text-jarvis-text-secondary">{icon}</span>}
      </div>
      <span
        className={clsx("stat-value", {
          "text-jarvis-green": trend === "up",
          "text-jarvis-red": trend === "down",
        })}
      >
        {value}
      </span>
      {subtitle && (
        <span
          className={clsx("text-xs", {
            "text-jarvis-green": trend === "up",
            "text-jarvis-red": trend === "down",
            "text-jarvis-text-secondary": trend === "neutral" || !trend,
          })}
        >
          {subtitle}
        </span>
      )}
    </div>
  );
}
