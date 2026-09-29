// A small line of recent values. Gaps (null) break the line rather than
// being drawn as zero.
interface Props {
  points: { t: number; value: number | null }[];
  min: number;
  max: number;
  width?: number;
  height?: number;
  label: string;
}

export function Sparkline({ points, min, max, width = 240, height = 48, label }: Props) {
  if (points.length < 2) return <div className="muted small">—</div>;
  const t0 = points[0]?.t ?? 0;
  const t1 = points[points.length - 1]?.t ?? t0 + 1;
  const span = Math.max(1, t1 - t0);
  const x = (t: number) => ((t - t0) / span) * width;
  const y = (v: number) =>
    height - ((Math.min(max, Math.max(min, v)) - min) / (max - min)) * height;
  let d = "";
  let pen = false;
  for (const point of points) {
    if (point.value === null) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x(point.t).toFixed(1)},${y(point.value).toFixed(1)}`;
    pen = true;
  }
  return (
    <svg
      className="sparkline"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label}
    >
      <path d={d} fill="none" strokeWidth={1.5} />
    </svg>
  );
}
