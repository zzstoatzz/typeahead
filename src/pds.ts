export function normalizePds(input: string): string | null {
  const host = input.replace(/^https?:\/\//i, "").replace(/\/$/, "").replace(/\.$/, "");
  if (!host.includes(".") || host.length > 253 || /[^a-z0-9.-]/i.test(host)) return null;
  if (!host.split(".").every((label) => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(label))) return null;
  return host.toLowerCase();
}

