//! Bounded work queue + concurrent worker pool, built on Io.Queue + Io.Group.
//!
//! Each submitted Item is processed exactly once by exactly one worker.
//! Retry/backoff/scheduling are the responsibility of the process function —
//! the pool only handles queue + concurrency lifecycle.
//!
//! Designed to be self-contained: zero deps beyond std. If we like the shape,
//! it should be extractable to its own repo with no surgery.
//!
//! Lifecycle:
//!     var pool: WorkerPool(Item, Ctx) = undefined;
//!     try pool.init(allocator, .{
//!         .queue_size = 4096,
//!         .ctx = &my_ctx,
//!         .process = MyCtx.process, // *const fn (*MyCtx, Io, Item) void
//!     });
//!     defer pool.deinit();
//!     try pool.start(io, 4); // spawn 4 concurrent workers
//!     _ = pool.tryEnqueue(io, item); // non-blocking; false if dropped
//!     // ... later, on shutdown ...
//!     pool.shutdown(io); // close queue, await workers

const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;

pub fn WorkerPool(comptime Item: type, comptime Ctx: type) type {
    return struct {
        const Self = @This();

        pub const ProcessFn = *const fn (*Ctx, Io, Item) void;

        pub const Options = struct {
            queue_size: usize,
            ctx: *Ctx,
            process: ProcessFn,
        };

        allocator: Allocator,
        buffer: []Item,
        queue: Io.Queue(Item),
        group: Io.Group = .init,
        ctx: *Ctx,
        process_fn: ProcessFn,
        dropped: std.atomic.Value(u64) = .init(0),

        pub fn init(self: *Self, allocator: Allocator, opts: Options) !void {
            const buffer = try allocator.alloc(Item, opts.queue_size);
            self.* = .{
                .allocator = allocator,
                .buffer = buffer,
                .queue = .init(buffer),
                .ctx = opts.ctx,
                .process_fn = opts.process,
            };
        }

        pub fn deinit(self: *Self) void {
            self.allocator.free(self.buffer);
        }

        /// Spawn `worker_count` concurrent workers that pull from the queue.
        /// Each worker runs until the queue is closed or the group is canceled.
        pub fn start(self: *Self, io: Io, worker_count: usize) !void {
            for (0..worker_count) |_| {
                try self.group.concurrent(io, workerLoop, .{ self, io });
            }
        }

        fn workerLoop(self: *Self, io: Io) void {
            while (true) {
                const item = self.queue.getOne(io) catch return; // Closed or Canceled
                self.process_fn(self.ctx, io, item);
            }
        }

        /// Non-blocking enqueue. Returns true if accepted, false if dropped (queue full).
        /// Safe to call from any context. `dropped` counter is incremented on rejection.
        pub fn tryEnqueue(self: *Self, io: Io, item: Item) bool {
            // putUncancelable with min=0 → guaranteed non-blocking; returns count actually queued
            const n = self.queue.putUncancelable(io, &.{item}, 0) catch {
                _ = self.dropped.fetchAdd(1, .monotonic);
                return false;
            };
            if (n == 0) {
                _ = self.dropped.fetchAdd(1, .monotonic);
                return false;
            }
            return true;
        }

        /// Graceful shutdown: close the queue (workers drain remaining items),
        /// then wait for all workers to exit. Safe to call once.
        pub fn shutdown(self: *Self, io: Io) void {
            self.queue.close(io);
            self.group.await(io) catch {};
        }

        pub fn droppedCount(self: *const Self) u64 {
            return self.dropped.load(.monotonic);
        }

        pub fn capacity(self: *const Self) usize {
            return self.queue.capacity();
        }
    };
}
