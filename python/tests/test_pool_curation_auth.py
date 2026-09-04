"""The pool-curation endpoints rewrite what the router trades and must be authenticated."""
import pytest
from fastapi.testclient import TestClient

from arb_bot import api
from arb_bot.event_graph import LivePoolGraph, PoolMetadata

TOKEN = "s3cret-operator-token"


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _slipstream(n: int) -> dict:
    return {
        "address": addr(n), "token0": addr(100), "token1": addr(101),
        "venue": "aerodrome-slipstream", "pool_type": "concentrated", "tick_spacing": 100,
    }


class _Store:
    def __init__(self):
        self.rows = {}

    async def load(self):
        return list(self.rows.values())

    async def add(self, pool):
        self.rows[pool.address] = pool

    async def remove(self, address):
        return self.rows.pop(PoolMetadata.normalize_address(address), None) is not None

    async def replace(self, pools):
        materialised = list(pools)
        self.rows = {p.address: p for p in materialised}
        return len(materialised)


@pytest.fixture
def client(monkeypatch):
    graph = LivePoolGraph()
    graph.register(PoolMetadata.create(
        address=addr(1), token0=addr(100), token1=addr(101),
        venue="aerodrome", pool_type="xyk", stable=False,
    ))
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", _Store())
    monkeypatch.setitem(api.schema_status, "ready", True)
    monkeypatch.setattr(api.settings, "operator_api_token", TOKEN)
    return TestClient(api.app), graph


MUTATIONS = [
    ("POST", "/pools/select", [_slipstream(2)]),
    ("POST", "/pools/register", _slipstream(2)),
    ("DELETE", f"/pools/{addr(1)}", None),
]


@pytest.mark.parametrize("method,path,body", MUTATIONS)
def test_mutating_curation_rejects_an_unauthenticated_caller(client, method, path, body):
    http, graph = client
    before = graph.addresses()

    response = http.request(method, path, json=body)

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert graph.addresses() == before, "an unauthenticated request must change nothing"


@pytest.mark.parametrize("method,path,body", MUTATIONS)
def test_mutating_curation_rejects_a_wrong_token(client, method, path, body):
    http, graph = client
    before = graph.addresses()

    response = http.request(
        method, path, json=body, headers={"Authorization": "Bearer not-the-token"}
    )

    assert response.status_code == 401
    assert graph.addresses() == before


