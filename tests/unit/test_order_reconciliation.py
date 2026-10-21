from decimal import Decimal

from py_market_maker.sdk_adapter import OrderBookSummary, OrderSummary

from py_market_maker import bot
from py_market_maker.bot import (
    InventoryBuyRoom,
    LiveMarketState,
    LiveTokenState,
    PreflightMarketSnapshot,
    PreflightRiskAuditResult,
    QuoteBand,
    QuotePlan,
    TokenQuote,
    post_quote_plan,
    inventory_adjusted_buy_size,
    pre_post_liquidity_reject_reason,
    price_move_reject_reason,
    position_reconcile_error_for,
    preflight_risk_audit,
    preflight_snapshot_for_market,
    preflight_stale_data_reason,
    quote_market,
    stale_input_reason,
    token_cost_basis,
    token_ledger_position,
)
from py_market_maker.config import parse_args
from py_market_maker.market_loss import BUY, SELL, ProposedOrder
from py_market_maker.state import FillLedger, FillRecord, PauseState


def test_cancel_refreshes_open_orders_before_posting_replacement():
    stale_order = _open_order("stale", BUY, "0.46", "5")
    market_state = _market_state(open_orders=[stale_order])
    client = FakeClient(
        open_order_pages={"yes": [[]]},
        post_responses=[_post_response(True, "replacement")],
    )

    post_quote_plan(client, _plan(buy_band=_buy_band()), parse_args([]), market_state)

    assert client.cancel_batches == [["stale"]]
    assert client.get_order_tokens == ["yes"]
    assert len(client.posted_orders) == 1
    assert market_state.open_orders("yes") == []
    assert market_state.pending_orders == [
        ProposedOrder("yes", BUY, Decimal("0.49"), Decimal("10"))
    ]


def test_cancel_refresh_skips_post_when_canceled_order_is_still_open():
    stale_order = _open_order("stale", BUY, "0.46", "5")
    market_state = _market_state(open_orders=[stale_order])
    client = FakeClient(
        open_order_pages={"yes": [[stale_order]]},
        post_responses=[_post_response(True, "replacement")],
    )

    post_quote_plan(client, _plan(buy_band=_buy_band()), parse_args([]), market_state)

    assert client.cancel_batches == [["stale"]]
    assert client.get_order_tokens == ["yes"]
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == [stale_order]
    assert market_state.pending_orders == []


def test_rejected_post_does_not_record_pending_order_and_refreshes_state():
    market_state = _market_state()
    client = FakeClient(
        open_order_pages={"yes": [[]]},
        post_responses=[_post_response(False, "rejected")],
    )

    post_quote_plan(client, _plan(buy_band=_buy_band()), parse_args([]), market_state)

    assert len(client.posted_orders) == 1
    assert client.get_order_tokens == ["yes"]
    assert market_state.pending_orders == []
    assert market_state.open_orders("yes") == []


def test_failed_batch_response_dedupes_partially_filled_pending_order_now_open():
    market_state = _market_state(balance=Decimal("10"))
    posted_buy = _open_order("posted-buy", BUY, "0.49", "3")
    client = FakeClient(
        open_order_pages={"yes": [[posted_buy]]},
        post_responses=[
            _post_response(True, "posted-buy"),
            _post_response(False, "rejected-sell"),
        ],
    )

    post_quote_plan(
        client,
        _plan(
            buy_band=_buy_band(avg_size=Decimal("5")),
            sell_band=_sell_band(avg_size=Decimal("5")),
        ),
        parse_args(["--quote-sides", "both"]),
        market_state,
    )

    assert [order["side"] for order in client.created_orders] == [BUY, SELL]
    assert market_state.pending_orders == []
    assert market_state.open_orders("yes") == [posted_buy]


