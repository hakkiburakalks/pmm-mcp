# PMM MCP

An MCP server for [Percona Monitoring and Management](https://www.percona.com/software/database-tools/percona-monitoring-and-management) (PMM 2.x). It gives Claude and other MCP clients **read-only** access to MySQL/MongoDB/PostgreSQL metrics (PromQL) and Query Analytics (slowest queries, query details, ClickHouse SQL).

## Requirements
- Linux, macOS or Windows, Python 3.11+, [`uv`](https://docs.astral.sh/uv/)
- Network access to your PMM server (VPN if needed)
- A PMM Grafana token: Administration → Service accounts → role Viewer → Add token

## Installation
Linux / macOS:

    git clone https://github.com/hakkiburakalks/pmm-mcp && cd pmm-mcp && ./install.sh

Windows (PowerShell):

    git clone https://github.com/hakkiburakalks/pmm-mcp; cd pmm-mcp; powershell -ExecutionPolicy Bypass -File .\install.ps1

The installer asks for your PMM URL and token, stores them in `~/.config/mcp/pmm.env` (owner-only access) and registers the server with Claude Code under the name `pmm`. If `claude` is not installed it prints the JSON snippet for Claude Desktop instead.

### Manual setup (any MCP client)
The server is a plain stdio MCP server. Run it with:

    uv run --env-file /path/to/pmm.env --directory /path/to/pmm-mcp server.py

and register that command in your client, for example in `claude_desktop_config.json`:

    {"mcpServers": {"pmm": {"command": "uv", "args": ["run", "--quiet", "--env-file", "C:\\Users\\you\\.config\\mcp\\pmm.env", "--directory", "C:\\path\\to\\pmm-mcp", "server.py"]}}}

Use the full path to `uv` as `command` if your client does not inherit your `PATH`.

## Configuration
Environment variables (see `pmm.env.example`):

| Variable | Description |
|---|---|
| `PMM_URL` | PMM server URL (required) |
| `PMM_TOKEN` | Grafana service-account token, Viewer role (required) |
| `PMM_VERIFY` | TLS verification, default `1`. Use `0` only for self-signed certificates |
| `PMM_CA_BUNDLE` | Path to a custom CA bundle (preferred over disabling verification) |
| `PMM_TIMEOUT` | HTTP timeout in seconds, default `60` |
| `PMM_METRICS_UID` / `PMM_CLICKHOUSE_UID` | Grafana datasource UIDs, only if they differ from PMM's defaults |

## Tools
- `pmm_list_services`: monitored databases and their status
- `pmm_qan_by_service`: how much query time each database spends
- `pmm_qan_top_queries`: heaviest queries
- `pmm_qan_query_detail`: a single query's example and its distribution over time
- `pmm_qan_sql`: free-form, validated SELECT against QAN ClickHouse
- `pmm_promql` / `pmm_promql_range` / `pmm_label_values`: metrics

## Security
- Use a **Viewer** token; the server never writes to PMM.
- TLS certificates are verified by default.
- `pmm_qan_sql` accepts only a single plain `SELECT`/`WITH`. Comments, quoted identifiers, `SETTINGS`/`FORMAT`/`INTO`, table functions (`url`, `file`, `remote`, ...) and system tables are rejected. Row counts and time windows are capped.
- Tool inputs (labels, service types, limits, time windows) are validated before being used in a query.
- The token lives in `~/.config/mcp/pmm.env`, created with owner-only permissions. Never commit it.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Development
    uv sync --extra dev
    uv run pytest

## Notes
- PMM's `/v1` API rejects Viewer tokens, so everything goes through the Grafana datasource proxy.
- The same queryid can appear in several services; group by service when aggregating.
- In MySQL QAN the `max_ms`/`p99` fields are 0 (a perfschema limitation).

## License
[MIT](LICENSE): free for anyone to use, modify and distribute, including commercially.
