# Deployment tooling

Production uses the root Worker configuration and
[search transport configuration](search-transport/wrangler.jsonc).
Use the root `justfile` deploy commands so lint and the normal verification gates
run before publication. Search snapshot rollout belongs to the
[managed refresh controller](../docs/plans/automatic-snapshot-refresh.md).

## Qualification and preview tooling

[tantivy-qualification](tantivy-qualification/README.md) contains the matched
engine trial controller and image tooling. Its historical result receipts live
in the [October 29 evidence collection](../docs/evals/tantivy-qualification-20261029/).
Resource ledgers and build receipts that the tools read remain beside their code.

`tantivy-dev` contains the original isolated backend’s build and measurement
tooling. Its machine and volume were retired on October 31; the adjacent resource
ledger records the historical run and is not a current inventory. See the
[retirement record](../docs/ops/test-resource-retirement-20261031/).

[tantivy-dev-ui](tantivy-dev-ui/README.md) is the separate preview Worker. The
October 31 retirement record verified it against managed slot B, independently
of the retired dev backend. Verify its current binding before operating it.

These trial and preview tools are separate from production refresh. Their old
commands, resource IDs and receipts do not authorize recreating retired resources.
