from decimal import Decimal

from py_market_maker.sdk_adapter import OrderBookSummary, OrderSummary

from py_market_maker.bot import (
    LiquidityRejectKind,
    LiquidityRejectReason,
    build_token_quote,
    liquidity_reject_reason,
    should_enforce_liquidity_quality,
)
from py_market_maker.config import parse_args


def test_liquidity_guard_follows_two_sided_live_flag():
    config = parse_args([])
    assert should_enforce_liquidity_quality(config) is False

    live_config = parse_args(_live_args())
    assert should_enforce_liquidity_quality(live_config) is True

    disabled_config = parse_args([*_live_args(), "--no-require-two-sided-live"])
    assert should_enforce_liquidity_quality(disabled_config) is False


def test_liquidity_rejects_missing_two_sided_book():
    reason = liquidity_reject_reason(
        [level("0.49", "10")],
        [],
        Decimal("0.01"),
        20,
        Decimal("5"),
    )

    assert reason == LiquidityRejectReason(LiquidityRejectKind.MISSING_TWO_SIDED_BOOK)


def test_liquidity_rejects_wide_book():
    reason = liquidity_reject_reason(
        [level("0.40", "10")],
        [level("0.70", "10")],
        Decimal("0.01"),
        20,
        Decimal("5"),
    )

    assert reason == LiquidityRejectReason(
        LiquidityRejectKind.SPREAD_TOO_WIDE,
        spread_ticks=Decimal("30"),
        max_spread_ticks=20,
    )


def test_liquidity_rejects_shallow_best_bid():
    reason = liquidity_reject_reason(
        [level("0.49", "4.9"), level("0.48", "100")],
        [level("0.51", "10")],
        Decimal("0.01"),
        20,
        Decimal("5"),
    )

    assert reason == LiquidityRejectReason(
        LiquidityRejectKind.BID_DEPTH_TOO_LOW,
        depth=Decimal("4.9"),
        min_depth=Decimal("5"),
    )


def test_liquidity_accepts_tight_book_with_enough_top_depth():
    reason = liquidity_reject_reason(
        [level("0.49", "2"), level("0.49", "3")],
        [level("0.51", "5")],
        Decimal("0.01"),
        20,
        Decimal("5"),
    )

    assert reason is None


def test_live_token_quote_records_liquidity_skip_reason():
    token_quote = build_token_quote(
        _market(),
        _token(),
        OrderBookSummary(
            bids=[level("0.40", "10")],
            asks=[level("0.70", "10")],
            tick_size="0.01",
        ),
        100.0,
        parse_args(_live_args()),
    )

    assert token_quote.plan is None
    assert token_quote.skip_reason == "liquidity quality check failed: spread is 30 ticks above max 20"


def test_disabled_live_liquidity_guard_allows_one_sided_fallback_quote():
    token_quote = build_token_quote(
        _market(),
        _token(),
        OrderBookSummary(bids=[level("0.49", "10")], asks=[], tick_size="0.01"),
        100.0,
        parse_args([*_live_args(), "--no-require-two-sided-live"]),
    )

    assert token_quote.plan is not None
    assert token_quote.skip_reason is None


def level(price: str, size: str) -> OrderSummary:
    return OrderSummary(price=price, size=size)


def _live_args() -> list[str]:
    return [
        "--live",
        "--private-key",
        "0xabc",
        "--deposit-wallet",
        "0xdef",
        "--chain-id",
        "137",
    ]


def _market() -> dict:
    return {
        "condition_id": "0xmarket",
        "market_slug": "market",
        "question": "Question?",
        "minimum_order_size": "1",
        "enable_order_book": True,
        "active": True,
        "closed": False,
        "archived": False,
        "accepting_orders": True,
        "tokens": [_token()],
    }


def _token() -> dict:
    return {"token_id": "yes", "outcome": "Yes", "price": "0.50"}
