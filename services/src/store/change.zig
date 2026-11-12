//! Every kind of change the store accepts. A `Change` is applied through a
//! `Batch`; the same values drive the model tests and the crash harness.

const std = @import("std");
const keys = @import("keys.zig");
const record = @import("record.zig");

/// First sight of a DID. A known DID is left as it is, apart from its hourly
/// activity stamp.
pub const Observe = struct {
    did: []const u8,
    /// Queue the new actor for identity resolution. A known actor's queue
    /// entry is never touched, so discovery cannot reset a backoff.
    enqueue_identity: bool = false,
};

/// A profile-bearing event. An empty field keeps the stored value.
pub const Profile = struct {
    did: []const u8,
    handle: []const u8 = "",
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    pds: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    enqueue_identity: bool = false,
};

/// A resolution from the DID's authority, applied only when it is at least
/// as new as the one already applied. An empty field keeps the stored value.
pub const IdentityResolved = struct {
    did: []const u8,
    observed_at: u64,
    handle: []const u8 = "",
    pds: []const u8 = "",
};

pub const Gone = struct {
    did: []const u8,
    status: enum { deactivated, deleted },
};

pub const LabelEvent = struct {
    did: []const u8,
    source: []const u8,
    value: []const u8,
    subject: keys.Subject = .account,
    negated: bool = false,
    created: []const u8,
    cid: []const u8 = "",
    expires: []const u8 = "",
    /// The label stream's sequence. An older frame never replaces a newer one.
    sequence: u64,
};

/// The labeler's current answer for one account. It replaces what an earlier
/// answer stored and leaves alone what the stream delivered.
pub const LabelsReconciled = struct {
    did: []const u8,
    labels: []const record.Label,
};

pub const OverrideSet = struct {
    did: []const u8,
    action: enum { show, hide },
    reason: []const u8 = "",
};

pub const DomainBlocked = struct {
    domain: []const u8,
    reason: []const u8 = "",
};

/// When a profile source reports a handle other than the stored one, how
/// readily the actor goes back through identity resolution.
pub const HandleCheck = struct {
    /// An actor resolved within this many seconds is left alone, so a source
    /// that stays wrong about a handle costs one resolution per period.
    settle_seconds: u64 = 86_400,
    /// Nothing is queued while this many identity entries are due, so
    /// disagreements wait behind the relay's and the PLC export's signals.
    backlog_limit: usize = 100,
};

pub const HandleVerdict = enum {
    /// No handle was reported, it is the stored one, or no check was asked.
    none,
    /// The actor was queued for identity resolution.
    queued,
    /// The actor already has an identity queue entry.
    pending,
    /// The actor's identity was resolved within `settle_seconds`.
    settled,
    /// `backlog_limit` identity entries are due.
    deferred,
};

/// What a profile fetch returned. Empty strings and zero counts keep the
/// stored values, as the SQL `COALESCE(NULLIF(...))` writes did.
pub const Enrichment = struct {
    did: []const u8,
    source: record.ProfileSource,
    /// The handle the source reported. It is never written: a profile source
    /// is not an authority for handles. One that differs from the stored
    /// handle, without case, is grounds to resolve the identity again.
    handle: []const u8 = "",
    /// Null leaves a differing handle unanswered.
    handle_check: ?HandleCheck = null,
    display_name: []const u8 = "",
    avatar: []const u8 = "",
    description: []const u8 = "",
    banner: []const u8 = "",
    created_at: []const u8 = "",
    associated: []const u8 = "",
    followers_count: u64 = 0,
    follows_count: u64 = 0,
    posts_count: u64 = 0,
    quality_score: f32 = 0,
    /// Null when the source carries no labels (a PDS record). Otherwise the
    /// complete list: the moderation service's are reconciled into the label
    /// range, the rest replace the actor's other labels.
    labels: ?[]const record.Label = null,
    /// When to fetch again; null leaves the actor out of the refresh queue.
    next_refresh: ?u64 = null,
    /// The lease this fetch ran under. The queue entry is settled only while
    /// it still carries that lease: a refresh requested since stays queued.
    lease: ?u64 = null,
    /// False when no source was asked: a bundle line that carries nothing a
    /// profile fetch writes. The actor's check time is left alone, so the
    /// first-check pass (`Store.firstCheck`) still finds it.
    checked: bool = true,
};

pub const IdentityEnqueue = struct {
    did: []const u8,
    reason: record.IdentityReason,
};

pub const IdentityLease = struct {
    limit: usize,
    lease_seconds: u64 = 300,
};

pub const IdentityResult = struct {
    did: []const u8,
    generation: u64,
    observed_at: u64,
    pds: []const u8 = "",
    outcome: union(enum) {
        resolved: struct { handle: []const u8 = "" },
        retry: struct { attempts: u64, delay_seconds: u64, reason: []const u8 = "" },
    },
};

pub const RefreshEnqueue = struct {
    did: []const u8,
    due: u64,
};

