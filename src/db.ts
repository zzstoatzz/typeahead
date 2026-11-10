import { createClient, type Client, type InValue, type TransactionMode, type Value } from "@libsql/client/web";
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

/** one result row keyed by column name, as either store returns it */
export type SqlRow = Record<string, Value>;

export interface SqlDb {
  prepare(sql: string): Stmt;
  batch(stmts: Stmt[], mode?: "write" | "read"): Promise<{ results: SqlRow[]; meta: { changes: number } }[]>;
}

/** src/moderation.ts imports the interface under this name. Its bytes are
 *  hashed into src/moderation-regexes.json and from there into every Tantivy
 *  snapshot manifest, so it is not edited for a rename. */
export type TursoDB = SqlDb;

/** batch() hands back raw rows; callers name the shape of the SELECT they wrote. */
export function rowsAs<T>(rows: SqlRow[]): T[] {
  // SAFETY: same boundary contract as Stmt.all() — T is the caller's declaration
  // of the columns its own query projects.
  return rows as T[];
}

interface BoundStmt extends Stmt {
  sql: string;
  args(): SqlValue[];
}

export function tursoDb(client: Client): SqlDb {
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

/** The same statement interface over a D1 binding. D1 takes numbered
 *  parameters (`?1`) and runs a batch as one transaction, as libSQL does;
 *  it has no read mode, so `batch`'s second argument is ignored. */
export function d1Db(binding: D1Database): SqlDb {
  const prepared = new WeakMap<Stmt, () => D1PreparedStatement>();
  return {
    prepare(sql) {
      let statement = binding.prepare(sql);
      const s: Stmt = {
        bind(...args) { statement = binding.prepare(sql).bind(...args); return s; },
        async all<T>() {
          const r = await statement.all<T>();
          return { results: r.results };
        },
        first<T>() { return statement.first<T>(); },
        async run() {
          const r = await statement.run();
          return { meta: { changes: r.meta.changes } };
        },
      };
      prepared.set(s, () => statement);
      return s;
    },
    async batch(stmts) {
      const results = await binding.batch<SqlRow>(stmts.map((s) => {
        const statement = prepared.get(s);
        if (statement === undefined) throw new Error("statement was prepared by another database");
        return statement();
      }));
      return results.map((r) => ({ results: r.results, meta: { changes: r.meta.changes } }));
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
export async function getDb(env: Env): Promise<SqlDb> {
  return tursoDb(createClient({
    url: env.TURSO_URL,
    authToken: env.TURSO_AUTH_TOKEN,
  }));
}
