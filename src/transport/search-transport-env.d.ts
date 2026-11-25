import type { RouteAuthority } from "./route-authority";

export interface SearchTransportEnv {
  SEARCH_CONTROL: R2Bucket;
  ROUTE_AUTHORITY: DurableObjectNamespace<RouteAuthority>;
}
