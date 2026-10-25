"""MCP server for Percona Monitoring and Management (PMM) 2.x and 3.x.

PMM's own API (/v1/...) rejects Viewer-role tokens ("Failed to list api keys"), so
everything goes through Grafana instead: metrics via the datasource proxy to
VictoriaMetrics, Query Analytics (QAN) via /api/ds/query against ClickHouse (pmm.metrics).

Tools are read-only unless PMM_ALLOW_WRITE=1. The few write tools (silences, annotations) are not even
registered otherwise, and each one only previews its change until called with confirm=true.

Configuration (environment variables):
  PMM_TOKEN          required, Grafana service-account token (Viewer is enough for the read tools;
                     write tools need Editor or Admin)
  PMM_ALLOW_WRITE    default "0"; set "1" to register the write tools
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
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

METRICS_UID = os.environ.get("PMM_METRICS_UID", "PA58DA793C7250F1B")
CLICKHOUSE_UID = os.environ.get("PMM_CLICKHOUSE_UID", "PDEE91DDB90597936")
PROM = f"/graph/api/datasources/proxy/uid/{METRICS_UID}/api/v1"

MAX_ROWS_CAP = 1000
MAX_HOURS = 24 * 31
MAX_RANGE_POINTS = 3000
MAX_SERIES = 50
MAX_LIST = 500
SERVICE_TYPES = ("mysql", "mongodb", "postgresql", "proxysql", "haproxy")

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("pmm")
read_tool = mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
write_tool = mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))

_client: httpx.Client | None = None


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


ALLOW_WRITE = _env_flag("PMM_ALLOW_WRITE", "0")


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
    r"|rename|optimize|grant|revoke|kill|system|set|use|show|describe|explain|information_schema)\b"
    # Table functions / dictionary lookups only matter when called, so a plain column such as
    # `cluster` (it exists in pmm.metrics) must stay selectable.
    r"|\b(url|urlcluster|file|s3|s3cluster|gcs|azureblobstorage|hdfs|remote|remotesecure|cluster"
    r"|clusterallreplicas|mysql|postgresql|mongodb|jdbc|odbc|sqlite|redis|input|executable"
    r"|dictget\w*|dictionary|merge|numbers|zeros|generaterandom|values)\s*\(",
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
        raise ValueError(f"Keyword or function not allowed in read-only QAN queries: {m.group(0).strip()}")
    return s


_STEP_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)?$")
_STEP_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def _step_seconds(step: str) -> float:
    m = _STEP_RE.match(step.strip())
    if not m or float(m.group(1)) <= 0:
        raise ValueError("step must look like 30s, 5m, 1h or a number of seconds.")
    return float(m.group(1)) * _STEP_UNITS[m.group(2)]


def _to_epoch(ts: str) -> float:
    """RFC3339 or unix timestamp to epoch seconds."""
    try:
        return float(ts)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise ValueError(f"Invalid timestamp {ts!r}: use RFC3339 or a unix timestamp.") from None


def _check_range(start: str, end: str, step: str) -> None:
    span = _to_epoch(end) - _to_epoch(start)
    if span <= 0:
        raise ValueError("end must be after start.")
    if span / _step_seconds(step) > MAX_RANGE_POINTS:
        raise ValueError(f"Too many points (limit {MAX_RANGE_POINTS} per series): increase step or shorten the range.")


def _cap_series(data: dict, max_series: int = MAX_SERIES) -> dict:
    """Trim a PromQL matrix/vector result so a broad selector cannot flood the context."""
    result = data.get("result")
    if isinstance(result, list) and len(result) > max_series:
        return {**data, "result": result[:max_series], "series_total": len(result), "truncated": True}
    return data


# ---------- HTTP helpers ----------

def _check(r: httpx.Response):
    if r.status_code >= 400:
        raise RuntimeError(f"PMM returned HTTP {r.status_code} for {r.request.url.path}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(
            f"PMM returned a non-JSON response for {r.request.url.path} "
            f"(login page or proxy in the way?): {r.text[:200]}"
        ) from None


def _request(method: str, path: str, **kw):
    """One retry on transient network errors; 4xx/5xx are not retried."""
    client = _get_client()
    for attempt in (0, 1):
        try:
            return _check(client.request(method, path, **kw))
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt:
                raise


def _get(path: str, **params):
    return _request("GET", path, params={k: v for k, v in params.items() if v is not None})


def _prom(path: str, **params):
    return _get(f"{PROM}{path}", **params)["data"]


def _sql(sql: str, max_rows: int = 200) -> dict:
    max_rows = _check_limit(max_rows)
    body = {"queries": [{"refId": "A", "datasource": {"uid": CLICKHOUSE_UID}, "rawSql": sql, "format": 1}]}
    res = _request("POST", "/graph/api/ds/query", json=body)["results"]["A"]
    if res.get("error"):
        raise RuntimeError(res["error"])
    frames = res.get("frames") or []
    if not frames:
        return {"rows": [], "row_count": 0, "truncated": False}
    frame = frames[0]
    cols = [f["name"] for f in frame["schema"]["fields"]]
    vals = frame["data"]["values"]
    rows = [dict(zip(cols, r, strict=False)) for r in zip(*vals, strict=False)] if vals else []
    return {"rows": rows[:max_rows], "row_count": len(rows), "truncated": len(rows) > max_rows}


def _q(s: str) -> str:
    """Quote a value as a ClickHouse string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _window(hours: float, end: str | None) -> str:
    hours = _check_hours(hours)
    to = f"parseDateTimeBestEffort({_q(end)})" if end else "now()"
    return f"period_start >= {to} - INTERVAL {int(hours * 3600)} SECOND AND period_start < {to}"