def test_stale_book_skips_live_post(monkeypatch):
    now = 100.0
    monkeypatch.setattr(bot.time, "monotonic", lambda: now)
    stale_order = _open_order("stale", BUY, "0.46", "5")
    market_state = _market_state(open_orders=[stale_order], now=now)
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band(), book_fetched_at=now - 11),
        parse_args(["--max-data-age-secs", "10"]),
        market_state,
    )

    assert client.cancel_batches == []
    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == [stale_order]
    assert market_state.pending_orders == []


def test_stale_book_after_cancel_refresh_skips_replacement_post(monkeypatch):
    times = iter([109.0, 109.5, 111.0])
    monkeypatch.setattr(bot.time, "monotonic", lambda: next(times))
    stale_order = _open_order("stale", BUY, "0.46", "5")
    market_state = _market_state(open_orders=[stale_order], now=100.0)
    client = FakeClient(
        open_order_pages={"yes": [[]]},
        post_responses=[_post_response(True, "posted")],
    )

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band(), book_fetched_at=100.0),
        parse_args(["--max-data-age-secs", "10"]),
        market_state,
    )

    assert client.cancel_batches == [["stale"]]
    assert client.get_order_tokens == ["yes"]
    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == []
    assert market_state.pending_orders == []


def test_stale_input_reason_flags_data_older_than_threshold():
    reason = stale_input_reason("order book", 100.0, 111.0, 10.0)

    assert reason is not None
    assert "order book" in reason


def test_stale_input_reason_accepts_fresh_data():
    assert stale_input_reason("order book", 100.0, 109.0, 10.0) is None


def test_price_move_guard_rejects_large_fair_move():
    reason = price_move_reject_reason(
        Decimal("0.50"),
        Decimal("0.53"),
        Decimal("0.01"),
        2,
    )

    assert reason is not None
    assert "fair moved 3 ticks" in reason


def test_price_move_guard_allows_move_at_limit():
    assert price_move_reject_reason(
        Decimal("0.50"),
        Decimal("0.52"),
        Decimal("0.01"),
        2,
    ) is None


def test_pre_post_liquidity_guard_rejects_missing_two_sided_book():
    reason = pre_post_liquidity_reject_reason(
        OrderBookSummary(
            bids=[OrderSummary(price="0.49", size="10")],
            asks=[],
            tick_size="0.01",
        ),
        parse_args(_live_args()),
    )

    assert reason is not None
    assert "missing" in reason.message()


def test_pre_post_liquidity_guard_follows_live_flag():
    reason = pre_post_liquidity_reject_reason(
        OrderBookSummary(
            bids=[OrderSummary(price="0.49", size="10")],
            asks=[],
            tick_size="0.01",
        ),
        parse_args(["--require-two-sided-live"]),
    )

    assert reason is None


def test_preflight_stale_data_reason_flags_stale_token_inputs():
    now = 100.0
    token_quotes = [
        TokenQuote(
            token_id="yes",
            outcome="Yes",
            fair_price=Decimal("0.50"),
            book_fetched_at=now,
            plan=None,
            skip_reason=None,
        )
    ]
    market_state = _market_state(now=now)
    market_state.token_state("yes").open_orders_fetched_at = now - 11

    reason = preflight_stale_data_reason(
        token_quotes,
        market_state,
        now,
        parse_args(["--max-data-age-secs", "10"]),
    )

    assert reason is not None
    assert "token yes open orders" in reason


def test_configured_price_range_can_skip_quote_plan():
    quote = bot.build_token_quote(
        _market(),
        {"token_id": "yes", "outcome": "Yes", "price": "0.50"},
        _book(),
        bot.time.monotonic(),
        parse_args(["--min-price", "0.55"]),
    )

    assert quote.plan is None


def test_inventory_adjusted_buy_size_caps_to_remaining_room():
    assert inventory_adjusted_buy_size(Decimal("5"), Decimal("1"), Decimal("3")) == Decimal("3")


def test_inventory_adjusted_buy_size_skips_when_room_is_below_minimum():
    assert inventory_adjusted_buy_size(Decimal("5"), Decimal("1"), Decimal("0.5")) is None


