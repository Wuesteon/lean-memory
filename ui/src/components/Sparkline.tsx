import { Line, LineChart, ResponsiveContainer, Tooltip } from "recharts";

export default function Sparkline({
  adds,
  searches,
}: {
  adds: number;
  searches: number;
}) {
  const data = [
    { label: "adds", value: adds },
    { label: "searches", value: searches },
  ];
  if (adds === 0 && searches === 0) {
    return <div className="h-8 text-xs text-slate-400">no activity (7d)</div>;
  }
  return (
    <div className="h-8 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <Tooltip
            cursor={false}
            contentStyle={{ fontSize: 11 }}
            formatter={(v: number, _n, p) => [v, p.payload.label]}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="#0f172a"
            strokeWidth={1.5}
            dot={{ r: 2 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
