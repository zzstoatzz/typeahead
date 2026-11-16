// The pinned local workerd predates custom spans; production supports ctx.tracing.
export function trace<T>(tracing: Pick<Tracing, "enterSpan"> | undefined, name: string, run: (span?: Span) => T): T {
  return tracing ? tracing.enterSpan(name, run) : run();
}
