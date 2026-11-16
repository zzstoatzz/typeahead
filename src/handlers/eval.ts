import type { Env } from "../types";
import { readSnapshot } from "../eval/archive";
import { evalPage } from "../pages/eval";

export async function handleEval(request: Request, env: Pick<Env, "KV">): Promise<Response> {
  const url = new URL(request.url);
  const { report, runs } = await readSnapshot(env.KV, url.searchParams.get("run"));
  if (url.searchParams.has("run") && !report) return new Response("evaluation run not found", { status: 404 });
  if (url.pathname === "/eval/data.json") return Response.json(report, { headers: { "Cache-Control": "no-store" } });
  return new Response(evalPage(report, url.searchParams, runs), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}
