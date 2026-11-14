import { expect, test } from "bun:test";
import { Miniflare } from "miniflare";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("public image routes preserve streamed responses and failures without bundling the renderer", async () => {
  const received: string[] = [];
  const image = Uint8Array.fromBase64("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=");
  const server = Bun.serve({ port: 0, fetch(request) {
    const url = new URL(request.url);
    if (!["/og-image", "/apple-touch-icon.png"].includes(url.pathname)) return new Response(null, { status: 503 });
    received.push(url.pathname + url.search);
    if (url.searchParams.has("fail")) return new Response("renderer unavailable", { status: 503 });
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(image.slice(0, 12));
      controller.enqueue(image.slice(12));
      controller.close();
    } }), { headers: { "Content-Type": "image/png", "Cache-Control": "public, max-age=3600" } });
  } });
  const directory = await mkdtemp(join(tmpdir(), "typeahead-image-routing-"));
  const entry = join(directory, "worker.ts");
  await writeFile(entry, `export {default} from ${JSON.stringify(new URL("./index.ts", import.meta.url).pathname)}`);
  const built = await Bun.build({ entrypoints: [entry], target: "browser", metafile: true,
    external: ["node:*", "cloudflare:workers"] });
  if (!built.success || !built.metafile) throw new Error(built.logs.join("\n"));
  expect(Object.keys(built.metafile.inputs).filter(path => path.includes("/workers-og/") || path.endsWith(".wasm"))).toEqual([]);
  const mf = new Miniflare({ workers: [
    { name: "public", modules: true, script: await built.outputs[0].text(),
      compatibilityDate: "2024-12-01", compatibilityFlags: ["nodejs_compat"],
      serviceBindings: { IMAGE_RENDERER: "images" } },
    { name: "images", modules: true, script: `export default {fetch(request) {
      const url = new URL(request.url);
      return fetch(new Request(new URL(url.pathname + url.search, ${JSON.stringify(server.url.toString())}), request));
    }}` },
  ] });
  try {
    for (const path of ["/og-image?apps=plyr.fm,pckt&range=1h", "/apple-touch-icon.png"]) {
      const response = await mf.dispatchFetch("https://public.test" + path);
      expect(response.status).toBe(200);
      expect(response.headers.get("Content-Type")).toBe("image/png");
      expect(response.headers.get("Cache-Control")).toBe("public, max-age=3600");
      expect(new Uint8Array(await response.arrayBuffer())).toEqual(image);
      const forwarded = new URL(received.at(-1) ?? "", "https://fixture.test");
      const expected = new URL(path, "https://public.test");
      expect(forwarded.pathname).toBe(expected.pathname);
      expect([...forwarded.searchParams]).toEqual([...expected.searchParams]);
    }
    const failed = await mf.dispatchFetch("https://public.test/og-image?fail=1");
    expect(failed.status).toBe(503);
    expect(await failed.text()).toBe("renderer unavailable");
    const calls = received.length;
    const manifest = await mf.dispatchFetch("https://public.test/site.webmanifest");
    expect(await manifest.json()).toMatchObject({ icons: [{ src: "/favicon.svg" }, { src: "/apple-touch-icon.png" }] });
    expect((await mf.dispatchFetch("https://public.test/llms.txt")).status).toBe(200);
    expect(received.length).toBe(calls);
  } finally { await mf.dispose(); server.stop(true); await rm(directory, { recursive: true, force: true }); }
}, 30000);
