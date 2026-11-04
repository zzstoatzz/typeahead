<a href="https://typeahead.waow.tech"><img src="docs/typeahead.svg" width="72" height="72" alt="typeahead" /></a>

# [typeahead](https://typeahead.waow.tech)

find people across [AT Protocol](https://atproto.com). search by handle or display
name, or look up a public profile—no sign-in required. use it on the web or add
account search to your app.

[try a search ↗](https://typeahead.waow.tech/) · [API docs](https://typeahead.waow.tech/docs) · [who’s using it?](https://typeahead.waow.tech/stats)

## use

```sh
curl 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&limit=10'
```

get matching accounts and their public profile details. you can also narrow a
search to an account hosting server ([PDS](https://atproto.com/specs/account))
with the `pds` parameter, including accounts with custom-domain handles.

building an app? the [API docs](https://typeahead.waow.tech/docs) cover search,
profile lookups and the Bluesky-compatible search endpoint.
[llms.txt](https://typeahead.waow.tech/llms.txt) has a text version.

## how it works

typeahead follows account changes across the network and keeps a searchable
index, so each search doesn’t have to visit individual hosting servers. fresh
copies of the index are built and checked separately from the one answering
searches. see the [architecture and diagram](docs/architecture.md).
the public Worker's bundle is minified, and image rendering runs in a separate
Worker so its dependencies stay off the search startup path.

## develop

install [Bun](https://bun.sh/) and [just](https://just.systems/), then:

```sh
bun install          # install dependencies
bunx wrangler dev    # start the local API and website
just check           # check types, lint, tests and deployment builds
```

see [development](docs/development.md) for service prerequisites, engine tests
and deployment commands. local services need runtime configuration and credentials.

## docs

[docs/](docs/README.md) covers the design, operations and evaluations.
repository workflows are in [AGENTS.md](AGENTS.md).
