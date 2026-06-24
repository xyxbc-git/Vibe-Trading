import { clsx } from "clsx";

interface GaugeChartProps {
  value: number;
  min?: number;
  max?: number;
  label: string;
  description?: string;
  size?: number;
}

export default function GaugeChart({
  value,
  min = -1,
  max = 1,
  label,
  description,
  size = 120,
}: GaugeChartProps) {
  const range = max - min;
  const normalized = Math.max(0, Math.min(1, (value - min) / range));
  const angle = -90 + normalized * 180;
  const r = size / 2 - 10;
  const cx = size / 2;
  const cy = size / 2;

  const needleX = cx + r * 0.7 * Math.cos((angle * Math.PI) / 180);
  const needleY = cy + r * 0.7 * Math.sin((angle * Math.PI) / 180);

  const color =
    value > 0.3
      ? "#3fb950"
      : value < -0.3
        ? "#f85149"
        : "#d29922";

  return (
    <div className="flex flex-col items-center">
      <svg width={size} height={size / 2 + 20} viewBox={`0 0 ${size} ${size / 2 + 20}`}>
        <path
          d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
          fill="none"
          stroke="#30363d"
          strokeWidth={8}
          strokeLinecap="round"
        />
        <path
          d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
          fill="none"
          stroke={color}
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${normalized * Math.PI * r} ${Math.PI * r}`}
        />
        <line
          x1={cx}
          y1={cy}
          x2={needleX}
          y2={needleY}
          stroke={color}
          strokeWidth={2}
          strokeLinecap="round"
        />
        <circle cx={cx} cy={cy} r={4} fill={color} />
        <text
          x={cx}
          y={cy + 16}
          textAnchor="middle"
          fill="#e6edf3"
          fontSize={14}
          fontWeight={600}
          fontFamily="SF Mono, monospace"
        >
          {typeof value === "number" ? value.toFixed(2) : value}
        </text>
      </svg>
      <span className="text-sm font-medium text-jarvis-text mt-1">{label}</span>
      {description && (
        <span
          className={clsx("text-xs mt-0.5", {
            "text-jarvis-green": value > 0.3,
            "text-jarvis-red": value < -0.3,
            "text-jarvis-yellow": value >= -0.3 && value <= 0.3,
          })}
        >
          {description}
        </span>
      )}
    </div>
  );
}
