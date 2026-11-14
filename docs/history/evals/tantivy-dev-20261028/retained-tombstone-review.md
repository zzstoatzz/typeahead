# Retained tombstone source policy review

Reviewed `services/tantivy-input/sync.py` and its SQLite/HTTP replay tests independently. No blocking issue remains in the reviewed policy `turso-current-state-cutoff-v1`.

Source delete writers atomically remove the actor and write its tombstone; ingestion can subsequently recreate the actor without removing that historical tombstone. The native snapshot reads current nonempty-handle actor rows. Consequently, a retained tombstone must not erase a current eligible recreation solely because both operations have the same second-resolution timestamp.

The tombstone statement joins the current actor by DID within that statement's SQL snapshot. It suppresses a tombstone only when the actor has a nonempty handle and `deleted_at <= updated_at <= cycle_upper_bound`. Hidden recreations remain hidden; absent, cleared-handle, and older actor rows do not suppress deletion. Backend version checks and equal-version conflict handling remain unchanged.

The upper bound is essential: with backend actor v90, tombstone v100, cutoff105, and current source actor v110, suppressing the tombstone would leave v90 visible while claiming progress through105. The implemented guard applies v100 now and restores v110 in the next eligible cycle. A concrete two-cycle test covers this case.

Raw tombstone keysets advance even when every row in a page is suppressed, and cursor commits remain atomic with submitted mutations. Receipts separately count raw, submitted, and superseded records. The policy identifier is durable. Upgrading a legacy cursor copies it before adding the policy, preserving the exact old value for the first CAS; unknown explicit policies fail closed. Query-plan tests require indexed tombstone selection and actor primary-key lookups.

The added tests exercise equal/newer live recreations, hidden/blank/absent/older rows, all-suppressed bounded pause/resume, the future-cutoff counterexample, legacy cursor CAS upgrade, and a late same-version recreation after a tombstone was already committed. That last case deliberately remains a conflict rather than relaxing backend ordering.

The input owner reports all 15 policy tests passed and actual Rust integration passed the new cases, including legacy CAS upgrade and the future-cutoff regression. These execution results supplement this independent code/test review; no cloud test was run in this lane.

This is bounded current-state replay, not a transactionally consistent snapshot across both streams. A row committed after its actor page has drained can require a subsequent cycle; commits older than the configured 300-second lookback can be missed. The join does not remove those pre-existing limits, and a late equal-version recreation after committed deletion still requires explicit recovery. No cloud operation or upstream write was performed for this review.
