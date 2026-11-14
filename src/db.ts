/** a value that can be bound into a statement */
export type SqlValue = string | number | boolean | null | ArrayBuffer;

export interface Stmt {
  bind(...args: SqlValue[]): Stmt;
  /** Rows in the shape the caller declares for its SELECT: one plain object
   *  per row keyed by column name. Nothing checks the declaration against the
   *  query; that is the contract every caller in this repo signs. */
  all<T>(): Promise<{ results: T[] }>;
  first<T>(): Promise<T | null>;
  run(): Promise<{ meta: { changes: number } }>;
}

/** one result row keyed by column name */
export type SqlRow = Record<string, string | number | null | ArrayBuffer>;

export interface SqlDb {
  prepare(sql: string): Stmt;
  /** One transaction. `mode` states the caller's intent; D1 has no read mode. */
  batch(stmts: Stmt[], mode?: "write" | "read"): Promise<{ results: SqlRow[]; meta: { changes: number } }[]>;
}

/** batch() hands back raw rows; callers name the shape of the SELECT they wrote. */
export function rowsAs<T>(rows: SqlRow[]): T[] {
  // SAFETY: same boundary contract as Stmt.all() — T is the caller's declaration
  // of the columns its own query projects.
  return rows as T[];
}

/** The statement interface over a D1 binding. D1 takes numbered parameters
 *  (`?1`) and runs a batch as one transaction. */
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
