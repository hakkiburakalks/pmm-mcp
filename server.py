"""MCP server for Percona Monitoring and Management (PMM) 2.x.

PMM's own API (/v1/...) rejects Viewer-role tokens ("Failed to list api keys"), so
everything goes through Grafana instead: metrics via the datasource proxy to
VictoriaMetrics, Query Analytics (QAN) via /api/ds/query against ClickHouse (pmm.metrics).

All tools are read-only.

Configuration (environment variables):
  PMM_TOKEN          required, Grafana service-account token with the Viewer role
  PMM_URL            required, e.g. https://pmm.example.com
  PMM_VERIFY         TLS certificate verification, default "1"; set "0" only for
                     self-signed certificates (or point PMM_CA_BUNDLE at your CA)
  PMM_CA_BUNDLE      optional path to a custom CA bundle (implies verification)
  PMM_TIMEOUT        HTTP timeout in seconds, default 60
  PMM_METRICS_UID    Grafana datasource UID of the metrics store (PMM default is preset)
  PMM_CLICKHOUSE_UID Grafana datasource UID of ClickHouse (PMM default is preset)
"""
import logging
import os
import re
import sys

import httpx
from mcp.server.mcpserver import MCPServer

METRICS_UID = os.environ.get("PMM_METRICS_UID", "PA58DA793C7250F1B")
CLICKHOUSE_UID = os.environ.get("PMM_CLICKHOUSE_UID", "PDEE91DDB90597936")
PROM = f"/graph/api/datasources/proxy/uid/{METRICS_UID}/api/v1"

MAX_ROWS_CAP = 1000
MAX_HOURS = 24 * 31
SERVICE_TYPES = ("mysql", "mongodb", "postgresql", "proxysql", "haproxy")

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("pmm")

_client: httpx.Client | None = None


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _get_client() -> httpx.Client:
    """Build the HTTP client lazily so importing this module needs no credentials."""
    global _client
    if _client is None:
        token = os.environ.get("PMM_TOKEN", "").strip()
        url = os.environ.get("PMM_URL", "").strip().rstrip("/")
        if not token or not url:
            raise RuntimeError("PMM_URL and PMM_TOKEN must be set (see pmm.env.example).")
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("PMM_URL must start with https:// (or http:// for local testing).")
        verify: bool | str = os.environ.get("PMM_CA_BUNDLE") or _env_flag("PMM_VERIFY", "1")
        if verify is False:
            print("WARNING: TLS certificate verification is disabled (PMM_VERIFY=0).", file=sys.stderr)
        _client = httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            timeout=float(os.environ.get("PMM_TIMEOUT", "60")),
            follow_redirects=False,
        )
    return _client


# ---------- Validation helpers ----------

_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(into|outfile|settings|format|attach|detach|create|drop|alter|insert|update|delete|truncate"
    r"|rename|optimize|grant|revoke|kill|system|set|use|show|describe|explain"
    r"|url|urlcluster|file|s3|s3cluster|gcs|azureblobstorage|hdfs|remote|remotesecure|cluster"
    r"|clusterallreplicas|mysql|postgresql|mongodb|jdbc|odbc|sqlite|redis|input|executable"
    r"|dictget|dictionary|merge|numbers|zeros|generaterandom|values|information_schema)\b",
    re.I,
)


def _check_label(label: str) -> str:
    if not _LABEL_RE.match(label):
        raise ValueError("Invalid label name: use letters, digits and underscores only.")
    return label


def _check_service_type(service_type: str | None) -> str | None:
    if service_type is not None and service_type not in SERVICE_TYPES:
        raise ValueError(f"service_type must be one of: {', '.join(SERVICE_TYPES)}")
    return service_type


def _check_hours(hours: float) -> float:
    if not 0 < hours <= MAX_HOURS:
        raise ValueError(f"hours must be greater than 0 and at most {MAX_HOURS}.")
    return float(hours)


def _check_limit(limit: int, cap: int = MAX_ROWS_CAP) -> int:
    return max(1, min(int(limit), cap))


def validate_select(sql: str) -> str:
    """Allow a single plain SELECT/WITH statement and nothing that reads outside pmm.*."""
    s = sql.strip().rstrip(";").strip()
    if not re.match(r"^(SELECT|WITH)\b", s, re.I):
        raise ValueError("Only a single SELECT/WITH query is allowed.")
    if "`" in s or '"' in s:
        raise ValueError("Quoted identifiers are not allowed.")
    bare = _STRING_LITERAL_RE.sub("0", s)
    if ";" in bare:
        raise ValueError("Multiple statements are not allowed.")
    if "--" in bare or "/*" in bare or "#" in bare:
        raise ValueError("SQL comments are not allowed.")
    if "'" in bare:
        raise ValueError("Unterminated string literal.")
    m = _FORBIDDEN_SQL_RE.search(bare)
    if m:
        raise ValueError(f"Keyword or function not allowed in read-only QAN queries: {m.group(0)}")
    return s


