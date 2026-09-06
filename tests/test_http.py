"""HTTP transport tests: auth, JSON-RPC over POST, and the shape of the responses."""
from fastapi.testclient import TestClient

from gsheets_mcp.app import create_app


def client(env=None, **settings):
    if env:
        env(**settings)
    return TestClient(create_app())


def rpc(c, method, params=None, headers=None, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return c.post(path, json=body, headers=headers or {})


def test_index_describes_the_server():
    data = client().get("/").json()
    assert data["name"] == "gsheets-mcp"
    assert data["endpoint"] == "/mcp"
    assert "gsheets_read_sheet" in data["tools"]


def test_healthz():
    assert client().get("/healthz").json() == {"status": "ok"}


def test_tools_list_over_http():
    body = rpc(client(), "tools/list").json()
    assert body["jsonrpc"] == "2.0"
    assert {t["name"] for t in body["result"]["tools"]} >= {"gsheets_read_sheet"}


def test_endpoint_works_with_and_without_the_trailing_slash():
    c = client()
    assert rpc(c, "ping", path="/mcp").status_code == 200
    assert rpc(c, "ping", path="/mcp/").status_code == 200


def test_notification_gets_202_and_an_empty_body():
    response = client().post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202
    assert response.content == b""


def test_batch_request():
    response = client().post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert [m["id"] for m in response.json()] == [1, 2]


def test_broken_json_is_a_parse_error():
    response = client().post(
        "/mcp", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_get_is_rejected_with_a_hint():
    response = client().get("/mcp")
    assert response.status_code == 405
    assert "POST" in response.json()["error"]["message"]


def test_no_token_configured_means_open_access():
    assert rpc(client(), "ping").status_code == 200


def test_token_required_when_configured(env):
    c = client(env, mcp_auth_token="s3cret")
    assert rpc(c, "ping").status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "s3cret"}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_401_advertises_the_bearer_scheme(env):
    c = client(env, mcp_auth_token="s3cret")
    assert rpc(c, "ping").headers["www-authenticate"] == "Bearer"


def test_read_only_instance_advertises_only_read_tools(env):
    c = client(env, gsheets_read_only="true")
    names = {t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert names == {"gsheets_list_sheets", "gsheets_read_sheet"}
    assert c.get("/").json()["read_only"] is True


def test_a_local_browser_origin_is_allowed():
    for origin in ("http://localhost:3000", "http://127.0.0.1:8077", "http://[::1]:9"):
        assert rpc(client(), "ping", headers={"origin": origin}).status_code == 200


def test_a_foreign_origin_is_rejected_even_without_auth():
    # The DNS-rebinding case: no token configured, so this 403 is the only guard.
    response = rpc(client(), "ping", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert "evil.example" in response.json()["detail"]


def test_an_opaque_origin_is_rejected():
    # Sandboxed iframes and file:// pages send this.
    assert rpc(client(), "ping", headers={"origin": "null"}).status_code == 403


def test_a_configured_origin_is_allowed(env):
    c = client(env, mcp_allowed_origins="https://app.example, https://other.example/")
    assert rpc(c, "ping", headers={"origin": "https://app.example"}).status_code == 200
    assert rpc(c, "ping", headers={"origin": "https://other.example"}).status_code == 200
    assert rpc(c, "ping", headers={"origin": "https://nope.example"}).status_code == 403


def test_origin_is_checked_before_the_token(env):
    # A rebinding attacker has no token either; the clearer error is the origin one.
    c = client(env, mcp_auth_token="s3cret")
    response = rpc(c, "ping", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
