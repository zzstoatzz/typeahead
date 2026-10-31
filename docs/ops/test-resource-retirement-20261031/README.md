# Typeahead test resource retirement — October 31, 2026

The operator requested an audit and removal of leftover Typeahead test infrastructure.
Provider inventory covered every Fly app named Typeahead and the Prefect Hetzner
builder project. No cloud builder servers remained. Benchmark/trial app-name
searches found no additional apps beyond the empty Typeahead qualification app.

Removed after verifying the live routing object, production readiness and search:

| App | Deleted machine | Deleted volume | Capacity |
| --- | --- | --- | --- |
| typeahead-tantivy-dev | 840e95a2153308 | vol_4oj6mqjpnlgeyqxr | 40 GB |
| typeahead-search-refresh | 801539a6551778 | vol_40o3xy5812d6kkk4 | 20 GB |

Neither app appears in current, previous or native rollback routing. The dev UI
was verified to target B, not the retired original dev machine. App registrations
remain: the native app also owns a retained image registry. Their shared IPv4 and
IPv6 addresses carry no dedicated-IP charge. No serving image or R2 artifact was
removed. Deleted-volume backup retention may outlive the volume.

A remains production; B remains the previous backend pending its separately
authorized scheduled stop. The native `typeahead-startup-20261024` backend remains
the rollback anchor required by the current refresh controller. The ingester is
unchanged. The preview UI will lose its backend when B stops; production remains
at typeahead.waow.tech. No replacement preview deployment was created.

Evergreen's October 31 snapshot attributes $12.64/month to the retired dev
resources and $9.63/month to the older native refresh resources: $22.27/month
removed on its July-price estimate. At current Fly list rates, the corresponding
compute plus provisioned disk is about $23.80 per 30 days. This excludes retained
backup storage and is not a claim about the final invoice. Evergreen updates daily
and estimates compute using trailing uptime, so its display need not fall immediately.

Evidence: inventory-before.json, inventory-after.json, route-before.json,
retired.json, sanitized machine recreation metadata, and verification.json.
Both public aliases and a known PDS-scoped search returned the current A build
after deletion without Bluesky fallback. The full public serving route is unchanged.

README.md was reviewed; its architecture and usage remain accurate. This is a
resource retirement record, not an architectural change. Historical evidence is
preserved rather than rewriting old inventories as current.
