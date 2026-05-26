//! Fixed-size bloom filter for DID dedup
//! Avoids re-sending already-known bare DIDs to the worker

const std = @import("std");
const Allocator = std.mem.Allocator;

const BLOOM_HASHES: usize = 7;

pub const BloomFilter = struct {
    bits: std.DynamicBitSetUnmanaged,
    num_bits: usize,
    num_hashes: usize,
    count: usize = 0,

    pub fn init(allocator: Allocator, num_bits: usize, num_hashes: usize) !BloomFilter {
        const bits = try std.DynamicBitSetUnmanaged.initEmpty(allocator, num_bits);
        return .{
            .bits = bits,
            .num_bits = num_bits,
            .num_hashes = num_hashes,
        };
    }

    pub fn deinit(self: *BloomFilter, allocator: Allocator) void {
        self.bits.deinit(allocator);
    }

    fn hashIndices(self: *const BloomFilter, key: []const u8) [BLOOM_HASHES]usize {
        const h1 = std.hash.Wyhash.hash(0, key);
        const h2 = std.hash.Wyhash.hash(1, key);
        var indices: [BLOOM_HASHES]usize = undefined;
        for (0..self.num_hashes) |i| {
            indices[i] = @intCast((h1 +% i *% h2) % self.num_bits);
        }
        return indices;
    }

    pub fn insert(self: *BloomFilter, key: []const u8) void {
        const indices = self.hashIndices(key);
        for (indices) |idx| {
            self.bits.set(idx);
        }
        self.count += 1;
    }

    pub fn contains(self: *const BloomFilter, key: []const u8) bool {
        const indices = self.hashIndices(key);
        for (indices) |idx| {
            if (!self.bits.isSet(idx)) return false;
        }
        return true;
    }

    pub fn reset(self: *BloomFilter) void {
        self.bits.unsetAll();
        self.count = 0;
    }
};