pub const RefreshComplete = struct {
    did: []const u8,
    next_due: ?u64 = null,
    /// As `Enrichment.lease`.
    lease: ?u64 = null,
};

/// Take due refresh entries: each moves `lease_seconds` ahead, so a holder
/// that never reports back loses nothing. The batch lists what was leased.
pub const RefreshLease = struct {
    limit: usize,
    lease_seconds: u64 = 300,
};

/// A fetch that learned nothing. The entry moves to `due` while it still
/// carries `lease`; the actor's check time is left alone.
pub const RefreshRetry = struct {
    did: []const u8,
    lease: u64,
    due: u64,
};

pub const WalkPool = enum {
    /// The quality range: every actor a search can rank, best first.
    quality,
    /// The actor range, less what the quality walk covers: live actors with
    /// a handle that are hidden or unscored.
    rest,

    pub fn cursor(self: WalkPool) keys.Cursor {
        return switch (self) {
            .quality => .walk_quality,
            .rest => .walk_rest,
        };
    }
};

/// One step of a standing walk: request a refresh, due now, for the next
/// actors of `pool` after the walk's stored position, and store the new
/// position. An actor already queued is passed over. At the end of the
/// range the walk starts again from the top.
pub const RefreshWalk = struct {
    pool: WalkPool,
    /// Actors to queue at most.
    limit: usize,
    /// Keys to examine at most.
    scan: usize,
};

pub const TombstonesPruned = struct {
    /// Remove actors deleted or deactivated before this time.
    before: u64,
    limit: usize,
};

pub const Change = union(enum) {
    observe: Observe,
    profile: Profile,
    identity_resolved: IdentityResolved,
    gone: Gone,
    reactivated: []const u8,
    label: LabelEvent,
    labels_reconciled: LabelsReconciled,
    override_set: OverrideSet,
    override_cleared: []const u8,
    domain_blocked: DomainBlocked,
    domain_unblocked: []const u8,
    enrichment: Enrichment,
    identity_enqueue: IdentityEnqueue,
    identity_lease: IdentityLease,
    identity_complete: IdentityResult,
    refresh_enqueue: RefreshEnqueue,
    refresh_complete: RefreshComplete,
    refresh_lease: RefreshLease,
    refresh_retry: RefreshRetry,
    refresh_walk: RefreshWalk,
    hidden_refreshed: []const u8,
    tombstones_pruned: TombstonesPruned,
};

pub const Outcome = struct {
    /// False when a guard turned the change away: an unknown or gone actor,
    /// an older resolution, an older label frame, a foreign label source.
    applied: bool = false,
    /// Actors whose content this change altered.
    changed: usize = 0,
    /// A new actor record was written.
    created: bool = false,
    /// What an enrichment did about the handle its source reported.
    handle: HandleVerdict = .none,
};

pub const Job = struct {
    did: []const u8,
    generation: u64,
    attempts: u64,
    reason: record.IdentityReason,
};

/// A leased refresh, with what the fetcher needs from the record.
pub const RefreshJob = struct {
    did: []const u8,
    /// The entry's due time while leased; hand it back with the result.
    lease: u64,
    handle: []const u8,
    pds: []const u8,
    override: record.Override,
};

pub const Walked = struct {
    examined: u64 = 0,
    queued: u64 = 0,
    /// Passes completed by reaching the end of the range.
    wrapped: u64 = 0,

    pub fn since(self: Walked, earlier: Walked) Walked {
        return .{ .examined = self.examined - earlier.examined, .queued = self.queued - earlier.queued, .wrapped = self.wrapped - earlier.wrapped };
    }
};

pub const WalkTally = std.EnumArray(WalkPool, Walked);

pub const CursorUpdate = union(enum) {
    /// The relay sequence the batch's events run up to.
    relay: u64,
    /// The label stream sequence; never moves backwards.
    labeler: u64,
    /// The PLC export timestamp; never moves backwards.
    plc: []const u8,
    /// The index bridge's position in the change log.
    bridge: u64,
    /// The revision of the Worker's policy snapshot the batch made the
    /// store's overrides and blocked domains equal to.
    control_policy: ControlPosition,
    /// The sequence of the last indexing request the batch queued.
    control_requests: ControlPosition,
};

/// A position in the Worker's control record. It means something only
/// against the control database it was read from, which `generation` names.
/// Within one generation a position never moves backwards. A position from
/// another generation replaces the stored generation and drops both stored
/// positions first, so nothing taken from the old database outlives it.
pub const ControlPosition = struct {
    generation: u64,
    at: u64,
};

pub const max_attempts = 12;
pub const min_retry_seconds = 30;
pub const max_retry_seconds = 86_400;
pub const activity_interval = 3600;
pub const max_cursor_bytes = 64;

pub fn retryDelay(requested: u64) u64 {
    return @min(max_retry_seconds, @max(min_retry_seconds, requested));
}

pub const Invalid = error{InvalidChange};

fn part(text: []const u8, max: usize, required: bool) Invalid!void {
    if (text.len > max or (required and text.len == 0) or std.mem.indexOfScalar(u8, text, 0) != null) return error.InvalidChange;
}

