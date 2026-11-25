import { bindings, defineConfig, exports, triggers, type CloudflareConfig } from "cf/config";

export const CONTROL_DB_ID = "a41ac6ae-0297-44ce-ba4c-066897d12f0d";
export const STATS_DB_ID = "624dc8c4-2a82-42f5-920c-6489466f7b7e";
export const KV_ID = "af614e8f42a44543920f7aad0ae7b70a";

const transportName = "typeahead-search-transport";
const rendererName = "typeahead-image-renderer";
const collectorName = "typeahead-eval-collector";
const appTraces = {
  enabled: true,
  headSamplingRate: 1,
  traces: { enabled: true, headSamplingRate: 0.05, destinations: ["logfire-typeahead-traces"] },
};
const kv = bindings.kv({ id: KV_ID });

export default defineConfig(({ mode }): CloudflareConfig => {
  switch (mode) {
    case undefined:
      return {
        worker: {
          name: "typeahead",
          entrypoint: "src/index.ts",
          compatibilityDate: "2024-12-01",
          compatibilityFlags: ["nodejs_compat"],
          workersDev: true,
          previewUrls: true,
          observability: appTraces,
          triggers: [triggers.scheduled({ schedule: "0 * * * *" })],
          env: {
            SERVICE_DID: bindings.text("did:plc:y6kywgnb7xletjpa5hhxmd4k"),
            PDS_UI_ENABLED: bindings.text("1"),
            SEARCH_CACHE_GENERATION: bindings.text("slots-20261114"),
            SEARCH_TRANSPORT: bindings.worker({ worker: transportName }),
            IMAGE_RENDERER: bindings.worker({ worker: rendererName }),
            KV: kv,
            STATS_DB: bindings.d1({ name: "typeahead-stats", id: STATS_DB_ID }),
            CONTROL_DB: bindings.d1({ name: "typeahead-control", id: CONTROL_DB_ID }),
            CF_VERSION_METADATA: bindings.versionMetadata(),
            RATE_LIMITER: bindings.rateLimit({ namespace: "0", simple: { limit: 60, period: 60 } }),
            RATE_LIMITER_STRICT: bindings.rateLimit({ namespace: "0", simple: { limit: 10, period: 60 } }),
          },
        },
      };
    case "transport":
      return {
        worker: {
          name: transportName,
          entrypoint: "src/transport/search-transport.ts",
          compatibilityDate: "2024-12-01",
          workersDev: true,
          previewUrls: false,
          observability: appTraces,
          env: {
            SEARCH_CONTROL: bindings.r2({ name: "typeahead-index" }),
            ROUTE_AUTHORITY: bindings.durableObject({ worker: transportName, exportName: "RouteAuthority" }),
          },
          exports: { RouteAuthority: exports.durableObject({ storage: "sqlite" }) },
        },
      };
    case "renderer":
      return {
        worker: {
          name: rendererName,
          entrypoint: "src/image-renderer/image-renderer.ts",
          compatibilityDate: "2026-11-04",
          workersDev: false,
          previewUrls: false,
          observability: { enabled: true, headSamplingRate: 1, traces: { enabled: true } },
          env: { KV: kv },
        },
      };
    case "collector":
      return {
        worker: {
          name: collectorName,
          entrypoint: "src/eval/eval-scheduler.ts",
          compatibilityDate: "2026-11-16",
          compatibilityFlags: ["global_fetch_strictly_public"],
          workersDev: false,
          previewUrls: false,
          triggers: [triggers.scheduled({ schedule: "2,17,32,47 * * * *" })],
          observability: { enabled: true },
          env: {
            EVAL_RUNNER: bindings.durableObject({ worker: collectorName, exportName: "EvaluationSchedule" }),
            KV: kv,
          },
          exports: { EvaluationSchedule: exports.durableObject({ storage: "sqlite" }) },
        },
      };
    default:
      throw new Error(`unknown Typeahead Worker mode: ${mode}`);
  }
});
