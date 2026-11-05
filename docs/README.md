# Documentation

Start with [architecture](architecture.md) for how the system fits together,
[development](development.md) for local setup and checks, and
[operations](operations.md) for serving, recovery and deployment.
The public [API reference](https://typeahead.waow.tech/docs) covers app integration.

## Working on the system

- [Discovery and identity](discovery-and-identity.md): how accounts, handles and hosting servers reach the index.
- [Snapshot refresh](plans/automatic-snapshot-refresh.md): the managed build and promotion procedure.
- [Lifecycle cleanup](plans/lifecycle-cleanup.md): remaining release and recovery work, with verification limits.
- [PDS identity repair](pds-identity-repair.md): repair contracts and cloud ownership.
- [Repository instructions](../AGENTS.md): invariants and required verification.

## Plans and historical evidence

[Plans](plans/) contain proposals and dated rollout records. Read each status
before treating a design as implemented; the refresh procedure above is the
operational entry point.

[Evaluations](evals/) preserve measurements, qualification reports and their
receipts. [Operational records](ops/) preserve dated interventions and resource
inventories. Their IDs, paths and deployment observations describe those runs;
use the operations guide and live checks for current state.

[Retrospectives](retros/) explain incidents and their corrective actions.
[Archived designs and procedures](archive/) are superseded material.
[Drafts](drafts/) and [blog assets](blog-assets/) support unpublished work and
articles, rather than serving as operating instructions.
