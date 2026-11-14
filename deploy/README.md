# Deployment tooling

Production uses the root Worker configuration and
[search transport configuration](search-transport/wrangler.jsonc).
Use the root `justfile` deploy commands so lint and the normal verification gates
run before publication. Search snapshot rollout belongs to the
[managed refresh controller](../docs/history/plans/automatic-snapshot-refresh.md).

## Qualification and preview tooling

The matched native/Tantivy qualification harness was removed with the native
engine; its historical result receipts live in the
[October 29 evidence collection](../docs/history/evals/tantivy-qualification-20261029/).

`tantivy-dev` contains the original isolated backend’s measurement tooling. Its
machine and volume were retired on October 31, and its native-snapshot bootstrap
image was removed with the native engine; the adjacent resource ledger records
the historical run and is not a current inventory. See the
[retirement record](../docs/history/ops/test-resource-retirement-20261031/).

[tantivy-dev-ui](tantivy-dev-ui/README.md) is the separate preview Worker. The
October 31 retirement record verified it against managed slot B, independently
of the retired dev backend. Verify its current binding before operating it.

These trial and preview tools are separate from production refresh. Their old
commands, resource IDs and receipts do not authorize recreating retired resources.
