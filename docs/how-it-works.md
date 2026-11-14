# How typeahead works

Typeahead answers account search for AT Protocol apps: someone types a few
letters and gets matching accounts back. This page explains the running system
in five pictures. Procedures are in [operations](operations.md).

## A search request

<img src="diagrams/search.svg" alt="An app sends a search to the public Worker, which answers from its cache when it can. On a miss the search transport reads the route record and sends the query to the serving slot's search engine." width="600" />

The public Worker checks the parameters and the rate limit, then looks in its
cache, which keeps each answer for 60 seconds. On a miss it hands the query to
the search transport, a second Worker whose only job is to know which slot is
serving. The transport reads that from the route record and works from a copy
at most one second old. The slot's search engine matches handles and display
names by prefix, ranks them with an exact handle first, and returns the stored
profiles. Answers are public and the same for every caller.

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

## How an account gets in

<img src="diagrams/accounts.svg" alt="A relay event, a PLC identity change or an index request starts the work. The store records the account, verifies its handle, fetches its profile, applies policy, and the bridge sends it to the search engine." width="600" />

Three things bring an account to a slot's attention: an event from the
account on the relay, an identity change in the PLC directory, or a request
made with the "index this account" button, which the Worker records and every
slot picks up within 30 seconds. From there the store does the same work each
time. It creates or updates the record, verifies the handle against the
account's DID, asks Bluesky's AppView for the profile, and applies moderation
policy: labels, domain blocks and operator overrides decide whether the
account is shown. The index bridge then sends the account to the search
engine. Accounts already in the store are refreshed on a schedule by the same
steps.

## Two slots and the route

<img src="diagrams/route.svg" alt="The search transport follows a route record that names the serving slot. Two slots each follow the same sources on their own. A flip is one write to the route record." width="600" />

Two slots run the same build. Each follows the sources on its own and keeps
its own accounts and index, and they never talk to each other. The route
record names the one that serves; the other is the standby.

Changing which slot serves is one write to the route record. No machine is
restarted and nothing is deployed. The write is allowed only when the standby
reports the same build and moderation policy as the serving slot, data less
than two minutes behind, and an account count within 2%. The record keeps the
previous slot, so going back is the same kind of write.

This is how a slot is upgraded: deploy to the standby, check it, flip, then
deploy to the other.

## What runs where

<img src="diagrams/outside.svg" alt="Cloudflare holds the public Worker, the search transport, the route record, the control database, statistics and the image renderer. Fly holds the two slots. The relay, the PLC directory, the labeler and the Bluesky AppView are run by others." width="600" />

The accounts and the search index live only on the slots. Cloudflare holds
everything around them: the Workers, the route record, the control database
that carries moderation policy and index requests to the slots, and the
request statistics behind `/stats`. The slots read the relay, the PLC
directory, the labeler and Bluesky's AppView, and write to none of them.

Details: [index bridge](index-bridge.md), [slot control](slot-control.md),
[discovery and identity](discovery-and-identity.md),
[store slot operations](ops/store-slot-operations.md).

## Changing the pictures

The five pictures are written by one script,
[`diagrams/build.py`](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/diagrams/build.py).
Every part names an icon slot, and the script holds a default icon for each, so
a part looks the same wherever it appears. Edit the script and run it; do not
edit the SVG files.

Replacing a default icon with a Doodl drawing is not built. The model to follow
is how ZDS does it:
[slots with bundled defaults, and a config that pins a drawing to a slot](https://tangled.org/zat.dev/zds/blob/56918b12276a425b66c3b78f1822b70600f25b1c/docs/iconography-proposal.md).
That link is pinned to a commit so it keeps working if the file moves. A
drawing would have to be inlined into the SVG when the script runs, because
these files are shown as images and an image cannot load another file.