# ---------- HTTP helpers ----------

def _check(r: httpx.Response):
    if r.status_code >= 400:
        raise RuntimeError(f"PMM returned HTTP {r.status_code} for {r.request.url.path}: {r.text[:300]}")
    return r.json()


def _prom(path: str, **params):
    return _check(_get_client().get(f"{PROM}{path}", params=params))["data"]


def _sql(sql: str, max_rows: int = 200) -> dict:
    max_rows = _check_limit(max_rows)
    body = {"queries": [{"refId": "A", "datasource": {"uid": CLICKHOUSE_UID}, "rawSql": sql, "format": 1}]}
    res = _check(_get_client().post("/graph/api/ds/query", json=body))["results"]["A"]
    if res.get("error"):
        raise RuntimeError(res["error"])
    frames = res.get("frames") or []
    if not frames:
        return {"rows": [], "row_count": 0, "truncated": False}
    frame = frames[0]
    cols = [f["name"] for f in frame["schema"]["fields"]]
    vals = frame["data"]["values"]
    rows = [dict(zip(cols, r)) for r in zip(*vals)] if vals else []
    return {"rows": rows[:max_rows], "row_count": len(rows), "truncated": len(rows) > max_rows}


def _q(s: str) -> str:
    """Quote a value as a ClickHouse string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _window(hours: float, end: str | None) -> str:
    hours = _check_hours(hours)
    to = f"parseDateTimeBestEffort({_q(end)})" if end else "now()"
    return f"period_start >= {to} - INTERVAL {int(hours * 3600)} SECOND AND period_start < {to}"


# ---------- Metrics ----------

@mcp.tool()
def pmm_promql(query: str, time: str | None = None) -> dict:
    """Run an instant PromQL query against the PMM metrics store (VictoriaMetrics).
    Common labels: service_name (e.g. db01-mysql), service_type (mysql|mongodb|postgresql),
    node_name, environment. Examples:
      rate(mysql_global_status_questions{service_name="db01-mysql"}[5m])
      mysql_global_status_threads_running
      100 - avg by (node_name)(rate(node_cpu_seconds_total{mode="idle"}[5m]))*100
    time: RFC3339 or unix timestamp (default: now)."""
    return _prom("/query", query=query, **({"time": time} if time else {}))


@mcp.tool()
def pmm_promql_range(query: str, start: str, end: str, step: str = "60s") -> dict:
    """PromQL over a time range. start/end: RFC3339 (e.g. 2026-10-02T08:00:00Z) or unix timestamp.
    For long ranges increase step (e.g. 300s for one day), otherwise the output gets huge."""
    return _prom("/query_range", query=query, start=start, end=end, step=step)


@mcp.tool()
def pmm_label_values(label: str, match: str | None = None) -> list:
    """List the values of a label (e.g. label=service_name, match='mysql_up')."""
    return _prom(f"/label/{_check_label(label)}/values", **({"match[]": match} if match else {}))


@mcp.tool()
def pmm_list_services(service_type: str | None = None) -> list:
    """List monitored services: service_name, service_type, node_name, environment and up status.
    service_type: mysql | mongodb | postgresql | proxysql | haproxy (default: all)."""
    _check_service_type(service_type)
    sel = f'service_type="{service_type}"' if service_type else f'service_type=~"{"|".join(SERVICE_TYPES)}"'
    data = _prom("/query", query=f"max by (service_name, service_type, node_name, environment) (up{{{sel}}})")
    out = [{**r["metric"], "up": r["value"][1] == "1"} for r in data["result"]]
    return sorted(out, key=lambda s: (s.get("service_type", ""), s.get("service_name", "")))


# ---------- Query Analytics (ClickHouse pmm.metrics) ----------

ORDER = {
    "load": "sum(m_query_time_sum)",
    "count": "sum(num_queries)",
    "avg_time": "sum(m_query_time_sum) / greatest(sum(m_query_time_cnt), 1)",
    "rows_examined": "sum(m_rows_examined_sum)",
    "max_time": "max(m_query_time_max)",
    "errors": "sum(num_queries_with_errors)",
}


@mcp.tool()
def pmm_qan_top_queries(
    hours: float = 1,
    end: str | None = None,
    service_name: str | None = None,
    database: str | None = None,
    order_by: str = "load",
    limit: int = 15,
) -> dict:
    """Query Analytics: heaviest queries (grouped by fingerprint).
    hours: look-back window; end: window end (e.g. '2026-10-02 11:05:00', UTC; default now).
    order_by: load (total time) | count | avg_time | rows_examined | max_time | errors.
    Returned fields: queryid, service_name, database, fingerprint, num_queries, total_time_s, avg_ms, max_ms,
    p99_ms, rows_examined, rows_sent, load (average concurrent queries = total_time / window)."""
    if order_by not in ORDER:
        raise ValueError(f"order_by must be one of: {', '.join(ORDER)}")
    hours = _check_hours(hours)
    where = [_window(hours, end)]
    if service_name:
        where.append(f"service_name = {_q(service_name)}")
    if database:
        where.append(f"(database = {_q(database)} OR schema = {_q(database)})")
    sql = f"""