# ---------- Metrics ----------

@read_tool
def pmm_promql(query: str, time: str | None = None) -> dict:
    """Run an instant PromQL query against the PMM metrics store (VictoriaMetrics).
    Common labels: service_name (e.g. db01-mysql), service_type (mysql|mongodb|postgresql),
    node_name, environment. Examples:
      rate(mysql_global_status_questions{service_name="db01-mysql"}[5m])
      mysql_global_status_threads_running
      100 - avg by (node_name)(rate(node_cpu_seconds_total{mode="idle"}[5m]))*100
    time: RFC3339 or unix timestamp (default: now)."""
    return _cap_series(_prom("/query", query=query, time=time or None))


@read_tool
def pmm_promql_range(query: str, start: str, end: str, step: str = "60s") -> dict:
    """PromQL over a time range. start/end: RFC3339 (e.g. 2026-10-02T08:00:00Z) or unix timestamp.
    For long ranges increase step (e.g. 300s for one day); at most 3000 points per series are allowed
    and at most 50 series are returned (`truncated` and `series_total` say when that happened)."""
    _check_range(start, end, step)
    return _cap_series(_prom("/query_range", query=query, start=start, end=end, step=step))


@read_tool
def pmm_label_values(label: str, match: str | None = None) -> list:
    """List the values of a label (e.g. label=service_name, match='mysql_up')."""
    return _prom(f"/label/{_check_label(label)}/values", **{"match[]": match})[:MAX_LIST]


@read_tool
def pmm_label_names(match: str | None = None) -> list:
    """List all label names, optionally only for series matching a selector (e.g. match='mysql_up')."""
    return _prom("/labels", **{"match[]": match})[:MAX_LIST]


@read_tool
def pmm_metric_names(contains: str | None = None, limit: int = 100) -> dict:
    """Discover metric names. contains: case-insensitive substring (e.g. 'innodb_buffer_pool', 'replication')."""
    names = _prom("/label/__name__/values")
    if contains:
        names = [n for n in names if contains.lower() in n.lower()]
    limit = _check_limit(limit, MAX_LIST)
    return {"metrics": names[:limit], "total": len(names), "truncated": len(names) > limit}


@read_tool
def pmm_metric_metadata(metric: str) -> dict:
    """Help text and type (counter/gauge/histogram) of a metric, to write a correct PromQL query."""
    _check_label(metric.replace(":", "_"))
    return _prom("/metadata", metric=metric)


@read_tool
def pmm_list_services(service_type: str | None = None) -> list:
    """List monitored services: service_name, service_type, node_name, environment and up status.
    service_type: mysql | mongodb | postgresql | proxysql | haproxy (default: all)."""
    _check_service_type(service_type)
    sel = f'service_type="{service_type}"' if service_type else f'service_type=~"{"|".join(SERVICE_TYPES)}"'
    data = _prom("/query", query=f"max by (service_name, service_type, node_name, environment) (up{{{sel}}})")
    out = [{**r["metric"], "up": r["value"][1] == "1"} for r in data["result"]]
    return sorted(out, key=lambda s: (s.get("service_type", ""), s.get("service_name", "")))


# ---------- Alerting (Grafana unified alerting, read-only) ----------

AM = "/graph/api/alertmanager/grafana/api/v2"


