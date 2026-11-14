# Documentation

Start with [how it works](how-it-works.md) for the running system in pictures,
[architecture](architecture.md) for how the rest fits together,
[development](development.md) for local setup and checks, and
[operations](operations.md) for serving, recovery and deployment.
The public [API reference](https://typeahead.waow.tech/docs) covers app integration.

## Working on the system

- [Discovery and identity](discovery-and-identity.md): how accounts, handles and hosting servers reach the index.
- [Snapshot refresh](history/plans/automatic-snapshot-refresh.md): the managed build and promotion procedure.
- [Lifecycle cleanup](history/plans/lifecycle-cleanup.md): remaining release and recovery work, with verification limits.
- [PDS identity repair](history/pds-identity-repair.md): repair contracts and cloud ownership.
- [Slot control](slot-control.md): how moderation policy and indexing requests reach every store slot, and the rollout.
- [Turso retirement](turso-retirement.md): what the Worker still does in Turso, how profiles and corpus counts are read from the routed slot, and the order in which Turso and its feeders are stopped.
- [Repository instructions](../AGENTS.md): invariants and required verification.

## Plans and historical evidence

[Plans](history/plans/) contain proposals and dated rollout records. Read each status
before treating a design as implemented; the refresh procedure above is the
operational entry point.

[Evaluations](history/evals/) preserve measurements, qualification reports and their
receipts. [Operational records](ops/) preserve dated interventions and resource
inventories. Their IDs, paths and deployment observations describe those runs;
use the operations guide and live checks for current state.

[Retrospectives](history/retros/) explain incidents and their corrective actions.
[Archived designs and procedures](history/archive/) are superseded material.
[Drafts](history/drafts/) and [blog assets](history/blog-assets/) support unpublished work and
articles, rather than serving as operating instructions.
