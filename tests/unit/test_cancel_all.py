import signal

import pytest

from py_market_maker import bot
from py_market_maker.bot import (
    CANCEL_ORDER_BATCH_SIZE,
    MarketCandidate,
    PreflightRiskAudit,
    PreflightRiskAuditResult,
    ShutdownRequested,
    cancel_open_orders_for_markets,
    managed_token_ids,
    replace_managed_scope,
)
from py_market_maker.config import parse_args
from py_market_maker.state import PauseState


def test_managed_token_ids_are_deduped_in_scope_order():
    markets = [
        _market("market-a", ["yes", "no"]),
        _market("market-b", ["yes", "maybe"]),
    ]

    assert managed_token_ids(markets) == ["yes", "no", "maybe"]


def test_cancel_open_orders_for_markets_batches_and_verifies():
    order_count = CANCEL_ORDER_BATCH_SIZE + 1
    client = FakeClient(
        {
            "yes": [_order(f"yes-{index}") for index in range(order_count)],
            "no": [],
        }
    )

    summary = cancel_open_orders_for_markets(client, [_market("market", ["yes", "no", "yes"])])

    assert summary.markets_checked == 1
    assert summary.tokens_checked == 2
    assert summary.orders_found == order_count
    assert summary.canceled == order_count
    assert summary.not_canceled == 0
    assert summary.remaining_open == 0
    assert client.order_filters == [("yes", "market"), ("no", "market")] * 2
    assert client.cancel_batches == [
        [f"yes-{index}" for index in range(CANCEL_ORDER_BATCH_SIZE)],
        [f"yes-{CANCEL_ORDER_BATCH_SIZE}"],
    ]


def test_replace_managed_scope_preserves_last_non_empty_scope():
    first_market = _market("market-a", ["yes"])
    second_market = _market("market-b", ["no"])
    managed_scope = []

    replace_managed_scope(managed_scope, [MarketCandidate(first_market, is_new=False)])
    replace_managed_scope(managed_scope, [])

    assert managed_scope == [first_market]

    replace_managed_scope(managed_scope, [MarketCandidate(second_market, is_new=False)])

    assert managed_scope == [second_market]


def test_cancel_on_exit_preserves_signal_exit_code(monkeypatch):
    canceled_scopes = []

    def fake_run_cycles(_public_client, _live_client, _config, managed_scope=None):
        managed_scope.append(_market("market", ["yes"]))
        raise ShutdownRequested(signal.SIGTERM)

    def fake_cancel_scope_orders(_public_client, _live_client, _config, managed_scope=None):
        canceled_scopes.append(list(managed_scope))

    monkeypatch.setattr(bot, "run_cycles", fake_run_cycles)
    monkeypatch.setattr(bot, "cancel_scope_orders", fake_cancel_scope_orders)

    with pytest.raises(SystemExit) as error:
        bot.run_with_cancel_on_exit(object(), object(), object())

    assert error.value.code == 128 + signal.SIGTERM
    assert canceled_scopes == [[_market("market", ["yes"])]]


def test_run_clear_pause_exits_before_creating_client(monkeypatch, tmp_path):
    path = tmp_path / "paused.json"
    PauseState.save_reason(path, "risk breach test")
    created_clients = []

    def fake_clob_client(*_args, **_kwargs):
        created_clients.append(True)
        raise AssertionError("clear pause should not create a client")

    monkeypatch.setattr(bot, "ClobClient", fake_clob_client)

    bot.run(parse_args(["--clear-pause", "--pause-path", str(path)]))

    assert PauseState.load(path) is None
    assert created_clients == []


def test_run_cycles_stops_before_discovery_when_paused(monkeypatch, tmp_path):
    path = tmp_path / "paused.json"
    PauseState.save_reason(path, "risk breach test")
    discovered = []

    def fake_discover_cycle_candidates(*_args, **_kwargs):
        discovered.append(True)
        return []

    monkeypatch.setattr(bot, "discover_cycle_candidates", fake_discover_cycle_candidates)

    bot.run_cycles(
        object(),
        None,
        parse_args(["--pause-path", str(path)]),
    )

    assert discovered == []


def test_run_cycles_skips_quote_when_preflight_skips_cycle(monkeypatch, tmp_path):
    quoted_markets = []

    monkeypatch.setattr(
        bot,
        "discover_cycle_candidates",
        lambda *_args, **_kwargs: [MarketCandidate(_market("market", ["yes"]), is_new=False)],
    )
    monkeypatch.setattr(
        bot,
        "preflight_risk_audit",
        lambda *_args, **_kwargs: PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, []),
    )
    monkeypatch.setattr(
        bot,
        "quote_market",
        lambda _public_client, _live_client, market, _config, _snapshot=None: quoted_markets.append(market),
    )

    bot.run_cycles(
        object(),
        object(),
        parse_args(["--pause-path", str(tmp_path / "paused.json")]),
    )

    assert quoted_markets == []


class FakeClient:
    def __init__(self, orders_by_token):
        self.orders_by_token = {
            token_id: list(orders)
            for token_id, orders in orders_by_token.items()
        }
        self.cancel_batches = []
        self.order_filters = []

    def get_orders(self, params):
        self.order_filters.append((params.asset_id, params.market))
        return list(self.orders_by_token.get(params.asset_id, []))

    def cancel_orders(self, order_ids):
        self.cancel_batches.append(list(order_ids))
        canceled = []
        order_id_set = set(order_ids)
        for token_id, orders in self.orders_by_token.items():
            remaining = []
            for order in orders:
                if order["id"] in order_id_set:
                    canceled.append(order["id"])
                else:
                    remaining.append(order)
            self.orders_by_token[token_id] = remaining
        return {"canceled": canceled, "not_canceled": []}


def _market(slug, token_ids):
    return {
        "slug": slug,
        "condition_id": slug,
        "enable_order_book": True,
        "active": True,
        "closed": False,
        "archived": False,
        "accepting_orders": True,
        "tokens": [{"token_id": token_id} for token_id in token_ids],
    }


def _order(order_id):
    return {"id": order_id}


def test_cancel_rejects_missing_condition_id_before_api_calls():
    market = _market("slug", ["yes"])
    del market["condition_id"]
    with pytest.raises(ValueError, match="Market is missing its condition ID"):
        cancel_open_orders_for_markets(object(), [market])