@read_tool
def pmm_list_alerts(include_silenced: bool = False, label_filter: str | None = None) -> dict:
    """Currently firing alerts. label_filter: Alertmanager matcher such as 'severity="critical"'.
    Returns name, state, severity, labels, annotations summary, startsAt and the silencedBy ids."""
    params: dict = {"active": "true", "silenced": str(include_silenced).lower(), "inhibited": "false"}
    if label_filter:
        params["filter"] = label_filter
    alerts = _get(f"{AM}/alerts", **params)
    out = [
        {
            "name": a.get("labels", {}).get("alertname"),
            "state": a.get("status", {}).get("state"),
            "silencedBy": a.get("status", {}).get("silencedBy", []),
            "labels": a.get("labels", {}),
            "summary": a.get("annotations", {}).get("summary") or a.get("annotations", {}).get("description"),
            "startsAt": a.get("startsAt"),
        }
        for a in alerts
    ]
    return {"alerts": out[:MAX_LIST], "total": len(out), "truncated": len(out) > MAX_LIST}


@read_tool
def pmm_alert_rules(state: str | None = None) -> dict:
    """Alert rule definitions with their current state (firing | pending | inactive), grouped by folder/group.
    state: optional filter on the rule state."""
    if state is not None and state not in ("firing", "pending", "inactive"):
        raise ValueError("state must be one of: firing, pending, inactive")
    data = _get("/graph/api/prometheus/grafana/api/v1/rules")["data"]
    rules = []
    for g in data.get("groups", []):
        for r in g.get("rules", []):
            if state and r.get("state") != state:
                continue
            rules.append({
                "name": r.get("name"), "group": g.get("name"), "folder": g.get("file"),
                "state": r.get("state"), "health": r.get("health"), "labels": r.get("labels", {}),
                "query": (r.get("query") or "")[:500], "lastEvaluation": r.get("lastEvaluation"),
            })
    return {"rules": rules[:MAX_LIST], "total": len(rules), "truncated": len(rules) > MAX_LIST}


@read_tool
def pmm_list_silences(active_only: bool = True) -> list:
    """Alert silences (who muted what, until when)."""
    silences = _get(f"{AM}/silences")
    if active_only:
        silences = [s for s in silences if s.get("status", {}).get("state") == "active"]
    return [
        {"id": s["id"], "state": s.get("status", {}).get("state"), "matchers": s.get("matchers"),
         "createdBy": s.get("createdBy"), "comment": s.get("comment"),
         "startsAt": s.get("startsAt"), "endsAt": s.get("endsAt")}
        for s in silences
    ][:MAX_LIST]


# ---------- Dashboards, annotations, identity ----------

def _panel_queries(panels: list, folder: str = "") -> list:
    out = []
    for p in panels:
        out += _panel_queries(p.get("panels", []), p.get("title", ""))  # collapsed rows nest panels
        exprs = [t.get("expr") or t.get("rawSql") for t in p.get("targets", []) if t.get("expr") or t.get("rawSql")]
        if exprs:
            out.append({"panel": p.get("title"), "row": folder, "type": p.get("type"),
                        "datasource": (p.get("datasource") or {}).get("type") if isinstance(p.get("datasource"), dict)
                        else p.get("datasource"), "queries": exprs})
    return out


@read_tool
def pmm_search_dashboards(query: str = "", tag: str | None = None, limit: int = 30) -> list:
    """Search PMM/Grafana dashboards by title or tag. PMM ships dashboards for every database engine;
    use the result uid with pmm_dashboard_queries to see the exact PromQL PMM itself uses."""
    res = _get("/graph/api/search", query=query or None, tag=tag, type="dash-db", limit=_check_limit(limit, 100))
    return [{"uid": d["uid"], "title": d["title"], "folder": d.get("folderTitle"), "tags": d.get("tags", []),
             "url": d.get("url")} for d in res]


@read_tool
def pmm_dashboard_queries(uid: str) -> dict:
    """Panels of a dashboard with their PromQL/SQL queries and template variables. Much smaller than the dashboard
    JSON, and the quickest way to learn which metrics answer a question ("how does PMM compute replication lag?")."""
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", uid):
        raise ValueError("Invalid dashboard uid.")
    d = _get(f"/graph/api/dashboards/uid/{uid}")["dashboard"]
    variables = [{"name": v.get("name"), "query": v.get("query") if isinstance(v.get("query"), str) else None}
                 for v in d.get("templating", {}).get("list", [])]
    return {"title": d.get("title"), "variables": variables, "panels": _panel_queries(d.get("panels", []))}


