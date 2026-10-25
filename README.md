# PMM MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](https://modelcontextprotocol.io/)

A community-maintained [Model Context Protocol](https://modelcontextprotocol.io/) server for [Percona Monitoring and Management](https://www.percona.com/software/database-tools/percona-monitoring-and-management) (PMM 2.x and 3.x). It gives Claude and other MCP clients **read-only** access to MySQL, MongoDB and PostgreSQL metrics (PromQL) and Query Analytics (slowest queries, query details, ClickHouse SQL).

> This is not an official Percona or Anthropic product.

## ✨ Features

- **Read-only by default**: works with a Grafana *Viewer* token. Optional write tools (silences, annotations) are off unless `PMM_ALLOW_WRITE=1` and always dry-run first
- **Metrics**: instant and range PromQL queries plus label discovery
- **Query Analytics (QAN)**: top queries, per-service load, single-query details and distribution over time
- **Safe SQL**: free-form `SELECT` against QAN's ClickHouse, strictly validated and capped
- **TLS verified by default**, with custom CA bundle support
- **Cross-platform**: Linux, macOS and Windows, with one-command installers

## 🚀 Installation

**Requirements**

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Network access to your PMM server (VPN if needed)
- A PMM Grafana token: *Administration → Service accounts → role Viewer → Add token*

**Linux / macOS**

```bash
git clone https://github.com/hakkiburakalks/pmm-mcp && cd pmm-mcp && ./install.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/hakkiburakalks/pmm-mcp; cd pmm-mcp; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer asks for your PMM URL and token, stores them in `~/.config/mcp/pmm.env` (owner-only access) and registers the server with Claude Code under the name `pmm`. If `claude` is not installed, it prints the JSON snippet for Claude Desktop instead.

## 🎯 Quick Start

### Claude Desktop / any MCP client

The server is a plain stdio MCP server. Run it with:

```bash
uv run --env-file /path/to/pmm.env --directory /path/to/pmm-mcp server.py
```

and register that command in your client, for example in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pmm": {
      "command": "uv",
      "args": [
        "run", "--quiet",
        "--env-file", "C:\\Users\\you\\.config\\mcp\\pmm.env",
        "--directory", "C:\\path\\to\\pmm-mcp",
        "server.py"
      ]
    }
  }
}
```

Use the full path to `uv` as `command` if your client does not inherit your `PATH`.

On macOS the config file is `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pmm": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "run", "--quiet",
        "--env-file", "/Users/you/.config/mcp/pmm.env",
        "--directory", "/Users/you/pmm-mcp",
        "server.py"
      ]
    }
  }
}
```

Desktop apps don't inherit your shell `PATH`, so give the full path to `uv` (check with `which uv`; typically `/opt/homebrew/bin/uv` on Apple Silicon, `/usr/local/bin/uv` on Intel, `~/.local/bin/uv` expanded to an absolute path if installed via the curl script). `install.sh` prints this snippet for you.

### Claude Code

```bash
claude mcp add pmm -- uv run --quiet --env-file ~/.config/mcp/pmm.env --directory /path/to/pmm-mcp server.py
```

## 🔧 Configuration

Set these environment variables (see [`pmm.env.example`](pmm.env.example)):

| Variable | Required | Description |
|---|---|---|
| `PMM_URL` | ✅ | PMM server URL |
| `PMM_TOKEN` | ✅ | Grafana service-account token. Viewer is enough for all read tools; the write tools need Editor or Admin |
| `PMM_ALLOW_WRITE` | | Default `0`. Set `1` to register the opt-in write tools (see below) |
| `PMM_VERIFY` | | TLS verification, default `1`. Use `0` only for self-signed certificates |
| `PMM_CA_BUNDLE` | | Path to a custom CA bundle (preferred over disabling verification) |
| `PMM_TIMEOUT` | | HTTP timeout in seconds, default `60` |
| `PMM_METRICS_UID` | | Grafana metrics datasource UID, only if it differs from PMM's default |
| `PMM_CLICKHOUSE_UID` | | Grafana ClickHouse datasource UID, only if it differs from PMM's default |

## 🛠️ Available Tools

### Inventory

| Tool | Description |
|---|---|
| `pmm_list_services` | Monitored databases and their status |
| `pmm_whoami` | Identity and role behind the configured token |

### Alerting

| Tool | Description |
|---|---|
| `pmm_list_alerts` | Currently firing alerts, optionally filtered by label |
| `pmm_alert_rules` | Alert rules and their state |
| `pmm_list_silences` | Active silences |

### Query Analytics

| Tool | Description |
|---|---|
| `pmm_qan_by_service` | How much query time each database spends |
| `pmm_qan_top_queries` | Heaviest queries, filterable by service, database, node, user, client host or text |
| `pmm_qan_query_detail` | A single query's example and its distribution over time |
| `pmm_qan_schema` | Actual columns of `pmm.metrics` on your PMM version |
| `pmm_qan_sql` | Free-form, validated `SELECT` against QAN ClickHouse |

### Metrics

| Tool | Description |
|---|---|
| `pmm_promql` | Instant PromQL query |
| `pmm_promql_range` | Range PromQL query (point and series caps) |
| `pmm_metric_names` | Find metric names by substring |
| `pmm_metric_metadata` | Help text and type of a metric |
| `pmm_label_names` / `pmm_label_values` | Discover labels and their values |

### Dashboards and context

| Tool | Description |
|---|---|
| `pmm_search_dashboards` | Find PMM dashboards |
| `pmm_dashboard_queries` | The PromQL/SQL behind each dashboard panel |
| `pmm_annotations` | Deploy/maintenance markers to correlate with changes |
| `pmm_deeplink` | PMM URL for QAN or a dashboard |

### Write tools (opt-in)

Registered only when `PMM_ALLOW_WRITE=1` **and** the token has Editor/Admin rights. Without the flag the model does not even see them.

| Tool | Description |
|---|---|
| `pmm_create_silence` | Silence alerts matching exact labels, max 72 h, comment required |
| `pmm_delete_silence` | Expire a silence |
| `pmm_add_annotation` | Add a deploy/maintenance marker |

Every write tool is a **dry run by default**: it returns exactly what it would do and changes nothing until it is called again with `confirm=true`. The tools are also annotated as non-read-only, so MCP clients ask for approval. The model is instructed to confirm with you first, so keep your client's per-tool approval on for these.

## 💬 Example Queries

**Overview**
- "Which databases does PMM monitor, and are they all healthy?"
- "Which service spends the most time running queries?"

**Query Analytics**
- "Show the 10 slowest queries on the production MySQL service"
- "Give me the details and time distribution of this query ID"

**Metrics**
- "What is the connection count on `db-prod-1` over the last 6 hours?"
- "Compare InnoDB buffer pool hit ratio across all MySQL nodes"

## 🔒 Security

- Use a **Viewer** token unless you need the write tools; by default the server never writes to PMM. Writes require `PMM_ALLOW_WRITE=1`, an Editor/Admin token and `confirm=true` on each call.
- TLS certificates are verified by default.
- `pmm_qan_sql` accepts only a single plain `SELECT`/`WITH`. Comments, quoted identifiers, `SETTINGS`/`FORMAT`/`INTO`, table functions (`url`, `file`, `remote`, ...) and system tables are rejected. Row counts and time windows are capped.
- Tool inputs (labels, service types, limits, time windows) are validated before being used in a query.
- The token lives in `~/.config/mcp/pmm.env`, created with owner-only permissions. Never commit it.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## 🐛 Troubleshooting

| Symptom | What to check |
|---|---|
| Server not listed in the client | Use the full path to `uv` as `command`; GUI clients often don't inherit `PATH` |
| `401` / `403` errors | Token is invalid, expired, or not a Grafana service-account token |
| TLS / certificate errors | Set `PMM_CA_BUNDLE` to your CA file. As a last resort, `PMM_VERIFY=0` |
| Timeouts | Check VPN/network access, or raise `PMM_TIMEOUT` |
| Datasource not found | Set `PMM_METRICS_UID` / `PMM_CLICKHOUSE_UID` if your PMM uses non-default UIDs |

## 🔍 Debugging

Launch the server in the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run --env-file /path/to/pmm.env --directory /path/to/pmm-mcp server.py
```

## 🧪 Development

```bash
uv sync --extra dev
uv run pytest
```

## 📝 Notes

- Tested against PMM 2 (Grafana 9.2) and PMM 3 (Grafana 12.4) with the default datasource UIDs, using Viewer and Admin tokens, a monitored MySQL and the full set of tools.

- PMM's `/v1` API rejects Viewer tokens, so everything goes through the Grafana datasource proxy.
- The same queryid can appear in several services; group by service when aggregating.
- In MySQL QAN the `max_ms`/`p99` fields are 0 (a perfschema limitation).

## 📄 License

[MIT](LICENSE): free for anyone to use, modify and distribute, including commercially.
