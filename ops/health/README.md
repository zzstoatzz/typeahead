# Typeahead system health

Service dashboard: https://fly-metrics.net/d/typeahead-health/typeahead-c2b7-service-health?orgId=877152

The dashboard lives in Fly managed Grafana and uses Fly's managed datasource: CPU,
memory and disk first, then request rate, HTTP P95 and throttling. Fly login is
required; public viewing is unresolved.

- Fly latency measures app HTTP requests, including health probes. It is not
  engine-only latency or Cloudflare's cache-hit latency.
- CPU throttle is the rate of the centisecond counter divided by 100, shown as
  throttled seconds per second.
- The dashboard says nothing about freshness, ingest backlog or Worker errors.
  Those are covered in [operating: alerts](../../docs/operating.md#alerts).

The probe exporter that used to live here ran on the Hetzner Prefect cluster,
which is retired. Its collector moved to the operations repository
(`deploy/prefect-edge/typeahead_health.py` in my-prefect-server) on 2026-10-27;
change probes there, not here.
