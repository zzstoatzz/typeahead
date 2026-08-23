import { createClient, type Client, type InValue, type Row, type TransactionMode } from "@libsql/client/web";
import type { Env } from "./types";

/** a value that can be bound into a statement — libsql's own contract */
export type SqlValue = InValue;

export interface Stmt {
  bind(...args: SqlValue[]): Stmt;
  /** rows in the shape the caller declares for its SELECT — see tursoDb for the invariant */
  all<T>(): Promise<{ results: T[] }>;
  first<T>(): Promise<T | null>;
  run(): Promise<{ meta: { changes: number } }>;
}

export interface TursoDB {
  prepare(sql: string): Stmt;
  batch(stmts: Stmt[], mode?: "write" | "read"): Promise<{ results: Row[]; meta: { changes: number } }[]>;
}

/** batch() hands back raw rows; callers name the shape of the SELECT they wrote. */
export function rowsAs<T>(rows: Row[]): T[] {
  // SAFETY: same boundary contract as Stmt.all() — T is the caller's declaration
  // of the columns its own query projects.
  return rows as T[];
}

interface BoundStmt extends Stmt {
  sql: string;
  args(): SqlValue[];
}

export function tursoDb(client: Client): TursoDB {
  return {
    prepare(sql) {
      let args: SqlValue[] = [];
      const s: BoundStmt = {
        sql,
        args: () => args,
        bind(...a) { args = a; return s; },
        async all<T>() {
          const r = await client.execute({ sql, args });
          // SAFETY: this is the Turso I/O boundary. libsql returns one plain object
          // per row keyed by column name; T is the caller's declaration of the
          // columns its own SELECT projects. Nothing checks the declaration against
          // the query — that is the contract every caller in this repo signs.
          return { results: r.rows as T[] };
        },
        async first<T>() {
          const r = await client.execute({ sql, args });
          const row = r.rows[0];
          if (row === undefined) return null;
          // SAFETY: same boundary as all(): T is the caller's column declaration.
          return row as T;
        },
        async run() {
          const r = await client.execute({ sql, args });
          return { meta: { changes: r.rowsAffected } };
        },
      };
      return s;
    },
    async batch(stmts, mode = "write") {
      const mode_: TransactionMode = mode;
      const results = await client.batch(
        stmts.map((s) => {
          // SAFETY: every Stmt in this codebase is created by prepare() above and
          // is a BoundStmt; the public Stmt type hides sql/args from handlers.
          const b = s as BoundStmt;
          return { sql: b.sql, args: b.args() };
        }),
        mode_,
      );
      return results.map((r) => ({
        results: r.rows,
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
