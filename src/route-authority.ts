import { DurableObject } from "cloudflare:workers";
import { REFRESH_EVERY_MS, RouteCache, type ValidatedRoute } from "./route-cache";

/** One instance, created by the transport so it sits near it and R2 in eastern North
 *  America. It serves the routing record from memory and re-reads R2 every
 *  REFRESH_EVERY_MS while searches arrive, then goes idle. */
export class RouteAuthority extends DurableObject<SearchTransportEnv> {
  private readonly cache = new RouteCache(this.env.SEARCH_CONTROL);
  private ticking = false;

  async current(): Promise<ValidatedRoute> {
    const result = await this.cache.current();
    if (!this.ticking) {
      this.ticking = true;
      await this.ctx.storage.setAlarm(Date.now() + REFRESH_EVERY_MS);
    }
    return result;
  }

  async alarm(): Promise<void> {
    if (!this.cache.active()) {
      this.ticking = false;
      return;
    }
    await this.cache.refresh().catch(() => undefined);
    await this.ctx.storage.setAlarm(Date.now() + REFRESH_EVERY_MS);
  }
}
