from decimal import Decimal
from types import SimpleNamespace

import pytest
from market_sdk.models import Market

from py_market_maker import sdk_adapter
from py_market_maker.config import parse_args


def market_record(*, version="v1", token_ids='["123","456"]'):
    return Market.model_validate({
        "id": "1",
        "version": version,
        "conditionId": "0x" + "1" * 64,
        "slug": "sample",
        "question": "Sample?",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": token_ids,
        "outcomePrices": '["0.4","0.6"]',
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    })


def test_ctf_gamma_market_keeps_tokens_and_safety_flags():
    record = sdk_adapter._market_record(market_record())
    assert record["condition_id"] == "0x" + "1" * 64
    assert record["tokens"] == [
        {"token_id": "123", "outcome": "Yes", "price": "0.4"},
        {"token_id": "456", "outcome": "No", "price": "0.6"},
    ]
    assert record["accepting_orders"] is True
    assert record["enable_order_book"] is True


def test_v2_or_missing_token_cannot_enter_ctf_quote_path():
    assert sdk_adapter._market_record(market_record(version="v2")) == {}
    assert sdk_adapter._market_record(market_record(token_ids='["123"]')) == {}


def test_environment_requires_explicit_sdk_endpoints():
    env = sdk_adapter.sdk_environment(parse_args([
        "--clob-host", "https://clob.example.org",
        "--gamma-host", "https://gamma.example.org",
        "--relayer-host", "https://relayer.example.org",
    ]))
    assert env._config.clob_url == "https://clob.example.org"
    assert env._config.gamma_url == "https://gamma.example.org"
    assert env._config.relayer_url == "https://relayer.example.org"
    with pytest.raises(SystemExit):
        parse_args(["--chain-id", "80002"])


def test_limit_order_passes_exact_decimal_and_post_only(monkeypatch):
    calls = []

    class FakeSecure:
        def create_limit_order(self, **kwargs):
            calls.append(kwargs)
            return "signed"

    monkeypatch.setattr(sdk_adapter, "SecureClient", FakeSecure)
    client = sdk_adapter.ClobClient(FakeSecure())
    order = client.create_order(sdk_adapter.OrderArgs(
        token_id="123", price=Decimal("0.41"), size=Decimal("2.000001"),
        side="BUY", post_only=True,
    ))
    assert order == "signed"
    assert calls == [{
        "token_id": "123", "price": "0.41", "size": "2.000001",
        "side": "BUY", "post_only": True,
    }]


def test_live_client_passes_deposit_wallet_to_unified_auth(monkeypatch):
    captured = {}

    class FakeSecure:
        @classmethod
        def create(cls, **kwargs):
            captured.update(kwargs)
            return cls()

    monkeypatch.setattr(sdk_adapter, "SecureClient", FakeSecure)
    monkeypatch.setattr(sdk_adapter, "_wallet_deployed", lambda _config: True)
    config = parse_args([
        "--live", "--private-key", "0x" + "2" * 64,
        "--deposit-wallet", "0x" + "3" * 40, "--chain-id", "137",
    ])
    sdk_adapter.ClobClient.authenticated(config)
    assert captured["wallet"] == config.deposit_wallet
    assert captured["private_key"] == config.private_key
    assert captured["environment"]._config.chain_id == 137


def test_undeployed_wallet_rejected_before_sdk_can_deploy(monkeypatch):
    calls = []

    class FakeSecure:
        @classmethod
        def create(cls, **kwargs):
            calls.append(kwargs)
            return cls()

    monkeypatch.setattr(sdk_adapter, "SecureClient", FakeSecure)
    monkeypatch.setattr(sdk_adapter, "_wallet_deployed", lambda _config: False)
    config = parse_args([
        "--live", "--private-key", "0x" + "2" * 64,
        "--deposit-wallet", "0x" + "3" * 40, "--chain-id", "137",
    ])
    with pytest.raises(ValueError, match="will not deploy"):
        sdk_adapter.ClobClient.authenticated(config)
    assert calls == []


def test_discovery_uses_supported_clob_route_and_site_scope(monkeypatch):
    condition_id = "0x" + "1" * 64
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{"condition_id": "0x" + "2" * 64}, {"condition_id": condition_id}],
                "next_cursor": sdk_adapter.END_CURSOR,
            }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(sdk_adapter.httpx, "get", fake_get)
    monkeypatch.setattr(sdk_adapter, "has_configured_site_scope", lambda: True)
    monkeypatch.setattr(sdk_adapter, "get_site_market_scope", lambda: SimpleNamespace(
        condition_ids={condition_id}, token_ids=set(),
    ))
    client = sdk_adapter.ClobClient(object(), parse_args([]))
    assert client.get_markets(sdk_adapter.INITIAL_CURSOR) == {
        "data": [{"condition_id": condition_id}], "next_cursor": sdk_adapter.END_CURSOR,
    }
    assert calls == [(
        "https://clob.kuest.com/markets",
        {"params": {"next_cursor": sdk_adapter.INITIAL_CURSOR}, "timeout": 10},
    )]


def test_post_cancel_and_balance_follow_unified_result_contract(monkeypatch):
    calls = []

    class FakeSecure:
        def post_order(self, signed):
            calls.append(("post", signed))
            return SimpleNamespace(ok=True, order_id="order-1", status="live")

        def cancel_orders(self, *, order_ids):
            calls.append(("cancel", order_ids))
            return SimpleNamespace(canceled=("order-1",), not_canceled={"order-2": "missing"})

        def get_balance_allowance(self, **kwargs):
            calls.append(("balance", kwargs))
            return SimpleNamespace(balance=2_000_000)

    monkeypatch.setattr(sdk_adapter, "SecureClient", FakeSecure)
    client = sdk_adapter.ClobClient(FakeSecure())
    assert client.post_order("signed") == {
        "success": True, "orderID": "order-1", "status": "live",
    }
    assert client.cancel_orders(["order-1", "order-2"]) == {
        "canceled": ["order-1"], "not_canceled": ["order-2"],
    }
    assert client.get_balance_allowance(sdk_adapter.BalanceAllowanceParams(
        sdk_adapter.AssetType.CONDITIONAL, "123",
    )).balance == 2_000_000
    assert calls == [
        ("post", "signed"),
        ("cancel", ["order-1", "order-2"]),
        ("balance", {"asset_type": "CONDITIONAL", "token_id": "123"}),
    ]
