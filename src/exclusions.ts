import { z } from "zod";

export const EXCLUSIONS_KEY = "search_exclusions";
export const EXCLUSIONS_REFRESH_MS = 23 * 3_600_000;

const exclusionsSchema = z.object({
  version: z.literal(2),
  as_of: z.string(),
  policy_hidden: z.number().int().nonnegative(),
  stale_hidden_flags: z.number().int().nonnegative(),
  handle_invalid: z.number().int().nonnegative(),
  quarantined: z.number().int().nonnegative(),
});
export type SearchExclusions = z.infer<typeof exclusionsSchema>;

export function parseExclusions(raw: string | null): SearchExclusions | null {
  if (raw === null) return null;
  try {
    const parsed = exclusionsSchema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

export type ExclusionsKv = {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
};

/** Whether the stored count is missing or a day old. */
export async function exclusionsDue(kv: ExclusionsKv, now: Date): Promise<boolean> {
  const previous = parseExclusions(await kv.get(EXCLUSIONS_KEY));
  return !previous || now.getTime() - Date.parse(previous.as_of) >= EXCLUSIONS_REFRESH_MS;
}