def test_token_cost_basis_uses_realized_average_cost_after_sells():
    records = [
        _fill_record("buy-a", "yes", BUY, "10", "0.40", 1),
        _fill_record("sell-a", "yes", SELL, "4", "0.70", 2),
    ]

    assert token_cost_basis(records, Decimal("6"), Decimal("0.50")) == Decimal("2.40")


def test_token_cost_basis_falls_back_for_uncovered_balance():
    records = [_fill_record("buy-a", "yes", BUY, "2", "0.40", 1)]

    assert token_cost_basis(records, Decimal("5"), Decimal("0.50")) == Decimal("2.30")


def test_token_ledger_position_tracks_buys_and_sells():
    records = [
        _fill_record("buy-a", "yes", BUY, "10", "0.40", 1),
        _fill_record("sell-a", "yes", SELL, "4", "0.70", 2),
    ]

    assert token_ledger_position(records) == Decimal("6")


def test_position_reconcile_error_respects_tolerance():
    assert position_reconcile_error_for(
        Decimal("6.0000005"),
        Decimal("6"),
        Decimal("0.000001"),
    ) is None

    error = position_reconcile_error_for(
        Decimal("7"),
        Decimal("6"),
        Decimal("0.000001"),
    )

    assert error is not None
    assert error.live_balance == Decimal("7")
    assert error.ledger_position == Decimal("6")
    assert error.difference == Decimal("1")
    assert error.tolerance == Decimal("0.000001")


def test_market_state_reports_unreconciled_position():
    market_state = _market_state(
        balance=Decimal("7"),
        position_reconcile_error=position_reconcile_error_for(
            Decimal("7"),
            Decimal("6"),
            Decimal("0.000001"),
        ),
    )

    reason = market_state.position_reconcile_reject_reason()

    assert reason is not None
    assert "token yes" in reason
    assert "live balance 7" in reason


def test_exposure_uses_token_cost_basis_for_existing_balances():
    market_state = _market_state(balance=Decimal("5"), cost_basis=Decimal("2"))

    assert market_state.exposure().worst_loss() == Decimal("2")


def test_live_market_state_fetches_fills_and_uses_cost_basis(tmp_path):
    token_quotes = [
        TokenQuote(
            token_id="yes",
            outcome="Yes",
            fair_price=Decimal("0.50"),
            book_fetched_at=bot.time.monotonic(),
            plan=None,
            skip_reason=None,
        )
    ]
    client = FakeClient(
        balances={"yes": Decimal("5")},
        trade_pages={
            "yes": [[{
                "id": "trade-a",
                "asset_id": "yes",
                "market": "market",
                "side": BUY,
                "size": "5",
                "price": "0.40",
                "status": "Matched",
                "match_time": 100,
            }]]
        },
    )

    market_state = LiveMarketState.load(
        client,
        token_quotes,
        parse_args([
            "--fill-state-path",
            str(tmp_path / "fills.json"),
            "--fill-max-records",
            "10",
        ]),
    )

    assert market_state.token_state("yes").cost_basis == Decimal("2.00")
    assert market_state.exposure().worst_loss() == Decimal("2.00")
    assert FillLedger.load(tmp_path / "fills.json").latest_matched_at_unix_secs("yes") == 100


def test_live_market_state_records_position_reconcile_error(tmp_path):
    token_quotes = [
        TokenQuote(
            token_id="yes",
            outcome="Yes",
            fair_price=Decimal("0.50"),
            book_fetched_at=bot.time.monotonic(),
            plan=None,
            skip_reason=None,
        )
    ]
    client = FakeClient(
        balances={"yes": Decimal("7")},
        trade_pages={
            "yes": [[{
                "id": "trade-a",
                "asset_id": "yes",
                "market": "market",
                "side": BUY,
                "size": "6",
                "price": "0.40",
                "status": "Matched",
                "match_time": 100,
            }]]
        },
    )

    market_state = LiveMarketState.load(
        client,
        token_quotes,
        parse_args([
            "--fill-state-path",
            str(tmp_path / "fills.json"),
            "--position-reconcile-tolerance",
            "0.000001",
        ]),
    )

    reason = market_state.position_reconcile_reject_reason()

    assert reason is not None
    assert "live balance 7" in reason
    assert "fill-ledger position 6" in reason