@pytest.mark.parametrize("method,path,body", MUTATIONS)
def test_mutating_curation_rejects_a_non_bearer_scheme(client, method, path, body):
    http, _graph = client
    response = http.request(
        method, path, json=body, headers={"Authorization": f"Basic {TOKEN}"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", MUTATIONS)
def test_mutating_curation_accepts_the_operator_token(client, method, path, body):
    http, _graph = client
    response = http.request(
        method, path, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code < 300, response.text


@pytest.mark.parametrize("method,path,body", MUTATIONS)
def test_curation_is_refused_entirely_when_no_token_is_configured(
    client, monkeypatch, method, path, body
):
    """Fails closed: an open-by-default toggle would leave every deployment as exposed."""
    http, graph = client
    monkeypatch.setattr(api.settings, "operator_api_token", "")
    before = graph.addresses()

    response = http.request(
        method, path, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 503
    assert "OPERATOR_API_TOKEN" in response.text
    assert graph.addresses() == before


def test_read_only_pool_endpoints_stay_open(client):
    """Only the endpoints that can rewrite routing are gated."""
    http, _graph = client
    assert http.get("/pools").status_code == 200
    assert http.get("/pools/addresses").status_code == 200


@pytest.mark.parametrize("configured", [8, 200])
def test_the_configured_endpoint_lag_reaches_discovery(monkeypatch, configured):
    """The setting existed but was never passed, so a tuned value silently meant 64.

    Deliberately parameterised away from the 64-block default: comparing the built value
    against the setting proves nothing while both are the default.
    """
    monkeypatch.setattr(api.settings, "pool_discovery_max_call_lag_blocks", configured)
    built = api._build_pool_discovery()
    assert built.max_call_endpoint_lag_blocks == configured


def test_the_other_discovery_knobs_reach_discovery_too(monkeypatch):
    """Same class of bug, same blast radius: a setting that quietly means nothing."""
    monkeypatch.setattr(api.settings, "pool_discovery_concurrency", 3)
    monkeypatch.setattr(api.settings, "pool_discovery_log_chunk_blocks", 777)
    monkeypatch.setattr(api.settings, "pool_discovery_from_block", 4242)
    monkeypatch.setattr(api.settings, "pool_discovery_token_allowlist", f"{addr(7)},{addr(8)}")

    built = api._build_pool_discovery()

    assert built.concurrency == 3
    assert built.log_chunk_blocks == 777
    assert built.uniswap_from_block == 4242
    assert built.token_allowlist == {addr(7), addr(8)}
    assert built.commit_lock is api._curation_lock


def test_the_split_verification_endpoint_reaches_discovery(monkeypatch):
    """The lag bound had nothing to measure: its endpoint had no setting to set.

    With both halves on one endpoint verify_endpoint_consistency() short-circuits, so the
    chain-id and lag guards never ran in the service at all -- the tuned lag bound was
    inert no matter what it was set to.
    """
    monkeypatch.setattr(api.settings, "pool_discovery_call_rpc_url", "http://127.0.0.1:8545")
    built = api._build_pool_discovery()
    assert built.call_w3 is not built.w3, "verification calls must use the configured endpoint"


def test_one_endpoint_by_default_keeps_calls_on_the_log_connection(monkeypatch):
    monkeypatch.setattr(api.settings, "pool_discovery_call_rpc_url", "")
    built = api._build_pool_discovery()
    assert built.call_w3 is built.w3


@pytest.mark.parametrize("token", ["tökén-with-non-ascii", "токен", "trailing-space "])
def test_an_unusable_operator_token_is_refused_as_a_misconfiguration(monkeypatch, client, token):
    """A token that no client can ever present is a server fault, and says so.

    Both shapes fail identically without the check: the operator sets a token, every
    request comes back rejected, and the rejection is indistinguishable from a caller
    using the wrong secret. Non-ASCII cannot go in an HTTP header at all; surrounding
    whitespace survives a real environment variable but is stripped off the presented
    half before comparison, so it matches nothing.

    503 rather than 401 for the same reason the unset-token branch is 503 -- and the
    detail names which defect, so it is fixable without guessing.
    """
    http, _graph = client
    monkeypatch.setattr(api.settings, "operator_api_token", token)

    response = http.request("POST", "/pools/register", json=_slipstream(2),
                            headers={"Authorization": "Bearer definitely-not-it"})

    assert response.status_code == 503
    assert "OPERATOR_API_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    "token,defect",
    [("tökén", "non-ASCII"), ("токен", "non-ASCII"),
     (" leading", "whitespace"), ("trailing ", "whitespace")],
)
def test_operator_token_defects_are_named(token, defect):
    assert defect in (api._operator_token_defect(token) or "")


@pytest.mark.parametrize("token", ["s3cret-operator-token", "a", "~!@#$%^&*()_+-="])
def test_a_usable_operator_token_has_no_defect(token):
    assert api._operator_token_defect(token) is None


def test_a_non_ascii_presented_token_is_a_401_not_a_500(monkeypatch):
    """The comparison must stay byte-safe even when the *caller* sends non-ASCII.

    hmac.compare_digest's str overload raises TypeError on either non-ASCII operand, so
    before the encode this was a 500 rather than the rejection it plainly is. Checked by
    calling the dependency directly: httpx refuses to put such a value on the wire.
    """
    from fastapi import HTTPException

    monkeypatch.setattr(api.settings, "operator_api_token", TOKEN)
    with pytest.raises(HTTPException) as caught:
        api.require_operator("Bearer tökén")
    assert caught.value.status_code == 401


def test_require_operator_still_accepts_a_matching_token(monkeypatch):
    """Encoding both sides must not break the comparison it exists to make safe."""
    monkeypatch.setattr(api.settings, "operator_api_token", TOKEN)
    assert api.require_operator(f"Bearer {TOKEN}") is None
