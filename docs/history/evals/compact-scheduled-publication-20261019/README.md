# October 19 scheduled compact publication

The real 09:00 UTC scheduled publisher completed at 11:25 UTC. The three
manifest files here are independent R2 reads: `latest.json` equals `compact.json`;
`legacy.json` is its same-watermark twin.

`rollback-result.json` records the passing full-size, cached-artifact recovery
qualification on HeavyPad with 8,000 response comparisons per stage and 60
mutation checks. `full_rollback.py` is the executed harness; its absolute paths
refer to retained HeavyPad fixtures and binaries. This test does not establish
empty-directory R2 restoration or production overlay catch-up. Those gates
remain separate in the [cutover runbook](../../plans/compact-production-cutover.md).