def test_inventory_buy_room_counts_balance_and_open_buys():
    open_buy = _open_order("open-buy", BUY, "0.40", "5")
    market_state = _market_state(open_orders=[open_buy], balance=Decimal("10"))

    room = market_state.inventory_buy_room(
        "yes",
        [],
        parse_args(["--max-inventory-per-token", "20", "--max-inventory-per-market", "50"]),
    )

    assert room == InventoryBuyRoom(
        token_position=Decimal("15"),
        market_inventory=Decimal("15"),
        room=Decimal("5"),
    )


def test_inventory_buy_room_ignores_open_sells():
    open_sell = _open_order("open-sell", SELL, "0.60", "5")
    market_state = _market_state(open_orders=[open_sell], balance=Decimal("20"))

    room = market_state.inventory_buy_room(
        "yes",
        [],
        parse_args(["--max-inventory-per-token", "25", "--max-inventory-per-market", "50"]),
    )

    assert room == InventoryBuyRoom(
        token_position=Decimal("20"),
        market_inventory=Decimal("20"),
        room=Decimal("5"),
    )


def test_buy_size_is_capped_by_token_inventory_room():
    market_state = _market_state(balance=Decimal("22"))
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--max-inventory-per-token", "25", "--max-inventory-per-market", "50"]),
        market_state,
    )

    assert len(client.created_orders) == 1
    assert Decimal(client.created_orders[0]["size"]) == Decimal("3.0")
    assert market_state.pending_orders == [
        ProposedOrder("yes", BUY, Decimal("0.49"), Decimal("3"))
    ]


def test_buy_is_skipped_when_inventory_room_is_below_minimum():
    market_state = _market_state(balance=Decimal("24.5"))
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--max-inventory-per-token", "25", "--max-inventory-per-market", "50"]),
        market_state,
    )

    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.pending_orders == []


def test_risk_breaches_detect_token_inventory_over_limit():
    market_state = _market_state(balance=Decimal("11"))
    breaches = market_state.risk_breaches(parse_args(["--max-inventory-per-token", "10"]))

    assert any(
        breach.kind == "token_inventory"
        and breach.token_id == "yes"
        and breach.value == Decimal("11")
        and breach.limit == Decimal("10")
        for breach in breaches
    )


def test_risk_breaches_detect_market_inventory_over_limit():
    market_state = _market_state(balance=Decimal("8"), no_balance=Decimal("8"))
    breaches = market_state.risk_breaches(parse_args(["--max-inventory-per-market", "15"]))

    assert any(
        breach.kind == "market_inventory"
        and breach.value == Decimal("16")
        and breach.limit == Decimal("15")
        for breach in breaches
    )


def test_risk_breaches_detect_market_loss_over_limit():
    market_state = _market_state(balance=Decimal("5"))
    breaches = market_state.risk_breaches(parse_args(["--max-loss-per-market", "2"]))

    assert any(
        breach.kind == "market_loss"
        and breach.value == Decimal("2.50")
        and breach.limit == Decimal("2")
        for breach in breaches
    )


def test_risk_breaches_detect_market_collateral_over_limit():
    open_buy = _open_order("open-buy", BUY, "0.50", "10")
    market_state = _market_state(open_orders=[open_buy])
    breaches = market_state.risk_breaches(parse_args(["--max-collateral-per-market", "4"]))

    assert any(
        breach.kind == "market_collateral"
        and breach.value == Decimal("5.00")
        and breach.limit == Decimal("4")
        for breach in breaches
    )


