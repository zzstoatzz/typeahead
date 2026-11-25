import type { ImageRendererEnv } from "./image-renderer-env";
import { handleOgImage, handleAppleTouchIcon, handleFirstImage } from "./og";

export default {
  async fetch(request: Request, env: ImageRendererEnv): Promise<Response> {
    if (request.method !== "GET") return new Response("Not found", { status: 404 });
    const { pathname } = new URL(request.url);
    if (pathname === "/og-image") return handleOgImage(request, env);
    if (pathname === "/og-image/first") return handleFirstImage(request);
    if (pathname === "/apple-touch-icon.png") return handleAppleTouchIcon();
    return new Response("Not found", { status: 404 });
  },
};
