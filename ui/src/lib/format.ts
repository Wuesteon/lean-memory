export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function formatCount(n: number): string {
  return n.toLocaleString();
}

/** retired / (latest + retired); 0 when there are no facts. */
export function supersessionRate(latest: number, retired: number): number {
  const total = latest + retired;
  return total === 0 ? 0 : retired / total;
}

export function formatPct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}

/** (latest + retired) / adds; null when adds == 0 (unknown, not zero). */
export function factsPerAdd(latest: number, retired: number, adds: number): number | null {
  if (adds === 0) return null;
  return (latest + retired) / adds;
}

export function formatTs(ms: number): string {
  return new Date(ms).toLocaleString();
}
