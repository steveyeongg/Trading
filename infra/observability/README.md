# Observability stack

Prometheus + Grafana, prewired to scrape the signal-service `/metrics`
endpoint and render the **ATLAS — Engine Overview** dashboard.

## Run

```bash
# 1. Start the API (host, :8000) — exposes /metrics
ATLAS_TREND_MODEL=ml/registry/trend/v1.joblib \
  uv run --package signal-service uvicorn signal_service.main:app --reload

# 2. Start the observability stack
docker compose -f infra/observability/docker-compose.yml up -d
```

- **Grafana** → http://localhost:3001 (admin / admin) → *Dashboards → ATLAS → Engine Overview*
- **Prometheus** → http://localhost:9090 (try the `/alerts` and `/targets` tabs)

The datasource, dashboard, and scrape config are all provisioned from this
folder — no click-ops. Edits to `grafana/dashboards/atlas.json` reload on
container restart.

## What it shows

| Panel | Metric |
|---|---|
| Signal outcomes (rate) | `atlas_signals_total{result}` |
| Signal reject ratio | vetoed+gated / total |
| Active WS connections | `atlas_ws_connections` |
| Pipeline P50/P95 by stage | `atlas_pipeline_seconds_bucket{stage}` |
| HTTP rate + P95 by route | `atlas_http_request*` |
| Alerts by channel | `atlas_alerts_fired_total{channel,ok}` |
| Orders + monitor exits | `atlas_orders_total`, `atlas_monitor_exits_total` |

## Alerts

`prometheus/alerts.yml` ships five rules: high reject rate, pipeline latency
regression, alert-delivery failures, order rejections, and service-down. View
them under Prometheus → Alerts. Wire Alertmanager (or Grafana contact points)
to route them to Slack/PagerDuty in production.

## Notes

- The scrape target is `host.docker.internal:8000` — the API running on the
  host. When the API itself is containerised, change the target in
  `prometheus/prometheus.yml` to the service name.
- This stack is separate from the app's `infra/docker/docker-compose.yml`
  (Postgres + Redis) so you can run either independently.
