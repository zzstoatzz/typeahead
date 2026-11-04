const BG = "#0a0b0a";

export function handleWebManifest(): Response {
  const manifest = {
    name: "typeahead",
    short_name: "typeahead",
    description: "community actor search for atproto",
    start_url: "/",
    display: "standalone",
    background_color: BG,
    theme_color: BG,
    icons: [
      { src: "/favicon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
    ],
  };
  return new Response(JSON.stringify(manifest), {
    headers: { "Content-Type": "application/manifest+json", "Cache-Control": "public, max-age=86400" },
  });
}
