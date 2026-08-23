import { createClient, type Client } from "@libsql/client/web";
import type { Env } from "./types";

export interface Stmt {
  bind(...args: unknown[]): Stmt;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<{ meta: { changes: number } }>;
}

export interface TursoDB {
  prepare(sql: string): Stmt;
  batch(stmts: Stmt[], mode?: "write" | "read"): Promise<{ results: unknown[]; meta: { changes: number } }[]>;
}

export function tursoDb(client: Client): TursoDB {
  return {
    prepare(sql) {
      let args: unknown[] = [];
      const s: Stmt & { _sql: string; _args: () => unknown[] } = {
        _sql: sql,
        _args: () => args,
        bind(...a) { args = a; return s; },
        async all<T>() {
          const r = await client.execute({ sql, args: args as any });
          return { results: r.rows as unknown as T[] };
        },
        async first<T>() {
          const r = await client.execute({ sql, args: args as any });
          return (r.rows[0] as unknown as T) ?? null;
        },
        async run() {
          const r = await client.execute({ sql, args: args as any });
          return { meta: { changes: r.rowsAffected } };
        },
      };
      return s;
    },
    async batch(stmts, mode = "write") {
      const results = await client.batch(
        stmts.map((s) => ({ sql: (s as any)._sql as string, args: (s as any)._args() as any[] })),
        mode as any,
      );
      return results.map((r) => ({
        results: r.rows as unknown[],
        meta: { changes: r.rowsAffected },
      }));
    },
  };
}

/** One client PER CALL — deliberately not a singleton.
 *
 *  @libsql/client wraps every execute/batch in a shared promise-limit(20).
 *  Cached across requests, that limiter became the one piece of state every
 *  request in an isolate shared: a request canceled mid-query (client
 *  disconnect, ~1/min on /admin/ingest) never settles its slot, so capacity
 *  shrank over the isolate's life until calls started queueing — and a
 *  request queued behind ANOTHER request's I/O has no pending I/O of its own,
 *  which the runtime reads as "will never generate a response" and answers
 *  with a 1101. That was 8–35%/hour of /admin/ingest for at least a day
 *  (2026-08-23), isolate-bound, and gone the minute a deploy recycled them.
 *
 *  createClient is a plain object over the platform fetch; there is no
 *  connection to reuse at this layer, so per-request costs nothing. */
export async function getDb(env: Env): Promise<TursoDB> {
  return tursoDb(createClient({
    url: env.TURSO_URL,
    authToken: env.TURSO_AUTH_TOKEN,
  }));
}
