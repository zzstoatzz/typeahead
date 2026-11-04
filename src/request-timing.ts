export class RequestTiming {
  private readonly startedAt = Date.now();
  rateLimitMs: number | null = null;

  elapsedMs() {
    return Date.now() - this.startedAt;
  }

  async limit(limiter: RateLimit, key: string) {
    const startedAt = Date.now();
    try {
      return await limiter.limit({ key });
    } finally {
      this.rateLimitMs = (this.rateLimitMs ?? 0) + Date.now() - startedAt;
    }
  }

  response(response: Response) {
    const timed = new Response(response.body, response);
    timed.headers.append("Server-Timing", `worker;dur=${this.elapsedMs()}`);
    if (this.rateLimitMs !== null) {
      timed.headers.append("Server-Timing", `rate_limit;dur=${this.rateLimitMs}`);
    }
    return timed;
  }
}