def test_risk_breach_skips_new_quotes_without_canceling_by_default():
    open_buy = _open_order("open-buy", BUY, "0.49", "5")
    market_state = _market_state(open_orders=[open_buy], balance=Decimal("11"))
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--max-inventory-per-token", "10"]),
        market_state,
    )

    assert client.cancel_batches == []
    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == [open_buy]


def test_cancel_on_risk_breach_cancels_open_buys_and_refreshes_state():
    open_buy = _open_order("open-buy", BUY, "0.49", "5")
    open_sell = _open_order("open-sell", SELL, "0.51", "5")
    market_state = _market_state(open_orders=[open_buy, open_sell], balance=Decimal("11"))
    client = FakeClient(open_order_pages={"yes": [[open_sell]]})

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band(), sell_band=_sell_band()),
        parse_args([
            "--live",
            "--private-key",
            "0xabc",
            "--deposit-wallet",
            "0xdef",
            "--chain-id",
            "137",
            "--cancel-on-risk-breach",
            "--max-inventory-per-token",
            "10",
        ]),
        market_state,
    )

    assert client.cancel_batches == [["open-buy"]]
    assert client.get_order_tokens == ["yes"]
    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == [open_sell]


def test_cancel_on_risk_breach_does_not_cancel_unrelated_token_buys():
    open_buy = _open_order("open-buy", BUY, "0.49", "5")
    market_state = _market_state(open_orders=[open_buy], no_balance=Decimal("11"))
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args([
            "--live",
            "--private-key",
            "0xabc",
            "--deposit-wallet",
            "0xdef",
            "--chain-id",
            "137",
            "--cancel-on-risk-breach",
            "--max-inventory-per-token",
            "10",
        ]),
        market_state,
    )

    assert client.cancel_batches == []
    assert client.get_order_tokens == []
    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.open_orders("yes") == [open_buy]


def test_pause_on_risk_breach_writes_pause_file_and_skips_new_quotes(tmp_path):
    pause_path = tmp_path / "paused.json"
    market_state = _market_state(balance=Decimal("11"))
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args([
            "--live",
            "--private-key",
            "0xabc",
            "--deposit-wallet",
            "0xdef",
            "--chain-id",
            "137",
            "--pause-on-risk-breach",
            "--pause-path",
            str(pause_path),
            "--max-inventory-per-token",
            "10",
        ]),
        market_state,
    )

    pause = PauseState.load(pause_path)
    assert pause is not None
    assert pause.reason == "risk breach market Yes: token yes inventory 11 exceeds limit 10"
    assert client.created_orders == []
    assert client.posted_orders == []


def test_active_pause_skips_stale_order_cancel(tmp_path):
    pause_path = tmp_path / "paused.json"
    PauseState.save_reason(pause_path, "manual stop")
    stale_order = _open_order("stale", BUY, "0.46", "5")
    market_state = _market_state(open_orders=[stale_order])
    client = FakeClient(open_order_pages={"yes": [[]]})

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--pause-path", str(pause_path)]),
        market_state,
    )

    assert client.cancel_batches == []
    assert client.get_order_tokens == []
    assert market_state.open_orders("yes") == [stale_order]


def test_active_pause_skips_posting_orders(tmp_path):
    pause_path = tmp_path / "paused.json"
    PauseState.save_reason(pause_path, "manual stop")
    market_state = _market_state()
    client = FakeClient(post_responses=[_post_response(True, "posted")])

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--pause-path", str(pause_path)]),
        market_state,
    )

    assert len(client.created_orders) == 1
    assert client.posted_orders == []
    assert market_state.pending_orders == []


def test_pre_post_move_guard_skips_post_when_fair_moves():
    market_state = _market_state()
    client = FakeClient(
        books={"yes": _book(bid="0.53", ask="0.55")},
        post_responses=[_post_response(True, "posted")],
    )

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--max-pre-post-move-ticks", "2"]),
        market_state,
        client,
    )

    assert client.get_book_tokens == ["yes"]
    assert len(client.created_orders) == 1
    assert client.posted_orders == []
    assert market_state.pending_orders == []


