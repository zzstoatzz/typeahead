function fields(value: string, delimiter: string): string[] {
  const parts: string[] = [];
  let start = 0, quoted = false, escaped = false;
  for (let i = 0; i < value.length; i++) {
    const c = value[i];
    if (escaped) { escaped = false; continue; }
    if (quoted && c === "\\") { escaped = true; continue; }
    if (c === '"') quoted = !quoted;
    else if (!quoted && c === delimiter) { parts.push(value.slice(start, i)); start = i + 1; }
  }
  if (!quoted) parts.push(value.slice(start));
  return parts;
}

export function timingDurations(header: string | null): Map<string, number> {
  const durations = new Map<string, number>();
  if (!header || header.length > 8192) return durations;
  const seen = new Set<string>();
  for (const metric of fields(header, ",")) {
    const [rawName, ...parameters] = fields(metric, ";");
    const name = rawName.trim();
    if (seen.has(name)) { durations.delete(name); continue; }
    seen.add(name);
    const values = parameters.filter(p => /^\s*dur\s*=/i.test(p));
    if (values.length !== 1) continue;
    const match = /^\s*dur\s*=\s*(\d+(?:\.\d+)?)\s*$/i.exec(values[0]);
    if (!match) continue;
    const value = Number(match[1]);
    if (Number.isFinite(value) && value <= 60_000) durations.set(name, value);
  }
  return durations;
}

export const BACKEND_TIMINGS = ["engine", "transport_route", "transport_origin_headers", "transport_total", "transport_hint_wait", "transport_origin_start"] as const;

export function retainedBackendTiming(header: string | null): string {
  const values = timingDurations(header);
  return BACKEND_TIMINGS.flatMap(key => {
    const value = values.get(key);
    return value === undefined ? [] : [`${key};dur=${value}`];
  }).join(", ");
}
