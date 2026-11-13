# How typeahead works

Typeahead answers account search for AT Protocol apps. This page explains the
running system in pictures. It is being rewritten one section at a time; until
every section is here, [architecture](architecture.md) and
[operations](operations.md) hold the rest.

## Inside one slot

Search is answered by a slot: one machine that keeps its own copy of every
account and its own search index. Two slots run; one serves and the other
stands by.

<img src="diagrams/slot.svg" alt="A slot runs a store process and a search engine. The store follows the relay, the PLC directory and the labeler, asks Bluesky for profiles and the Worker for policy, and keeps one record per account. An index bridge sends each changed account to the search engine, which answers the Worker." width="600" />

A slot runs two programs.

**The store process** is where accounts live. It follows the relay for account
events, the PLC directory for identity changes and the labeler for moderation
labels. It asks Bluesky's AppView for profiles and follower counts, and asks
the Worker every 30 seconds for moderation policy and indexing requests. It
keeps one record per account on disk and writes down every change it makes.
Nothing writes into a slot from outside: the store asks or follows.

**The search engine** holds one document per account, carrying what a search
result or a profile shows, and does prefix matching and ranking. Its port is
the only way into a slot.

**The index bridge**, part of the store process, reads the store's log of
changes and sends each changed account to the search engine on the same
machine. The engine stops answering when it has heard nothing from the store
for two minutes, so a slot whose store has stalled does not serve stale
results.

A supervisor starts both programs and restarts either one without stopping
the other.

Details: [index bridge](index-bridge.md), [slot control](slot-control.md),
[discovery and identity](discovery-and-identity.md),
[store slot operations](ops/store-slot-operations.md).