def test_buy_is_skipped_when_collateral_budget_is_too_small():
    market_state = _market_state()
    client = FakeClient(
        collateral_balance=Decimal("1"),
        post_responses=[_post_response(True, "posted")],
    )

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band()),
        parse_args(["--min-free-collateral", "1"]),
        market_state,
    )

    assert client.created_orders == []
    assert client.posted_orders == []
    assert market_state.pending_orders == []


def test_max_open_orders_per_token_trims_extra_kept_orders_before_posting():
    older = _open_order("older", BUY, "0.49", "5", "2026-01-01T00:00:00+00:00")
    newer = _open_order("newer", BUY, "0.49", "5", "2026-01-01T00:00:01+00:00")
    market_state = _market_state(open_orders=[older, newer])
    client = FakeClient(open_order_pages={"yes": [[older]]})

    post_quote_plan(
        client,
        _plan(buy_band=_buy_band(avg_size=Decimal("10"))),
        parse_args(["--max-open-orders-per-token", "1"]),
        market_state,
    )

    assert client.cancel_batches == [["newer"]]
    assert client.created_orders == []
    assert market_state.open_orders("yes") == [older]


def test_preflight_risk_audit_cancels_and_pauses_on_market_breach(tmp_path):
    pause_path = tmp_path / "paused.json"
    open_buy = _open_order("open-buy", BUY, "0.49", "5")
    client = FakeClient(
        books={"yes": _book()},
        balances={"yes": Decimal("11")},
        open_order_pages={"yes": [[open_buy]]},
    )

    result = preflight_risk_audit(
        client,
        client,
        [_market()],
        parse_args([
            "--live",
            "--private-key",
            "0xabc",
            "--deposit-wallet",
            "0xdef",
            "--chain-id",
            "137",
            "--cancel-on-risk-breach",
            "--pause-on-risk-breach",
            "--pause-path",
            str(pause_path),
            "--max-inventory-per-token",
            "10",
        ]),
    )

    pause = PauseState.load(pause_path)
    assert result.result == PreflightRiskAuditResult.STOP
    assert client.cancel_batches == [["open-buy"]]
    assert pause is not None
    assert pause.reason == "preflight risk breach market: token yes inventory 16 exceeds limit 10"


def test_preflight_risk_audit_skips_cycle_on_breach_without_pause():
    client = FakeClient(
        books={"yes": _book()},
        balances={"yes": Decimal("11")},
    )

    result = preflight_risk_audit(
        client,
        client,
        [_market()],
        parse_args([
            "--live",
            "--private-key",
            "0xabc",
            "--deposit-wallet",
            "0xdef",
            "--chain-id",
            "137",
            "--max-inventory-per-token",
            "10",
        ]),
    )

    assert result.result == PreflightRiskAuditResult.SKIP_CYCLE
    assert client.cancel_batches == []


def test_preflight_risk_audit_returns_market_snapshots():
    client = FakeClient(
        books={"yes": _book()},
        balances={"yes": Decimal("1")},
        trade_pages={
            "yes": [[{
                "id": "trade-a",
                "asset_id": "yes",
                "market": "market",
                "side": BUY,
                "size": "1",
                "price": "0.40",
                "status": "Matched",
                "match_time": 100,
            }]]
        },
    )

    result = preflight_risk_audit(
        client,
        client,
        [_market()],
        parse_args([]),
    )

    assert result.result == PreflightRiskAuditResult.CONTINUE
    assert len(result.snapshots) == 1
    assert result.snapshots[0].market_key == "market"
    assert [quote.token_id for quote in result.snapshots[0].token_quotes] == ["yes"]
    assert result.snapshots[0].market_state.token_state("yes").balance == Decimal("1")


