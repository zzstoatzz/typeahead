# plan: hrana v3 cursor source for the indexer (v2 — corrected after spec re-read)

Replace the CLI-shellout path in `index/builder.zig` with a direct
`POST /v3/cursor` streaming NDJSON read into the builder. Same Turso DB
host as our existing hrana v2 client, same JWT bearer.

**Correction from plan v1**: the cursor mechanism is NOT a set of
verbs over `/v3/pipeline`. Per the [Hrana 3 spec](https://github.com/tursodatabase/libsql/blob/main/docs/HRANA_3_SPEC.md):

> Cursor operations are **not** valid within pipeline requests. The HTTP
> variant supports only: CloseStreamReq, ExecuteStreamReq, BatchStreamReq,
> SequenceStreamReq, DescribeStreamReq, StoreSqlStreamReq, CloseSqlStreamReq,
> GetAutocommitStreamReq

The HTTP cursor path is a dedicated endpoint with newline-delimited JSON
response framing:

```
POST v3/cursor
-> JSON: CursorReqBody
<- line of JSON: CursorRespBody
   lines of JSON: CursorEntry
```

The previous plan described `open_cursor` / `fetch_cursor` / `close_cursor`
as pipeline request types — those are WebSocket-only. Plan rewritten
around the single HTTP request + streaming response shape.

## wire shapes (from the spec, verbatim)

```typescript
type CursorReqBody = {
    "baton": string | null,
    "batch": Batch,
}

type CursorRespBody = {
    "baton": string | null,
    "base_url": string | null,
}

type Batch = {
    "steps": Array<BatchStep>,
}

type BatchStep = {
    "condition"?: BatchCond | null,
    "stmt": Stmt,
}

type Stmt = {
    "sql"?: string | null,
    "sql_id"?: int32 | null,
    "args"?: Array<Value>,
    "named_args"?: Array<NamedArg>,
    "want_rows"?: boolean,
}

type Value =
    | { "type": "null" }
    | { "type": "integer", "value": string }
    | { "type": "float", "value": number }
    | { "type": "text", "value": string }
    | { "type": "blob", "base64": string }

type CursorEntry =
    | StepBeginEntry
    | StepEndEntry
    | StepErrorEntry
    | RowEntry
    | ErrorEntry

type StepBeginEntry  = { "type": "step_begin", "step": uint32, "cols": Array<Col> }
type StepEndEntry    = { "type": "step_end", "affected_row_count": uint32, "last_insert_rowid": string | null }
type RowEntry        = { "type": "row", "row": Array<Value> }
type StepErrorEntry  = { "type": "step_error", "step": uint32, "error": Error }
type ErrorEntry      = { "type": "error", "error": Error }
```

## the lifecycle is: one request, one response, one stream

Critical for our use case: there is no per-batch fetch round-trip. One
POST returns the entire result as NDJSON, framed line-by-line. We must
process it as a stream — buffering the whole 3 GB response body would
take us right back to the problem we're solving.

```
client → POST /v3/cursor with { baton: null, batch: { steps: [
    {stmt: {sql: "SELECT COALESCE(MAX(updated_at), 0) FROM actors WHERE handle != ''"}},
    {stmt: {sql: "SELECT did, handle, ... FROM actors WHERE handle != '' ORDER BY did"}}
]}}

server → line 1 (CursorRespBody): {"baton": "...", "base_url": null}
         line 2 (StepBeginEntry): {"type": "step_begin", "step": 0, "cols": [...]}
         line 3 (RowEntry):       {"type": "row", "row": [{"type": "integer", "value": "1779642639"}]}
         line 4 (StepEndEntry):   {"type": "step_end", "affected_row_count": 0, "last_insert_rowid": null}
         line 5 (StepBeginEntry): {"type": "step_begin", "step": 1, "cols": [...]}
         line 6..N (RowEntry):    {"type": "row", "row": [{"type": "text", "value": "did:plc:..."}, ...]}
         ...
         last line (StepEndEntry): step 1 ends, stream done
```

`base_url` from CursorRespBody is currently irrelevant — we log it for
debugging but don't act on it. Same for `baton` (the stream is fully
consumed in this one response).

## implementation shape

### in TursoClient.zig

**No change to existing v2 paths** (`query`, `queryRuntime`, `execRuntime`,
`/v2/pipeline`). Add v3 cursor as a separate code path so existing
callers keep working unchanged.

New public API — **streaming, callback-driven, no `CursorBatch` type**:

```zig
pub const Stmt = struct {
    sql: []const u8,
    args: []const RuntimeValue = &.{},
};

pub const CursorEntry = union(enum) {
    step_begin: struct { step: u32 },         // cols dropped: we know our own schema
    step_end: struct { step: u32, affected_row_count: u32 },
    row: TursoClient.Row,                     // reuses existing Row (json.Value slice)
    step_error: struct { step: u32, message: []const u8, code: ?[]const u8 },
    err: struct { message: []const u8, code: ?[]const u8 },
};

/// Drive a /v3/cursor stream. `onEntry` is invoked for every CursorEntry
/// as it arrives off the wire — never holds more than one entry in
/// memory at a time. Callback errors abort the stream and propagate.
///
/// `ctx` is the user-supplied state pointer threaded through to the
/// callback (e.g. *BuilderState).
pub fn streamCursor(
    self: *TursoClient,
    allocator: Allocator,
    stmts: []const Stmt,
    ctx: anytype,
    comptime onEntry: fn (@TypeOf(ctx), CursorEntry) anyerror!void,
) !void;
```

### NDJSON parser as a custom std.Io.Writer

The streaming happens through a custom writer that we hand to
`http_client.fetch(response_writer)`. The writer:

1. Buffers incoming bytes up to the next `'\n'`.
2. On newline, JSON-parses the buffered line.
3. Tags it as `CursorRespBody` (first line) or `CursorEntry` (subsequent).
4. Invokes the callback for entry lines.
5. Resets the buffer for the next line.

Bounded memory: at most one line buffered. Max line size capped (default
1 MiB — display names can be long, but not megabytes long). Lines
exceeding the cap fail the stream rather than buffer unboundedly.

```zig
const NdjsonStreamWriter = struct {
    line_buf: std.ArrayListUnmanaged(u8) = .empty,
    line_max: usize = 1 * 1024 * 1024,
    state: enum { expect_resp_body, expect_entries } = .expect_resp_body,
    allocator: Allocator,
    ctx_ptr: *anyopaque,
    on_entry: *const fn (*anyopaque, CursorEntry) anyerror!void,
    /// Captured from the first line's CursorRespBody for caller inspection.
    baton: ?[]const u8 = null,
    base_url: ?[]const u8 = null,
    /// Set if a callback returns an error or we hit a parse failure.
    deferred_error: ?anyerror = null,

    pub fn writer(self: *@This()) std.Io.Writer { ... }  // vtable adapter
};
```

**Concern (must verify in impl)**: Zig 0.16's `http.Client.fetch` does
deliver response bytes to the writer incrementally (it has to, since
HTTP body parsing is inherently streaming) — but if it accumulates in
its own internal buffer before flushing to our writer, we lose the
streaming win. If `fetch()` doesn't stream properly, fall back to the
lower-level `Request`/`receiveHead` API which exposes the response
reader directly. Confirm before committing the plan as code.

### in builder.zig

Replace the in-flight CLI-based `streamFromExport` (and everything that
shelled out — `runTursoExport`, `probeCli`, `deleteSourceFiles`) with
`streamFromCursor`. Keep the `SourceMode` dispatcher pattern for now:

```zig
pub const SourceMode = enum {
    auto,                  // = .hrana_v3_cursor for now
    hrana_v3_cursor,       // primary
    hrana_v2_paginated,    // fallback; kept one cycle then removed
};
```

```zig
fn streamFromCursor(gpa, turso, snapshot, cfg, stats) !void {
    var ctx = StreamCtx{
        .gpa = gpa, .snapshot = snapshot, .stats = stats, .cfg = cfg,
        .stmt_actor = ..., .stmt_emit = ..., .row_arena = ...,
        .current_step = -1,
    };

    try snapshot.exec("BEGIN", .{});
    errdefer _ = snapshot.exec("ROLLBACK", .{}) catch {};

    const stmts = [_]Stmt{
        .{ .sql = "SELECT COALESCE(MAX(updated_at), 0) FROM actors WHERE handle != ''" },
        .{ .sql = "SELECT did, handle, display_name, avatar_url, labels, pds, created_at, associated, hidden, quality_score FROM actors WHERE handle != '' ORDER BY did" },
    };

    try turso.streamCursor(gpa, &stmts, &ctx, processCursorEntry);
    try snapshot.exec("COMMIT", .{});
}

fn processCursorEntry(ctx: *StreamCtx, entry: CursorEntry) !void {
    switch (entry) {
        .step_begin => |sb| { ctx.current_step = @intCast(sb.step); },
        .step_end => |se| {
            // step 0 ended → watermark captured; step 1 ended → all actors processed
        },
        .row => |r| {
            if (ctx.current_step == 0) {
                ctx.stats.source_watermark = r.int(0);
            } else if (ctx.current_step == 1) {
                try processActorRow(ctx, r); // pack + derive + emit, with per-row arena reset
            }
        },
        .step_error => |e| {
            log.err("cursor step {d} error: {s}", .{ e.step, e.message });
            return error.TursoCursorStepError;
        },
        .err => |e| {
            log.err("cursor error: {s}", .{ e.message });
            return error.TursoCursorError;
        },
    }
}
```

## responses to codex's specific recommendations

1. **"Delete the uncommitted CLI experiment now."** ✓ Will do as step 1
   before any v3 code lands. The dispatcher pattern stays; the CLI-
   shellout guts get removed.

2. **"Use `/v3/cursor` as the primary implementation."** ✓ Corrected.
   See spec section above.