fn checkDid(text: []const u8) Invalid!void {
    try part(text, keys.max_did, true);
}

/// Reject a change the key layout cannot hold, before anything is staged.
pub fn validate(one: Change) Invalid!void {
    switch (one) {
        .observe => |c| try checkDid(c.did),
        .profile => |c| {
            try checkDid(c.did);
            try part(c.handle, keys.max_handle, false);
        },
        .identity_resolved => |c| {
            try checkDid(c.did);
            try part(c.handle, keys.max_handle, false);
        },
        .gone => |c| try checkDid(c.did),
        .reactivated, .override_cleared, .hidden_refreshed => |text| try checkDid(text),
        .label => |c| {
            try checkDid(c.did);
            try part(c.source, keys.max_label_source, true);
            try part(c.value, keys.max_label_value, true);
        },
        .labels_reconciled => |c| try checkDid(c.did),
        .override_set => |c| try checkDid(c.did),
        .domain_blocked => |c| try part(c.domain, keys.max_domain, true),
        .domain_unblocked => |name| try part(name, keys.max_domain, true),
        .enrichment => |c| {
            try checkDid(c.did);
            try part(c.handle, keys.max_handle, false);
            if (!std.math.isFinite(c.quality_score) or c.quality_score < 0) return error.InvalidChange;
        },
        .identity_enqueue => |c| try checkDid(c.did),
        .identity_complete => |c| {
            try checkDid(c.did);
            switch (c.outcome) {
                .resolved => |resolved| try part(resolved.handle, keys.max_handle, false),
                .retry => {},
            }
        },
        .refresh_enqueue => |c| try checkDid(c.did),
        .refresh_complete => |c| try checkDid(c.did),
        .refresh_retry => |c| try checkDid(c.did),
        .identity_lease, .refresh_lease, .refresh_walk, .tombstones_pruned => {},
    }
}

pub const profile_suffix = "/app.bsky.actor.profile/self";

/// The account a label URI is about, when it is `did` itself or its profile
/// record.
pub fn subjectOf(did: []const u8, uri: []const u8) ?keys.Subject {
    if (std.mem.eql(u8, uri, did)) return .account;
    if (uri.len == 5 + did.len + profile_suffix.len and std.mem.startsWith(u8, uri, "at://") and
        std.mem.eql(u8, uri[5..][0..did.len], did) and std.mem.endsWith(u8, uri, profile_suffix)) return .profile;
    return null;
}

pub fn subjectUri(arena: std.mem.Allocator, did: []const u8, subject: keys.Subject) ![]const u8 {
    return switch (subject) {
        .account => did,
        .profile => std.fmt.allocPrint(arena, "at://{s}" ++ profile_suffix, .{did}),
    };
}

test "label subjects map to URIs and back" {
    var arena: std.heap.ArenaAllocator = .init(std.testing.allocator);
    defer arena.deinit();
    const did = "did:plc:abc";
    for (std.enums.values(keys.Subject)) |subject| {
        try std.testing.expectEqual(@as(?keys.Subject, subject), subjectOf(did, try subjectUri(arena.allocator(), did, subject)));
    }
    try std.testing.expectEqual(@as(?keys.Subject, null), subjectOf(did, "did:plc:abcd"));
    try std.testing.expectEqual(@as(?keys.Subject, null), subjectOf(did, "at://did:plc:abc/app.bsky.feed.post/3k"));
    try std.testing.expectEqual(@as(?keys.Subject, null), subjectOf(did, "at://did:plc:abd/app.bsky.actor.profile/self"));
}

test "a change the key layout cannot hold is refused" {
    const long: [keys.max_handle + 1]u8 = @splat('a');
    try validate(.{ .observe = .{ .did = "did:plc:abc" } });
    try validate(.{ .profile = .{ .did = "did:plc:abc", .handle = long[0..keys.max_handle] } });
    try std.testing.expectError(error.InvalidChange, validate(.{ .observe = .{ .did = "" } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .observe = .{ .did = "did:plc:a\x00b" } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .profile = .{ .did = "did:plc:abc", .handle = &long } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .profile = .{ .did = "did:plc:abc", .handle = "a\x00.test" } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .label = .{ .did = "did:plc:abc", .source = "did:plc:mod", .value = "", .created = "", .sequence = 1 } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .domain_blocked = .{ .domain = "" } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .enrichment = .{ .did = "did:plc:abc", .source = .bsky, .quality_score = std.math.nan(f32) } }));
    try std.testing.expectError(error.InvalidChange, validate(.{ .enrichment = .{ .did = "did:plc:abc", .source = .bsky, .quality_score = -1 } }));
}

test "retry delays stay inside the queue's bounds" {
    try std.testing.expectEqual(@as(u64, 30), retryDelay(0));
    try std.testing.expectEqual(@as(u64, 600), retryDelay(600));
    try std.testing.expectEqual(@as(u64, 86_400), retryDelay(1 << 40));
}
