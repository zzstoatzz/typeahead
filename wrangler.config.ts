import { defineWranglerConfig } from "wrangler/experimental-config";

export default defineWranglerConfig(({ mode }) => mode === undefined
  ? { minify: true, uploadSourceMaps: true, types: { generate: false } }
  : { types: { generate: false } });
