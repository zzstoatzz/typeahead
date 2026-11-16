import { evalPage } from "../../src/pages/eval";
import { FAVICON_SVG } from "../../src/types";
import { readSnapshot } from "../../src/eval/archive";
import { ROOT } from "./archive";
import { directoryStore, publishLocal } from "./publish";
import { join } from "node:path";

if (import.meta.main) {
  await publishLocal();
  const store = directoryStore(join(ROOT, "published"));
  const server = Bun.serve({
    hostname: "127.0.0.1", port: 18790,
    async fetch(request) {
      const url = new URL(request.url);
      if (request.method !== "GET") return new Response("method not allowed", { status: 405 });
      if (url.pathname === "/favicon.svg") return new Response(FAVICON_SVG, { headers: { "Content-Type": "image/svg+xml" } });
      if (url.pathname === "/") return Response.redirect(new URL("/eval", url).href);
      if (url.pathname === "/stats" || url.pathname === "/docs") return Response.redirect(`https://typeahead.waow.tech${url.pathname}`);
      if (url.pathname !== "/eval" && url.pathname !== "/eval/data.json") return new Response("not found", { status: 404 });
      const { report, runs } = await readSnapshot(store, url.searchParams.get("run"));
      if (url.searchParams.has("run") && !report) return new Response("evaluation run not found", { status: 404 });
      if (url.pathname === "/eval/data.json") return Response.json(report, { headers: { "Cache-Control": "no-store" } });
      return new Response(evalPage(report, url.searchParams, runs), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    },
  });
  console.log(`local evaluation: ${server.url}eval`);
}
