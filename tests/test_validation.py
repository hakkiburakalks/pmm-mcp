import pytest

import server


@pytest.mark.parametrize("sql", [
    "SELECT queryid FROM pmm.metrics WHERE period_start > now() - INTERVAL 1 HOUR",
    "WITH x AS (SELECT 1) SELECT * FROM x ORDER BY 1 DESC;",
    "select service_name from pmm.metrics where fingerprint = 'file; -- url('",
])
def test_valid_select(sql):
    assert server.validate_select(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE pmm.metrics",
    "SELECT 1; SELECT 2",
    "SELECT * FROM url('http://evil', CSV, 'a String')",
    "SELECT * FROM file('/etc/passwd')",
    "SELECT * FROM remote('host', pmm.metrics)",
    "SELECT * FROM system.users",
    "SELECT 1 INTO OUTFILE '/tmp/x'",
    "SELECT 1 SETTINGS readonly=0",
    "SELECT 1 -- comment",
    "SELECT 1 /* c */",
    "SELECT `url`('x')",
    'SELECT "url"(\'x\')',
    "SELECT 'unterminated",
])
def test_rejected_select(sql):
    with pytest.raises(ValueError):
        server.validate_select(sql)


def test_label_validation():
    assert server._check_label("service_name")
    for bad in ("../admin", "a/b", "a b", "", "1abc"):
        with pytest.raises(ValueError):
            server._check_label(bad)


def test_service_type_validation():
    assert server._check_service_type("mysql") == "mysql"
    assert server._check_service_type(None) is None
    with pytest.raises(ValueError):
        server._check_service_type('mysql"} or vector(1) #')


def test_hours_and_limit_bounds():
    for bad in (0, -1, server.MAX_HOURS + 1):
        with pytest.raises(ValueError):
            server._check_hours(bad)
    assert server._check_limit(10**9) == server.MAX_ROWS_CAP
    assert server._check_limit(0) == 1


def test_quote_escapes():
    assert server._q("a'b\\c") == "'a\\'b\\\\c'"


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("PMM_TOKEN", raising=False)
    monkeypatch.setattr(server, "_client", None)
    with pytest.raises(RuntimeError):
        server._get_client()
