import { handleOgImage, handleAppleTouchIcon } from "./handlers/og";

export default {
  async fetch(request: Request, env: Pick<ImageRendererEnv, "KV">): Promise<Response> {
    if (request.method !== "GET") return new Response("Not found", { status: 404 });
    const { pathname } = new URL(request.url);
    if (pathname === "/og-image") return handleOgImage(request, env);
    if (pathname === "/apple-touch-icon.png") return handleAppleTouchIcon();
    return new Response("Not found", { status: 404 });
  },
};
