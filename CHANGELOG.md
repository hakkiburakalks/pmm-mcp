# Changelog

## 0.3.2

### Changed
- Documentation only: example env file with fake values, "Read-only or write?" section, SECURITY guidance for
  write mode, tested databases (MySQL, PostgreSQL, MongoDB). Released so the PyPI page shows the current README.

## 0.3.1

### Changed
- Code moved into the `pmm_mcp` package and published to PyPI: `uvx pmm-mcp` or `pip install pmm-mcp`.
  `server.py` stays as a compatibility shim, so `uv run server.py` and existing client configs keep working.
- Tag-triggered PyPI publishing via GitHub Actions (Trusted Publishing).

## 0.3.0

### Added
- Alerting (read): `pmm_list_alerts`, `pmm_alert_rules`, `pmm_list_silences`.
- Dashboards and context: `pmm_search_dashboards`, `pmm_dashboard_queries`, `pmm_annotations`, `pmm_deeplink`, `pmm_whoami`.
- Metric discovery: `pmm_metric_names`, `pmm_metric_metadata`, `pmm_label_names`.
- `pmm_qan_schema`, and `node_name` / `username` / `client_host` / `contains` filters on `pmm_qan_top_queries`.
- Opt-in write tools (`PMM_ALLOW_WRITE=1`): `pmm_create_silence`, `pmm_delete_silence`, `pmm_add_annotation`.
  They are not registered by default, preview their change and only act with `confirm=true`.
- All read tools carry the MCP `readOnlyHint` annotation.
- PMM 3 support, verified against PMM 2 (Grafana 9.2) and PMM 3 (Grafana 12.4).
- CI (ruff and pytest) and tests for the tool functions with mocked HTTP.

### Fixed
- `pmm_qan_top_queries` / `pmm_qan_by_service`: the `database` and `service_type` filters failed in ClickHouse
  because they resolved to aggregate aliases; `order_by="count"` failed for the same reason.
- `pmm_qan_sql` no longer rejects plain columns named like table functions (e.g. `cluster`).
- `pmm_promql_range` validates `start`, `end` and `step`, and caps points and series.
- Non-JSON responses give a clear error; transient connection errors are retried once.

## 0.2.0

Initial public release.
