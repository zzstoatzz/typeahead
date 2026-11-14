# typeahead at the race

on September 4, [bsky38.com](https://bsky38.com) opened voting for 38 influential Bluesky posters.

its login and its account picker uses [typeahead](https://typeahead.waow.tech) and since you get 10 votes to cast, that became a lot of actor searches!

note: my overwhelming reaction to scale: "oh boy!! this is fun!"

as the [previous post](https://pckt.blog/b/waow-tech/typeahead-more-like-typebehind-amirite-tzgmqge) from [waow.tech](https://waow.tech) explicitly set out that way

i think we held up pretty well! search volume went from 163 to 5,423 requests per 15 minutes, while the engine's median stayed around 3–4 ms.

here are the first 12 hours. choose a window to replay:

<!-- Native Leaflet embed: launch-replay/embed.html -->

Recorded engine searches, alongside public vote counts.[^data]

the prefix index has grown from 5.9M actors in May to 11.63M. search still runs on four shared CPUs and 2GB of RAM.

## what changed since May

search now uses a prepared index plus recent updates, without a full database replica. Turso keeps the actor records; heavypad [builds the index at home](https://nate.leaflet.pub/3mrxzyhlsc22r), orchestrated by [my Prefect server](https://tangled.org/zzstoatzz.io/my-prefect-server), and R2 delivers it to Fly.

## what broke

empty search results still triggered a fallback query to Turso. during the race, those queries saturated the database, stalled ingest writes, and caused the backlog to drop events.

i disabled that fallback, then a second one triggered by engine restarts. search no longer falls back to Turso; uncached requests during a restart can briefly return nothing.

## the hosting footprint

removing the replica let me shrink the search volume from 100GB to 60GB. the index itself is 14GB; builds now run at home.

my May estimate was too optimistic: storage and cache memory still grow with the index.

i also made [the race to 38](https://sites.wisp.place/zzstoatzz.io/race38/) to follow the votes. its account picker uses typeahead too.

[typeahead's stats](https://typeahead.waow.tech/stats) · [API docs](https://typeahead.waow.tech/docs) · [source](https://tangled.org/zzstoatzz.io/typeahead)

[^data]: Saved recording: September 4, 17:00 to September 5, 05:00 UTC, 2026. Sources: Logfire engine spans and [public race38 vote history](https://race38.n8-3e9.workers.dev/api/race). Public trace examples: [race search (3.03 ms)](https://logfire-us.pydantic.dev/public-trace/7dd16220-300e-47e2-839e-4da3f867a2df?spanId=68a394b06b561b6e), [later search (2.25 ms)](https://logfire-us.pydantic.dev/public-trace/d66d04cc-aeeb-4528-89e9-694dff95b4df?spanId=828db3dccaac6556), [slow search (565 ms)](https://logfire-us.pydantic.dev/public-trace/da713ca0-3a96-44dd-8041-60358bf85b23?spanId=4af6e212d42e9ad4). Five-minute buckets; all 144 counts checked against a separate aggregate query. After 20:08 UTC, [one-in-ten sampling](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/src/index/index_search.zig) makes counts estimates (≈); P95 uses recorded spans. Timing excludes the network and cache hits. Zero matches means an empty engine response, not a failed request; fallback may have found results. Portrait sizes emphasize votes with a readable minimum. Reconstructed history may omit withdrawn votes. Shared timestamps do not connect individual searches to votes.