def test_preflight_risk_audit_stops_when_total_open_buy_collateral_breaches(tmp_path):
    open_buy_a = _open_order("open-buy-a", BUY, "0.80", "40")
    open_buy_b = _open_order("open-buy-b", BUY, "0.80", "40")
    client = FakeClient(
        books={"yes": _book()},
        open_order_pages={"yes": [[open_buy_a], [open_buy_b]]},
    )

    result = preflight_risk_audit(
        client,
        client,
        [_market("market-a"), _market("market-b")],
        parse_args([
            "--max-loss-per-market",
            "100",
            "--max-collateral-per-market",
            "100",
            "--max-inventory-per-token",
            "100",
            "--max-inventory-per-market",
            "200",
            "--max-total-collateral",
            "50",
            "--fill-state-path",
            str(tmp_path / "fills.json"),
        ]),
    )

    assert result.result == PreflightRiskAuditResult.STOP


def test_preflight_snapshot_rejects_market_key_mismatch():
    snapshot = PreflightMarketSnapshot(
        market_key="market-a",
        token_quotes=[],
        market_state=_market_state(),
    )

    try:
        preflight_snapshot_for_market(snapshot, "market-b")
    except RuntimeError as error:
        assert "preflight snapshot market mismatch" in str(error)
    else:
        raise AssertionError("mismatched preflight snapshot should fail")


def test_quote_market_reuses_preflight_snapshot_before_pre_post_refresh():
    market_state = _market_state()
    snapshot = PreflightMarketSnapshot(
        market_key="market",
        token_quotes=[
            TokenQuote(
                token_id="yes",
                outcome="Yes",
                fair_price=Decimal("0.50"),
                book_fetched_at=bot.time.monotonic(),
                plan=_plan(buy_band=_buy_band()),
                skip_reason=None,
            )
        ],
        market_state=market_state,
    )
    client = FakeClient(
        books={"yes": _book()},
        post_responses=[_post_response(True, "posted")],
    )

    quote_market(client, client, _market(), parse_args([]), snapshot)

    assert client.get_book_tokens == ["yes"]
    assert len(client.created_orders) == 1
    assert len(client.posted_orders) == 1


class FakeClient:
    def __init__(
        self,
        open_order_pages=None,
        post_responses=None,
        books=None,
        balances=None,
        trade_pages=None,
        collateral_balance=Decimal("100"),
    ):
        self.open_order_pages = {
            token_id: list(pages)
            for token_id, pages in (open_order_pages or {}).items()
        }
        self.post_responses = list(post_responses or [])
        self.books = dict(books or {})
        self.balances = dict(balances or {})
        self.collateral_balance = collateral_balance
        self.trade_pages = {
            token_id: list(pages)
            for token_id, pages in (trade_pages or {}).items()
        }
        self.cancel_batches = []
        self.created_orders = []
        self.posted_orders = []
        self.get_order_tokens = []
        self.get_book_tokens = []
        self.get_trade_tokens = []

    def get_order_book(self, token_id):
        self.get_book_tokens.append(token_id)
        return self.books[token_id]

    def get_balance_allowance(self, params):
        if params.asset_type == bot.AssetType.COLLATERAL:
            return {"balance": str(self.collateral_balance * bot.CONDITIONAL_TOKEN_BASE_UNITS)}
        balance = self.balances.get(params.token_id, Decimal("0"))
        return {"balance": str(balance * bot.CONDITIONAL_TOKEN_BASE_UNITS)}

    def get_trades(self, params, next_cursor=None):
        self.get_trade_tokens.append(params.asset_id)
        pages = self.trade_pages.get(params.asset_id, [])
        return list(pages.pop(0)) if pages else {"data": [], "next_cursor": bot.END_CURSOR}

    def cancel_orders(self, order_ids):
        self.cancel_batches.append(list(order_ids))
        return {"canceled": list(order_ids), "not_canceled": []}

    def get_orders(self, params):
        self.get_order_tokens.append(params.asset_id)
        pages = self.open_order_pages.get(params.asset_id, [])
        return list(pages.pop(0)) if pages else []

    def create_order(self, order_args):
        order = {
            "token_id": order_args.token_id,
            "price": str(order_args.price),
            "size": str(order_args.size),
            "side": order_args.side,
        }
        self.created_orders.append(order)
        return order

    def post_order(self, order, orderType=None, post_only=True):
        self.posted_orders.append((order, orderType, post_only))
        return self.post_responses.pop(0)


