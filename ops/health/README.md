# Typeahead system health

Service dashboard: https://fly-metrics.net/d/typeahead-health/typeahead-c2b7-service-health?orgId=877152

The service dashboard now lives in Fly managed Grafana, with CPU, memory, and disk first, followed by request rate, HTTP P95, and throttling. It uses Fly’s existing managed datasource. Fly login is currently required: the public-dashboard dialog explicitly denies this account permission, including for dashboards we create. Public viewing is still unresolved.

The old Typeahead dashboard has been removed from Prefect Grafana, together with its Executive Overview link. The source `dashboard.json` below preserves the larger dashboard, including freshness and identity probes; those probe panels have **not** yet moved to Fly. The collector and its history remain running on the existing monitoring backend.

## Retained collector and original dashboard source

The following describes the retained backend and original dashboard, not the six-panel Fly dashboard. Do not reimport it into Prefect Grafana.

## What runs

- Existing Grafana and Prometheus on the Prefect k3s cluster; no additional Grafana instance.
- `typeahead-health` deployment and ClusterIP service in `monitoring`, scraped once per minute by a ServiceMonitor. Four parallel read-only probes, eight-second per-request timeouts. No credentials are needed by this exporter.
- Public freshness, search health, attached index status, and ingester health become numeric Prometheus series. Error responses never reuse an earlier reading. Monitoring requests identify themselves as `typeahead-health`.
- Existing heavypad node exporter over Tailscale. SSH access was verified as `stoat@heavypad`.
- Fly Prometheus data source `typeahead-fly`, using a dedicated organization read-only token named `typeahead-health-grafana`, created September 5, 2026 with a 90-day lifetime. Stored only in Grafana's encrypted secureJsonData; neither the token nor Grafana credentials are in this repository. Renew before December 4, 2026. Fly owns issuance/revocation; Grafana owns the consumer. No existing secrets were migrated or rotated.

New probe history begins September 5. Existing Prometheus retention is seven days; Fly's managed metrics retain approximately 15 days. Dashboard refresh is one minute. No notifications or alert rules have been enabled.

## Signal meanings

- Search + freshness combines the serving-path probe, search readiness, and endpoint checks. NO DATA is not healthy.
- Overlay lag warns at 600 seconds and goes red at 1800. Snapshot age warns at three days and goes red at four.
- Search disk headroom warns below 21 GB, red below 14 GB, based on the currently measured 14 GB snapshot. Revise these limits when the artifact grows.
- Fly latency measures app HTTP requests, including health probes; it is not the engine-only latency from Logfire or Cloudflare's cache-hit latency.
- CPU throttle is rate of the centisecond counter divided by 100, displayed as throttled seconds per second.
- Identity queue/drops are specifically identity resolution, not the complete repository firehose.
- Main ingest backlog and repository-event drops, per-deployment Prefect completion, and Worker cache/error metrics remain in linked Logfire/Prefect/stats tools. This dashboard does not assert those are healthy merely because the process answers HTTP.

## Update

Generate the original dashboard JSON with `python3 ops/health/build_dashboard.py`. Its custom probe queries require the retained Prometheus backend. This file is a migration source, not the currently deployed Fly dashboard.

Use the Prefect repository's explicit kubeconfig for production (never the ambient context):

```sh
kubectl --kubeconfig "$HOME/tangled.org/zzstoatzz.io/my-prefect-server/kubeconfig.yaml" -n monitoring create configmap typeahead-health-code --from-file=exporter.py=ops/health/exporter.py --dry-run=client -o yaml | kubectl --kubeconfig "$HOME/tangled.org/zzstoatzz.io/my-prefect-server/kubeconfig.yaml" apply -f -
kubectl --kubeconfig "$HOME/tangled.org/zzstoatzz.io/my-prefect-server/kubeconfig.yaml" apply -f ops/health/kubernetes.yaml
kubectl --kubeconfig "$HOME/tangled.org/zzstoatzz.io/my-prefect-server/kubeconfig.yaml" -n monitoring rollout restart deployment/typeahead-health
```

The runtime currently uses `python:3.13-alpine`; deployment does not modify either Fly service or the Worker. Pinning the image digest is preferable for later reproducible upgrades.

## Verification

`python3 -m unittest discover -s ops/health -p 'test_*.py'`

Tests cover 503 responses retaining failure diagnostics, malformed responses dropping prior values, and omitted fields staying absent. Live checks verified the deployment is ready, all four targets are scraped, every dashboard query parses, and anonymous dashboard access is denied. Browser layout review awaits the user's Grafana login.
