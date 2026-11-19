import { parseRoute, ROUTE_KEY } from "./search-route";

export type Route = ReturnType<typeof parseRoute>;
export type ValidatedRoute = { route: Route; validatedAt: number };

/** How old a route may be when a search relies on it: the same one second
 *  the transport has always allowed between R2 reads. */
export const ROUTE_LIFETIME_MS = 1000;
/** The refresher re-reads R2 this often, well inside the lifetime, so a
 *  search rarely waits for R2 even when one read is slow. */
export const REFRESH_EVERY_MS = 700;
/** With no search for this long the refresher stops, and the next search
 *  pays for one R2 read and restarts it. */
export const IDLE_AFTER_MS = 5 * 60_000;

type Bucket = Pick<R2Bucket, "get">;

/** The routing record as one copy shared by every transport isolate. Each
 *  isolate used to read R2 itself whenever its own copy passed one second,
 *  which put an R2 round trip (60-290 ms measured 2026-11-01) in front of
 *  most uncached searches and grew R2 reads with traffic. One reader keeps
 *  this copy younger than ROUTE_LIFETIME_MS while searches arrive, so R2
 *  reads are bounded by time, not traffic. */
export class RouteCache {
  private copy: ValidatedRoute | undefined;
  private reading: Promise<ValidatedRoute> | undefined;
  lastAskedAt = 0;

  constructor(private readonly bucket: Bucket, private readonly now: () => number = Date.now) {}

  /** a route validated within ROUTE_LIFETIME_MS, reading R2 only if the
   *  copy is older than that */
  async current(): Promise<ValidatedRoute> {
    this.lastAskedAt = this.now();
    if (this.copy && this.now() - this.copy.validatedAt < ROUTE_LIFETIME_MS) return this.copy;
    return this.refresh();
  }

  /** one read at a time; a failed read leaves the old copy to expire on its own */
  refresh(): Promise<ValidatedRoute> {
    this.reading ??= (async () => {
      try {
        const object = await this.bucket.get(ROUTE_KEY);
        if (object === null || object.size > 16384) throw new Error("Missing search route");
        const route = parseRoute(await object.json());
        this.copy = { route, validatedAt: this.now() };
        return this.copy;
      } finally {
        this.reading = undefined;
      }
    })();
    return this.reading;
  }

  /** whether searches are recent enough to keep refreshing ahead of them */
  active(): boolean {
    return this.now() - this.lastAskedAt < IDLE_AFTER_MS;
  }
}