3. **"Parse the response as a stream, not an allocated body."** ✓
   Plan now describes a custom `std.Io.Writer` that splits on `\n` and
   parses each line incrementally. Flagged the open question about
   whether `http.Client.fetch` actually streams to our writer or buffers
   internally — must verify in impl.

4. **"Add a new streaming API, not `CursorBatch`."** ✓ The API is
   callback-driven (`streamCursor(stmts, ctx, onEntry)`). No
   `CursorBatch` type. The builder processes entries inline as they
   arrive.

5. **"Be careful with transaction/snapshot claims."** ✓ Adjusted. The
   plan no longer promises snapshot semantics; it describes the
   two-step batch as "elegant if it works" and adds a verification
   step. If the watermark step doesn't share a snapshot with the
   actors step, we either accept the small race window (any row written
   between step 0 and step 1 is excluded from this build, picked up by
   the next) OR explicitly send `BEGIN` as step 0 (libsql/Turso likely
   doesn't allow BEGIN over hrana — needs testing).

6. **"Handle `base_url` from day one if easy."** ✓ The NdjsonStreamWriter
   captures `base_url` from `CursorRespBody` (line 1) and logs it.
   We don't act on it for `/v3/cursor` since the stream is one HTTP
   request — there's no follow-up that would honor `base_url`. If/when
   we add subsequent requests against the same stream (we shouldn't
   need to for our use case), revisit.

7. **"Stopping point: 1k actor cursor build, prove flat RSS."** ✓
   Implementation checklist updated below.

## implementation checklist (updated)

1. **Rip out the CLI experiment** from `builder.zig`:
   - `streamFromExport`, `runTursoExport`, `probeCli`, `deleteSourceFiles`
   - `.expand_arg0` references and `std.process.spawn` imports
   - The `SourceMode.export_sqlite` variant
   - Update `chooseSourceMode` to dispatch `.auto` → `.hrana_v3_cursor`
2. **Add v3 cursor request body builder** to `TursoClient.zig`
   - Compose `CursorReqBody` JSON from `[]Stmt`
   - Reuse existing `RuntimeValue` encoding for args
3. **Add `NdjsonStreamWriter`** with bounded line buffer + callback dispatch
4. **Add `streamCursor(stmts, ctx, onEntry)`** method
5. **Unit tests**:
   - Round-trip the request body JSON (fixture in, expected JSON out)
   - Feed canned NDJSON byte chunks (split at arbitrary points) into the
     writer; assert callbacks fire in correct order with correct shapes
   - Oversized line → expected error
   - Truncated stream (missing trailing newline) → expected error
6. **Verify `http.Client.fetch` streams** to the writer or fall back to
   `Request`/`receiveHead`. Tiny smoke test sufficient.
7. **End-to-end with INDEX_SAMPLE_LIMIT=1000** against live Turso
   - Confirm `source_watermark` set from step 0's single row
   - Confirm `actors`, `prefix_top_base`, `index_meta` populated
   - Confirm flat memory (peak RSS bounded, line by line)
8. **Full-scale with INDEX_SAMPLE_LIMIT=0** (5.9M rows)
   - Wall time, peak RSS, error count
   - Compare against `turso db export` baseline (234 s, 30 MB RSS)
9. **Commit** as a logical milestone:
   - Builder + TursoClient changes
   - Tests
   - Update docs/typeahead-index-design.md to mention the cursor source
   - Remove deprecated v2 paginated path in a follow-up
10. **Snapshot semantics test** (optional, can defer): open a cursor
    with the two-step batch, while it's mid-stream have a separate
    process write to `actors`. Verify whether the cursor's row stream
    reflects the post-cursor-open writes. Document the actual behavior.

## things still uncertain — call out for codex

- **`http.Client.fetch` streaming guarantee.** This is the
  load-bearing question. If it doesn't stream, we use `Request` API
  directly. Want codex's read on whether that's a clean fallback or a
  significant refactor of TursoClient.
- **`BEGIN` over hrana statements.** Spec says batches "run a series of
  statements" but doesn't promise transactional grouping unless
  conditions / explicit txn are used. Should we send
  `[BEGIN, watermark SELECT, actors SELECT, COMMIT]` as a four-step
  batch? Or trust the implicit snapshot, accept the small race window?
- **Cap on `max_count`-equivalent.** `/v3/cursor` doesn't have a fetch-
  size knob — the server decides framing. We may get one row per line
  or many rows in flight before any newline arrives (depending on
  server flushing). The NdjsonStreamWriter's flow control is "process
  whatever we get when we see a newline." Memory ceiling holds as long
  as the server doesn't produce a single line bigger than `line_max`.
  Probably fine; flag if test data ever shows otherwise.

## non-goals (unchanged from v1)

- Float/blob `RuntimeValue` variants — separate cleanup
- Embedded replicas — separate exploration
- Resumable cursors / mid-build recovery — v1 acceptable failure is rerun
