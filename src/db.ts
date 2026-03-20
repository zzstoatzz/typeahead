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

export function createDb(env: Env): TursoDB {
  return tursoDb(createClient({ url: env.TURSO_URL, authToken: env.TURSO_AUTH_TOKEN }));
}