def _market_state(
    open_orders=None,
    balance=Decimal("0"),
    no_balance=Decimal("0"),
    cost_basis=None,
    position_reconcile_error=None,
    now=None,
):
    now = bot.time.monotonic() if now is None else now
    return LiveMarketState(
        tokens=[
            LiveTokenState(
                token_id="yes",
                fair_price=Decimal("0.50"),
                balance=balance,
                balance_fetched_at=now,
                open_orders=list(open_orders or []),
                open_orders_fetched_at=now,
                cost_basis=cost_basis,
                position_reconcile_error=position_reconcile_error,
            ),
            LiveTokenState("no", Decimal("0.50"), no_balance, now, [], now),
        ],
        pending_orders=[],
    )


def _plan(buy_band=None, sell_band=None, book_fetched_at=None):
    book_fetched_at = bot.time.monotonic() if book_fetched_at is None else book_fetched_at
    return QuotePlan(
        market_key="market",
        market_slug="market",
        question="Question?",
        token_id="yes",
        outcome="Yes",
        fair_price=Decimal("0.50"),
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.51"),
        book_fetched_at=book_fetched_at,
        buy_band=buy_band,
        sell_band=sell_band,
    )


def _buy_band(avg_size=Decimal("10")):
    return QuoteBand(
        side=BUY,
        price=Decimal("0.49"),
        min_price=Decimal("0.47"),
        max_price=Decimal("0.49"),
        minimum_size=Decimal("1"),
        min_size=Decimal("5"),
        avg_size=avg_size,
        max_size=Decimal("15"),
    )


def _sell_band(avg_size=Decimal("10")):
    return QuoteBand(
        side=SELL,
        price=Decimal("0.51"),
        min_price=Decimal("0.51"),
        max_price=Decimal("0.53"),
        minimum_size=Decimal("1"),
        min_size=Decimal("5"),
        avg_size=avg_size,
        max_size=Decimal("15"),
    )


def _open_order(order_id, side, price, size, created_at=None):
    return {
        "id": order_id,
        "asset_id": "yes",
        "side": side,
        "price": price,
        "original_size": size,
        "size_matched": "0",
        **({"created_at": created_at} if created_at is not None else {}),
    }


def _fill_record(record_id, token_id, side, size, price, matched_at_unix_secs):
    return FillRecord(
        id=record_id,
        token_id=token_id,
        market="market",
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        status="Matched",
        matched_at_unix_secs=matched_at_unix_secs,
    )


def _post_response(success, order_id):
    return {
        "success": success,
        "orderID": order_id,
        "status": "live" if success else "rejected",
    }


def _market(slug="market"):
    return {
        "slug": slug,
        "question": "Question?",
        "enable_order_book": True,
        "active": True,
        "closed": False,
        "archived": False,
        "accepting_orders": True,
        "tokens": [{"token_id": "yes", "outcome": "Yes", "price": "0.50"}],
    }


def _live_args():
    return [
        "--live",
        "--private-key",
        "0xabc",
        "--deposit-wallet",
        "0xdef",
        "--chain-id",
        "137",
    ]


def _book(bid="0.49", ask="0.51"):
    return OrderBookSummary(
        bids=[OrderSummary(price=bid, size="10")],
        asks=[OrderSummary(price=ask, size="10")],
        tick_size="0.01",
    )
