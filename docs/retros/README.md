# retros

Incident and project retrospectives for typeahead. **The practice: after any
incident or messy debugging session, write one — proactively, before moving on.**
The cost is an hour; the alternative is relearning the same lesson the hard way
(this directory exists because we have).

## when to write one

- A user-visible incident, or a near-miss caught internally.
- A debugging session that floundered — wrong turns, symptom-chasing, churn.
- A significant rearchitecture worth recording the reasoning for.

If you're unsure whether it clears the bar, it probably does. Bias toward writing.

## format

`YYYY-MM-DD-<slug>.md`, dated by when the incident started. Sections:

- **What happened** — a few sentences. Lead with the honest bottom line.
- **Timeline** — a table. Times in UTC.
- **Root causes** — the actual mechanisms, not the symptoms.
- **What went wrong in the response** — the floundering, named plainly. This is
  the most valuable section and the easiest to skip. Don't skip it.
- **Action items** — checkboxes; mark what shipped, leave the rest open. Include
  the process lessons, not just the code fixes.

## entries

- [2026-08-28 — search volume full again, empty results for ~3 days](2026-08-28-search-volume-full-again.md)
- [2026-07-28 — search replica sync wedge, 48.7h silent](2026-07-28-search-replica-sync-wedge.md)
- [2026-06-12 — ingester wedge, recovery, throughput root cause](2026-06-12-ingester-wedge-and-recovery.md)
- [2026-05-25 — typeahead rearchitecture notes](2026-05-25-typeahead-rearchitecture-notes.md)
- [2026-05-23 — standard.site backfill & production degradation](2026-05-23-standard-site-backfill.md)