SELECT queryid, service_name, any(if(schema != '', schema, database)) AS database,
       substring(any(fingerprint), 1, 400) AS fingerprint,
       sum(num_queries) AS num_queries,
       round(sum(m_query_time_sum), 2) AS total_time_s,
       round(1000 * sum(m_query_time_sum) / greatest(sum(m_query_time_cnt), 1), 2) AS avg_ms,
       round(1000 * max(m_query_time_max), 1) AS max_ms,
       round(1000 * max(m_query_time_p99), 1) AS p99_ms,
       sum(m_rows_examined_sum) AS rows_examined, sum(m_rows_sent_sum) AS rows_sent,
       sum(num_queries_with_errors) AS errors,
       round(sum(m_query_time_sum) / {hours * 3600}, 3) AS load
FROM pmm.metrics WHERE {' AND '.join(where)}
GROUP BY queryid, service_name ORDER BY {ORDER[order_by]} DESC LIMIT {_check_limit(limit)}"""
    return _sql(sql)


@mcp.tool()
def pmm_qan_by_service(hours: float = 1, end: str | None = None, service_type: str | None = None) -> dict:
    """QAN: total load per service (which database spends the most time on queries).
    service_type: mysql | mongodb | postgresql."""
    hours = _check_hours(hours)
    where = [_window(hours, end)]
    if _check_service_type(service_type):
        where.append(f"service_type = {_q(service_type)}")
    sql = f"""
SELECT service_name, any(service_type) AS service_type, any(node_name) AS node_name,
       sum(num_queries) AS num_queries, round(sum(m_query_time_sum), 1) AS total_time_s,
       round(sum(m_query_time_sum) / {hours * 3600}, 3) AS load, uniqExact(queryid) AS distinct_queries
FROM pmm.metrics WHERE {' AND '.join(where)}
GROUP BY service_name ORDER BY total_time_s DESC"""
    return _sql(sql)


@mcp.tool()
def pmm_qan_query_detail(
    queryid: str, service_name: str | None = None, hours: float = 6, end: str | None = None, bucket_minutes: int = 10
) -> dict:
    """For a single queryid: a real query example, tables, and the distribution over time
    (buckets of bucket_minutes). Use it to see when a query started misbehaving.
    queryid and service_name come from pmm_qan_top_queries; the same queryid can appear in
    several services, and if service_name is omitted they are aggregated together."""
    w = _window(hours, end)
    if service_name:
        w += f" AND service_name = {_q(service_name)}"
    bucket_minutes = max(1, min(int(bucket_minutes), 24 * 60))
    meta = _sql(f"""
SELECT groupUniqArray(10)(service_name) AS services, any(fingerprint) AS fingerprint,
       argMax(example, m_query_time_max) AS slowest_example, any(tables) AS tables,
       groupUniqArray(10)(username) AS users, groupUniqArray(10)(client_host) AS client_hosts
FROM pmm.metrics WHERE queryid = {_q(queryid)} AND {w}""")
    series = _sql(f"""
SELECT toStartOfInterval(period_start, INTERVAL {bucket_minutes} MINUTE) AS t,
       sum(num_queries) AS num_queries, round(sum(m_query_time_sum), 2) AS total_time_s,
       round(1000 * max(m_query_time_max), 1) AS max_ms, sum(m_rows_examined_sum) AS rows_examined
FROM pmm.metrics WHERE queryid = {_q(queryid)} AND {w}
GROUP BY t ORDER BY t""", max_rows=500)
    return {"meta": meta["rows"][0] if meta["rows"] else None, "timeline": series["rows"]}


@mcp.tool()
def pmm_qan_sql(sql: str, max_rows: int = 200) -> dict:
    """Free-form SELECT against the QAN ClickHouse (read-only). Table: pmm.metrics, 1-minute period_start slices.
    Important columns: queryid, service_name, service_type, database, schema, username, client_host, node_name,
    period_start, fingerprint, example, num_queries, num_queries_with_errors, m_query_time_{sum,cnt,max,p99},
    m_lock_time_sum, m_rows_examined_sum, m_rows_sent_sum, m_rows_affected_sum, m_no_index_used_sum,
    m_full_scan_sum, m_tmp_disk_tables_sum, m_docs_scanned_sum (mongo), m_shared_blks_read_sum (pg), tables.
    Always filter on period_start; the table is large. Only a single plain SELECT/WITH is accepted:
    no comments, quoted identifiers, SETTINGS/FORMAT/INTO, table functions or system tables."""
    return _sql(validate_select(sql), max_rows=max_rows)


if __name__ == "__main__":
    mcp.run()