@read_tool
def pmm_annotations(hours: float = 24, end: str | None = None, tags: str | None = None, limit: int = 50) -> list:
    """Annotations in a time window (deploys, restarts, maintenance markers set in PMM/Grafana). Use them to
    correlate a metric or QAN change with something that happened. tags: comma-separated."""
    hours = _check_hours(hours)
    to = _to_epoch(end) if end else datetime.now().timestamp()
    params = {"from": int((to - hours * 3600) * 1000), "to": int(to * 1000), "limit": _check_limit(limit, 200)}
    if tags:
        params["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    res = _get("/graph/api/annotations", **params)
    return [{"time": a.get("time"), "text": a.get("text"), "tags": a.get("tags", []),
             "dashboardUID": a.get("dashboardUID")} for a in res]


@read_tool
def pmm_whoami() -> dict:
    """Identity and role behind the configured token, plus the server URL. First thing to call when requests
    fail with 401/403 or when you are unsure what the token is allowed to see."""
    url = str(_get_client().base_url)
    try:
        u = _get("/graph/api/user")
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
        u = {}
    if u.get("orgRole"):
        return {"login": u.get("login"), "name": u.get("name"), "role": u["orgRole"],
                "isGrafanaAdmin": u.get("isGrafanaAdmin"), "url": url, "write_tools_enabled": ALLOW_WRITE}
    # Service-account tokens have no user record (Grafana 9) or no orgRole (Grafana 12); derive the role
    # from the permissions Grafana grants them.
    perms = _get("/graph/api/access-control/user/permissions")
    role = ("Admin" if "org.users:add" in perms else "Editor" if "dashboards:create" in perms else "Viewer")
    return {
        "org": _get("/graph/api/org").get("name"), "role": role, "url": url, "write_tools_enabled": ALLOW_WRITE,
        "can_write_silences": "alert.instances:create" in perms,
        # The permission map has no scopes and lists annotations:create for Viewers too, but global
        # annotations are rejected for them, so go by role.
        "can_write_annotations": role in ("Admin", "Editor"),
    }


@read_tool
def pmm_deeplink(kind: str, service_name: str | None = None, queryid: str | None = None,
                 dashboard_uid: str | None = None, hours: float = 6) -> str:
    """Build a PMM URL to show the user. kind: qan (Query Analytics, optionally for service_name/queryid) |
    dashboard (needs dashboard_uid). The link opens the last `hours` hours."""
    hours = _check_hours(hours)
    base = str(_get_client().base_url).rstrip("/")
    q = {"from": f"now-{int(hours * 60)}m", "to": "now"}
    if kind == "dashboard":
        if not dashboard_uid or not re.match(r"^[A-Za-z0-9_-]{1,64}$", dashboard_uid):
            raise ValueError("dashboard_uid is required and must be a valid uid.")
        return f"{base}/graph/d/{dashboard_uid}?{urlencode(q)}"
    if kind == "qan":
        if service_name:
            q["var-service_name"] = service_name
        if queryid:
            q["queryid"] = queryid
        return f"{base}/graph/d/pmm-qan/pmm-query-analytics?{urlencode(q)}"
    raise ValueError("kind must be 'qan' or 'dashboard'.")


# ---------- Query Analytics (ClickHouse pmm.metrics) ----------

ORDER = {
    "load": "sum(m_query_time_sum)",
    "count": "num_queries",  # the select alias; sum(num_queries) would resolve to that alias and nest aggregates
    "avg_time": "sum(m_query_time_sum) / greatest(sum(m_query_time_cnt), 1)",
    "rows_examined": "sum(m_rows_examined_sum)",
    "max_time": "max(m_query_time_max)",
    "errors": "sum(num_queries_with_errors)",
}


@read_tool
def pmm_qan_top_queries(
    hours: float = 1,
    end: str | None = None,
    service_name: str | None = None,
    database: str | None = None,
    order_by: str = "load",
    limit: int = 15,
    node_name: str | None = None,
    username: str | None = None,
    client_host: str | None = None,
    contains: str | None = None,
) -> dict:
    """Query Analytics: heaviest queries (grouped by fingerprint).
    hours: look-back window; end: window end (e.g. '2026-10-02 11:05:00', UTC; default now).
    node_name / username / client_host: exact-match filters; contains: case-insensitive text in the fingerprint
    (e.g. a table name).
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
    for col, val in (("node_name", node_name), ("username", username), ("client_host", client_host)):
        if val:
            where.append(f"{col} = {_q(val)}")
    if contains:
        where.append(f"positionCaseInsensitive(fingerprint, {_q(contains)}) > 0")
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
FROM (SELECT * FROM pmm.metrics WHERE {' AND '.join(where)})
GROUP BY queryid, service_name ORDER BY {ORDER[order_by]} DESC LIMIT {_check_limit(limit)}"""
    return _sql(sql)


@read_tool
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
FROM (SELECT * FROM pmm.metrics WHERE {' AND '.join(where)})
GROUP BY service_name ORDER BY total_time_s DESC"""
    return _sql(sql)


@read_tool
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


@read_tool
def pmm_qan_sql(sql: str, max_rows: int = 200) -> dict:
    """Free-form SELECT against the QAN ClickHouse (read-only). Table: pmm.metrics, 1-minute period_start slices.
    Important columns: queryid, service_name, service_type, database, schema, username, client_host, node_name,
    period_start, fingerprint, example, num_queries, num_queries_with_errors, m_query_time_{sum,cnt,max,p99},
    m_lock_time_sum, m_rows_examined_sum, m_rows_sent_sum, m_rows_affected_sum, m_no_index_used_sum,
    m_full_scan_sum, m_tmp_disk_tables_sum, m_docs_scanned_sum (mongo), m_shared_blks_read_sum (pg), tables.
    Always filter on period_start; the table is large. Only a single plain SELECT/WITH is accepted:
    no comments, quoted identifiers, SETTINGS/FORMAT/INTO, table functions or system tables."""
    return _sql(validate_select(sql), max_rows=max_rows)


@read_tool
def pmm_qan_schema() -> dict:
    """Columns and types of pmm.metrics as the connected PMM actually has them, so pmm_qan_sql queries can be
    written without guessing column names (they differ between PMM versions)."""
    return _sql(
        "SELECT name, type FROM system.columns WHERE database = 'pmm' AND table = 'metrics' ORDER BY position",
        max_rows=MAX_ROWS_CAP,
    )


# ---------- Write tools (opt-in: PMM_ALLOW_WRITE=1) ----------
# Never run on the model's own initiative: each tool previews its change and only acts when the user has
# approved it and the call carries confirm=true.

MAX_SILENCE_HOURS = 72
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
WRITE_AUTHOR = "pmm-mcp"


def _preview(action: str, details: dict) -> dict:
    return {
        "dry_run": True,
        "action": action,
        "details": details,
        "note": "Nothing was changed. Show this to the user and repeat the call with confirm=true only after "
                "they explicitly approve.",
    }


def _register_write_tools():
    @write_tool
    def pmm_create_silence(matchers: dict, hours: float, comment: str, confirm: bool = False) -> dict:
        """Silence alerts whose labels match `matchers` (exact match, e.g. {"service_name": "db01-mysql"}) for
        `hours` (max 72). `comment` must say why. Ask the user before calling with confirm=true."""
        if not matchers or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in matchers.items()):
            raise ValueError("matchers must be a non-empty {label: value} object of strings.")
        for k in matchers:
            _check_label(k)
        if not 0 < hours <= MAX_SILENCE_HOURS:
            raise ValueError(f"hours must be greater than 0 and at most {MAX_SILENCE_HOURS}.")
        if not comment.strip():
            raise ValueError("comment is required.")
        now = datetime.now(UTC)
        body = {
            "matchers": [{"name": k, "value": v, "isRegex": False, "isEqual": True} for k, v in matchers.items()],
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(hours=hours)).isoformat(),
            "createdBy": WRITE_AUTHOR,
            "comment": comment.strip()[:500],
        }
        if not confirm:
            return _preview("create silence", body)
        return _request("POST", f"{AM}/silences", json=body)

    @write_tool
    def pmm_delete_silence(silence_id: str, confirm: bool = False) -> dict:
        """Expire a silence (id from pmm_list_silences) so its alerts can fire again."""
        if not _UUID_RE.match(silence_id):
            raise ValueError("Invalid silence id.")
        if not confirm:
            return _preview("expire silence", {"id": silence_id})
        _request("DELETE", f"{AM}/silence/{silence_id}")
        return {"expired": silence_id}

    @write_tool
    def pmm_add_annotation(text: str, tags: list[str] | None = None, confirm: bool = False) -> dict:
        """Add a global annotation (now) such as a deploy or maintenance marker; it appears on PMM graphs."""
        if not text.strip():
            raise ValueError("text is required.")
        body = {"text": text.strip()[:1000], "tags": [*(tags or []), WRITE_AUTHOR][:10]}
        if not confirm:
            return _preview("add annotation", body)
        return _request("POST", "/graph/api/annotations", json=body)

    return pmm_create_silence, pmm_delete_silence, pmm_add_annotation


if ALLOW_WRITE:
    _register_write_tools()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
