import json
import os
import subprocess
import sys

import httpx
import pytest

from pmm_mcp import server


@pytest.fixture
def pmm(monkeypatch):
    """Route the module's HTTP client to a handler(request) -> httpx.Response."""
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(server, "_client", httpx.Client(
            base_url="https://pmm.test", transport=httpx.MockTransport(wrapped)))
        return calls

    return install


def ok(data):
    return httpx.Response(200, json=data)


def test_cluster_column_allowed_but_function_rejected():
    assert server.validate_select("SELECT cluster, values FROM pmm.metrics WHERE period_start > now()")
    for bad in ("SELECT * FROM cluster ('a', b)", "SELECT * FROM values('a UInt8', 1)"):
        with pytest.raises(ValueError):
            server.validate_select(bad)


def test_range_validation():
    server._check_range("2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "60s")
    with pytest.raises(ValueError):
        server._check_range("2026-10-01T00:00:00Z", "2026-10-31T00:00:00Z", "1s")
    with pytest.raises(ValueError):
        server._check_range("2026-10-02T00:00:00Z", "2026-10-01T00:00:00Z", "60s")
    with pytest.raises(ValueError):
        server._step_seconds("fast")


def test_cap_series():
    data = {"result": [{"i": i} for i in range(server.MAX_SERIES + 5)]}
    capped = server._cap_series(data)
    assert len(capped["result"]) == server.MAX_SERIES
    assert capped["truncated"] and capped["series_total"] == server.MAX_SERIES + 5


def test_non_json_response_is_a_clear_error(pmm):
    pmm(lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(RuntimeError, match="non-JSON"):
        server.pmm_promql("up")


def test_retry_once_on_connect_error(pmm):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("boom")
        return ok({"data": {"result": []}})

    pmm(handler)
    assert server.pmm_promql("up") == {"result": []}
    assert len(attempts) == 2


def test_list_alerts(pmm):
    calls = pmm(lambda r: ok([{
        "labels": {"alertname": "MySQLDown", "severity": "critical"},
        "status": {"state": "active", "silencedBy": []},
        "annotations": {"summary": "db01 down"}, "startsAt": "2026-10-25T10:00:00Z",
    }]))
    res = server.pmm_list_alerts(label_filter='severity="critical"')
    assert res["alerts"][0]["name"] == "MySQLDown" and res["total"] == 1
    assert calls[0].url.params["filter"] == 'severity="critical"'


def test_alert_rules_state_filter(pmm):
    pmm(lambda r: ok({"data": {"groups": [{"name": "g", "file": "f", "rules": [
        {"name": "a", "state": "firing"}, {"name": "b", "state": "inactive"}]}]}}))
    assert [r["name"] for r in server.pmm_alert_rules(state="firing")["rules"]] == ["a"]
    with pytest.raises(ValueError):
        server.pmm_alert_rules(state="nope")


def test_dashboard_queries_walks_nested_panels(pmm):
    pmm(lambda r: ok({"dashboard": {"title": "MySQL",
                                    "templating": {"list": [{"name": "svc", "query": "label_values(up)"}]},
                                    "panels": [{"title": "Row", "panels": [
                                        {"title": "QPS", "type": "timeseries", "datasource": {"type": "prometheus"},
                                         "targets": [{"expr": "rate(mysql_global_status_questions[5m])"}]}]}]}}))
    res = server.pmm_dashboard_queries("mysql-overview")
    assert res["panels"][0]["queries"] == ["rate(mysql_global_status_questions[5m])"]
    assert res["variables"][0]["name"] == "svc"
    with pytest.raises(ValueError):
        server.pmm_dashboard_queries("../admin")


def test_qan_top_queries_filters_are_quoted(pmm):
    calls = pmm(lambda r: ok({"results": {"A": {"frames": []}}}))
    server.pmm_qan_top_queries(username="o'brien", contains="orders")
    sql = json.loads(calls[0].content)["queries"][0]["rawSql"]
    assert "username = 'o\\'brien'" in sql
    assert "positionCaseInsensitive(fingerprint, 'orders') > 0" in sql


def test_deeplink(pmm):
    pmm(lambda r: ok({}))
    url = server.pmm_deeplink("qan", service_name="db01-mysql", hours=2)
    assert url.startswith("https://pmm.test/graph/d/pmm-qan/") and "db01-mysql" in url and "now-120m" in url
    with pytest.raises(ValueError):
        server.pmm_deeplink("dashboard")


@pytest.mark.parametrize("flag, expected", [("0", False), ("1", True)])
def test_write_tools_registered_only_when_enabled(flag, expected):
    code = (
        "import asyncio; from pmm_mcp import server;"
        "print(sorted(t.name for t in asyncio.run(server.mcp.list_tools()) if t.name == 'pmm_create_silence'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PMM_ALLOW_WRITE": flag}).stdout
    assert ("pmm_create_silence" in out) is expected


def test_write_tools_preview_until_confirmed(pmm):
    calls = pmm(lambda r: ok({"silenceID": "abc12345"}))
    create, delete, annotate = server._register_write_tools()

    preview = create({"service_name": "db01-mysql"}, 2, "planned maintenance")
    assert preview["dry_run"] is True and not calls

    assert create({"service_name": "db01-mysql"}, 2, "planned maintenance", confirm=True) == {"silenceID": "abc12345"}
    assert calls[0].method == "POST" and calls[0].url.path.endswith("/silences")

    assert delete("abc12345")["dry_run"] is True and len(calls) == 1
    assert annotate("deploy v2")["dry_run"] is True and len(calls) == 1


def test_write_tool_validation(pmm):
    pmm(lambda r: ok({}))
    create, delete, _ = server._register_write_tools()
    bad = [({}, 1, "x"), ({"a": "b"}, 0, "x"), ({"a": "b"}, 73, "x"), ({"a": "b"}, 1, " "), ({"a b": "c"}, 1, "x")]
    for args in bad:
        with pytest.raises(ValueError):
            create(*args, confirm=True)
    with pytest.raises(ValueError):
        delete("../x", confirm=True)


def test_whoami_falls_back_for_service_account(pmm):
    def handler(r):
        p = r.url.path
        if p.endswith("/api/user"):
            return httpx.Response(404, json={"message": "user not found"})
        if p.endswith("/permissions"):
            return ok({"alert.instances:create": True, "annotations:create": True})
        return ok({"name": "Main Org."})

    pmm(handler)
    res = server.pmm_whoami()
    assert res["role"] == "Viewer" and res["can_write_silences"] and not res["can_write_annotations"]


def test_qan_filters_applied_inside_subquery(pmm):
    calls = pmm(lambda r: ok({"results": {"A": {"frames": []}}}))
    server.pmm_qan_top_queries(database="app")
    sql = json.loads(calls[0].content)["queries"][0]["rawSql"]
    # Filters must see raw columns, not the aggregate aliases of the outer SELECT (ClickHouse error 184).
    assert "FROM (SELECT * FROM pmm.metrics WHERE" in sql


def test_qan_by_service_filters_inside_subquery(pmm):
    calls = pmm(lambda r: ok({"results": {"A": {"frames": []}}}))
    server.pmm_qan_by_service(service_type="mysql")
    sql = json.loads(calls[0].content)["queries"][0]["rawSql"]
    assert "FROM (SELECT * FROM pmm.metrics WHERE" in sql and "service_type = 'mysql'" in sql


def test_order_by_never_nests_aggregates():
    # "sum(num_queries)" would resolve to the `sum(num_queries) AS num_queries` alias in the outer SELECT.
    assert server.ORDER["count"] == "num_queries"
