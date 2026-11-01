import type { z } from "zod";
import type { Env } from "./types";
import { clientIP, json } from "./utils";

export function authorized(request: Request, env: Env): boolean {
  return request.headers.get("Authorization") === `Bearer ${env.ADMIN_SECRET}`;
}

export async function rejectUnauthorized(request: Request, env: Env, endpoint: string): Promise<Response> {
  const ip = clientIP(request);
  await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
  console.log(JSON.stringify({ event: "auth_failure", endpoint, ip }));
  return json({ error: "unauthorized" }, 401);
}

export async function parseBody<T>(request: Request, schema: z.ZodType<T>): Promise<T | null> {
  try {
    const result = schema.safeParse(await request.json());
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}
