-- The control record every store slot pulls: moderation policy and indexing
-- requests, for the typeahead-control D1 database. docs/slot-control.md.

-- `policy_revision` rises by one with every statement that changes
-- `mod_overrides` or `blocked_domains`, in the same transaction. It starts at
-- 1 so that 0 never names a served revision.
CREATE TABLE control_meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID;
INSERT INTO control_meta (name, value) VALUES ('policy_revision', 1);

CREATE TABLE mod_overrides (
  did TEXT PRIMARY KEY,
  action TEXT NOT NULL CHECK (action IN ('show', 'hide')),
  reason TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE blocked_domains (
  domain TEXT PRIMARY KEY,
  reason TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;

-- AUTOINCREMENT, so a sequence is never reused after its row is pruned and a
-- slot's stored position stays meaningful.
CREATE TABLE index_requests (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  did TEXT NOT NULL,
  requested_at INTEGER NOT NULL
);
CREATE INDEX idx_index_requests_did ON index_requests (did, requested_at);
CREATE INDEX idx_index_requests_requested_at ON index_requests (requested_at);
