import { expect, test } from "bun:test";
import { trace } from "./worker-tracing";

test("tracing preserves values, promise identity without instrumentation, and failures without retry", async () => {
  const value = Promise.resolve("answer");
  expect(trace(undefined, "test", () => value)).toBe(value);
  let calls = 0;
  const failure = new Error("original");
  expect(() => trace(undefined, "test", () => { calls++; throw failure; })).toThrow(failure);
  expect(calls).toBe(1);
  const events: string[] = [];
  const span: Span = { isTraced: true, setAttribute() {}, end() {} };
  const tracing: Pick<Tracing, "enterSpan"> = {
    enterSpan<T, A extends unknown[]>(name: string, run: (span: Span, ...args: A) => T, ...args: A): T {
      events.push(name);
      return run(span, ...args);
    },
  };
  expect(trace(tracing, "sync", s => { expect(s).toBe(span); return 42; })).toBe(42);
  expect(trace(tracing, "async", () => value)).toBe(value);
  await expect(trace(tracing, "failure", async () => { throw failure; })).rejects.toBe(failure);
  expect(events).toEqual(["sync", "async", "failure"]);
});
