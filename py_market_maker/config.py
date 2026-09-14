from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Sequence

POLYGON = 137
AMOY = 80002


class DiscoveryMode(str, Enum):
    AUTO = "auto"
    SAMPLING = "sampling"
    SITE = "site"


class QuoteSides(str, Enum):
    BUY = "buy"
    SELL = "sell"
    BOTH = "both"

    def includes_buy(self) -> bool:
        return self in (QuoteSides.BUY, QuoteSides.BOTH)

    def includes_sell(self) -> bool:
        return self in (QuoteSides.SELL, QuoteSides.BOTH)


@dataclass(frozen=True)
class Config:
    clob_host: str
    live: bool
    private_key: str | None
    deposit_wallet: str | None
    chain_id: int | None
    discovery: DiscoveryMode
    max_markets: int
    max_pages: int
    order_size: Decimal
    edge_ticks: int
    min_spread_ticks: int
    band_min_margin_ticks: int | None
    band_avg_margin_ticks: int | None
    band_max_margin_ticks: int | None
    band_min_size: Decimal | None
    band_avg_size: Decimal | None
    band_max_size: Decimal | None
    min_price: Decimal
    max_price: Decimal
    max_collateral_per_market: Decimal
    max_loss_per_market: Decimal
    max_inventory_per_token: Decimal
    max_inventory_per_market: Decimal
    max_total_collateral: Decimal
    min_free_collateral: Decimal
    max_book_spread_ticks: int
    max_pre_post_move_ticks: int
    max_open_orders_per_token: int
    min_top_depth: Decimal
    quote_sides: QuoteSides
    allow_single_sided: bool
    respect_reward_min_size: bool
    cancel_before_quote: bool
    cancel_all: bool
    cancel_all_on_exit: bool
    cancel_on_risk_breach: bool
    pause_on_risk_breach: bool
    clear_pause: bool
    pause_path: Path
    post_only: bool
    require_two_sided_live: bool
    max_data_age_secs: int
    discover_only: bool
    cycles: int
    refresh_secs: int
    state_path: Path
    fill_state_path: Path
    fill_max_records: int
    position_reconcile_tolerance: Decimal
    event_slug: str | None = None


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = Config(
        clob_host=args.clob_host,
        live=args.live,
        private_key=args.private_key,
        deposit_wallet=args.deposit_wallet,
        chain_id=args.chain_id,
        discovery=DiscoveryMode(args.discovery),
        event_slug=args.event_slug,
        max_markets=args.max_markets,
        max_pages=args.max_pages,
        order_size=args.order_size,
        edge_ticks=args.edge_ticks,
        min_spread_ticks=args.min_spread_ticks,
        band_min_margin_ticks=args.band_min_margin_ticks,
        band_avg_margin_ticks=args.band_avg_margin_ticks,
        band_max_margin_ticks=args.band_max_margin_ticks,
        band_min_size=args.band_min_size,
        band_avg_size=args.band_avg_size,
        band_max_size=args.band_max_size,
        min_price=args.min_price,
        max_price=args.max_price,
        max_collateral_per_market=args.max_collateral_per_market,
        max_loss_per_market=args.max_loss_per_market,
        max_inventory_per_token=args.max_inventory_per_token,
        max_inventory_per_market=args.max_inventory_per_market,
        max_total_collateral=args.max_total_collateral,
        min_free_collateral=args.min_free_collateral,
        max_book_spread_ticks=args.max_book_spread_ticks,
        max_pre_post_move_ticks=args.max_pre_post_move_ticks,
        max_open_orders_per_token=args.max_open_orders_per_token,
        min_top_depth=args.min_top_depth,
        quote_sides=QuoteSides(args.quote_sides),
        allow_single_sided=args.allow_single_sided,
        respect_reward_min_size=args.respect_reward_min_size,
        cancel_before_quote=args.cancel_before_quote,
        cancel_all=args.cancel_all,
        cancel_all_on_exit=args.cancel_all_on_exit,
        cancel_on_risk_breach=args.cancel_on_risk_breach,
        pause_on_risk_breach=args.pause_on_risk_breach,
        clear_pause=args.clear_pause,
        pause_path=args.pause_path,
        post_only=args.post_only,
        require_two_sided_live=args.require_two_sided_live,
        max_data_age_secs=args.max_data_age_secs,
        discover_only=args.discover_only,
        cycles=args.cycles,
        refresh_secs=args.refresh_secs,
        state_path=args.state_path,
        fill_state_path=args.fill_state_path,
        fill_max_records=args.fill_max_records,
        position_reconcile_tolerance=args.position_reconcile_tolerance,
    )
    validate_config(config, parser)
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py-market-maker",
        description="Simple Python market maker example for Kuest based prediction markets",
    )
    parser.add_argument(
        "--clob-host",
        default=_env_str("KUEST_CLOB_HOST", "https://clob.kuest.com"),
    )
    parser.add_argument(
        "--live",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_LIVE", False),
    )
    parser.add_argument("--private-key", default=_env_optional_str("KUEST_PRIVATE_KEY"))
    parser.add_argument(
        "--deposit-wallet",
        default=_env_optional_str("KUEST_DEPOSIT_WALLET"),
    )
    parser.add_argument("--chain-id", type=int, default=_env_optional_int("CHAIN_ID"))
    parser.add_argument(
        "--discovery",
        choices=[mode.value for mode in DiscoveryMode],
        default=_env_choice(
            "MARKET_MAKER_DISCOVERY",
            DiscoveryMode.AUTO.value,
            [mode.value for mode in DiscoveryMode],
        ),
    )
    parser.add_argument("--event-slug", default=_env_optional_str("MARKET_MAKER_EVENT_SLUG"))
    parser.add_argument(
        "--max-markets",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_MARKETS", 3),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_PAGES", 5),
    )
    parser.add_argument(
        "--order-size",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_ORDER_SIZE", Decimal("5")),
    )
    parser.add_argument(
        "--edge-ticks",
        type=int,
        default=_env_int("MARKET_MAKER_EDGE_TICKS", 1),
    )
    parser.add_argument(
        "--min-spread-ticks",
        type=int,
        default=_env_int("MARKET_MAKER_MIN_SPREAD_TICKS", 2),
    )
    parser.add_argument(
        "--band-min-margin-ticks",
        type=int,
        default=_env_optional_int("MARKET_MAKER_BAND_MIN_MARGIN_TICKS"),
    )
    parser.add_argument(
        "--band-avg-margin-ticks",
        type=int,
        default=_env_optional_int("MARKET_MAKER_BAND_AVG_MARGIN_TICKS"),
    )
    parser.add_argument(
        "--band-max-margin-ticks",
        type=int,
        default=_env_optional_int("MARKET_MAKER_BAND_MAX_MARGIN_TICKS"),
    )
    parser.add_argument(
        "--band-min-size",
        type=_parse_decimal,
        default=_env_optional_decimal("MARKET_MAKER_BAND_MIN_SIZE"),
    )
    parser.add_argument(
        "--band-avg-size",
        type=_parse_decimal,
        default=_env_optional_decimal("MARKET_MAKER_BAND_AVG_SIZE"),
    )
    parser.add_argument(
        "--band-max-size",
        type=_parse_decimal,
        default=_env_optional_decimal("MARKET_MAKER_BAND_MAX_SIZE"),
    )
    parser.add_argument(
        "--min-price",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MIN_PRICE", Decimal("0.05")),
    )
    parser.add_argument(
        "--max-price",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_PRICE", Decimal("0.95")),
    )
    parser.add_argument(
        "--max-collateral-per-market",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_COLLATERAL_PER_MARKET", Decimal("25")),
    )
    parser.add_argument(
        "--max-loss-per-market",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_LOSS_PER_MARKET", Decimal("25")),
    )
    parser.add_argument(
        "--max-inventory-per-token",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_INVENTORY_PER_TOKEN", Decimal("25")),
    )
    parser.add_argument(
        "--max-inventory-per-market",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_INVENTORY_PER_MARKET", Decimal("50")),
    )
    parser.add_argument(
        "--max-total-collateral",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MAX_TOTAL_COLLATERAL", Decimal("50")),
    )
    parser.add_argument(
        "--min-free-collateral",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MIN_FREE_COLLATERAL", Decimal("1")),
    )
    parser.add_argument(
        "--max-book-spread-ticks",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_BOOK_SPREAD_TICKS", 20),
    )
    parser.add_argument(
        "--max-pre-post-move-ticks",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_PRE_POST_MOVE_TICKS", 2),
    )
    parser.add_argument(
        "--max-open-orders-per-token",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_OPEN_ORDERS_PER_TOKEN", 2),
    )
    parser.add_argument(
        "--min-top-depth",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_MIN_TOP_DEPTH", Decimal("5")),
    )
    parser.add_argument(
        "--quote-sides",
        choices=[side.value for side in QuoteSides],
        default=_env_choice(
            "MARKET_MAKER_QUOTE_SIDES",
            QuoteSides.BUY.value,
            [side.value for side in QuoteSides],
        ),
    )
    parser.add_argument(
        "--allow-single-sided",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_ALLOW_SINGLE_SIDED", True),
    )
    parser.add_argument(
        "--respect-reward-min-size",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_RESPECT_REWARD_MIN_SIZE", False),
    )
    parser.add_argument(
        "--cancel-before-quote",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_CANCEL_BEFORE_QUOTE", True),
    )
    parser.add_argument(
        "--cancel-all",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_CANCEL_ALL", False),
    )
    parser.add_argument(
        "--cancel-all-on-exit",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_CANCEL_ALL_ON_EXIT", False),
    )
    parser.add_argument(
        "--cancel-on-risk-breach",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_CANCEL_ON_RISK_BREACH", False),
    )
    parser.add_argument(
        "--pause-on-risk-breach",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_PAUSE_ON_RISK_BREACH", False),
    )
    parser.add_argument(
        "--clear-pause",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_CLEAR_PAUSE", False),
    )
    parser.add_argument(
        "--pause-path",
        type=_parse_path,
        default=_parse_path(_env_str("MARKET_MAKER_PAUSE_PATH", "state/paused.json")),
    )
    parser.add_argument(
        "--post-only",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_POST_ONLY", True),
    )
    parser.add_argument(
        "--require-two-sided-live",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_REQUIRE_TWO_SIDED_LIVE", True),
    )
    parser.add_argument(
        "--max-data-age-secs",
        type=int,
        default=_env_int("MARKET_MAKER_MAX_DATA_AGE_SECS", 10),
    )
    parser.add_argument(
        "--discover-only",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MARKET_MAKER_DISCOVER_ONLY", False),
    )
    parser.add_argument("--cycles", type=int, default=_env_int("MARKET_MAKER_CYCLES", 1))
    parser.add_argument(
        "--refresh-secs",
        type=int,
        default=_env_int("MARKET_MAKER_REFRESH_SECS", 30),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(_env_str("MARKET_MAKER_STATE_PATH", "state/seen-markets.json")),
    )
    parser.add_argument(
        "--fill-state-path",
        type=_parse_path,
        default=_parse_path(_env_str("MARKET_MAKER_FILL_STATE_PATH", "state/fills.json")),
    )
    parser.add_argument(
        "--fill-max-records",
        type=int,
        default=_env_int("MARKET_MAKER_FILL_MAX_RECORDS", 10000),
    )
    parser.add_argument(
        "--position-reconcile-tolerance",
        type=_parse_decimal,
        default=_env_decimal("MARKET_MAKER_POSITION_RECONCILE_TOLERANCE", Decimal("0.000001")),
    )
    return parser


def validate_config(config: Config, parser: argparse.ArgumentParser) -> None:
    if config.clear_pause and (config.cancel_all or config.cancel_all_on_exit):
        parser.error("MARKET_MAKER_CLEAR_PAUSE cannot be combined with cancel-all actions")
    if config.clear_pause:
        return
    if config.fill_max_records <= 0:
        parser.error("MARKET_MAKER_FILL_MAX_RECORDS must be greater than zero")
    if config.position_reconcile_tolerance < Decimal("0"):
        parser.error("MARKET_MAKER_POSITION_RECONCILE_TOLERANCE cannot be negative")
    if config.max_markets <= 0:
        parser.error("MARKET_MAKER_MAX_MARKETS must be greater than zero")
    if config.max_pages <= 0:
        parser.error("MARKET_MAKER_MAX_PAGES must be greater than zero")
    if config.order_size <= Decimal("0"):
        parser.error("MARKET_MAKER_ORDER_SIZE must be greater than zero")
    if config.edge_ticks <= 0:
        parser.error("MARKET_MAKER_EDGE_TICKS must be greater than zero")
    if config.min_spread_ticks <= 0:
        parser.error("MARKET_MAKER_MIN_SPREAD_TICKS must be greater than zero")
    band_min_margin, band_avg_margin, band_max_margin = band_margin_ticks(config)
    if band_min_margin <= 0 or band_avg_margin <= 0 or band_max_margin <= 0:
        parser.error("MARKET_MAKER_BAND_*_MARGIN_TICKS must be greater than zero")
    if band_min_margin > band_avg_margin or band_avg_margin > band_max_margin:
        parser.error("MARKET_MAKER_BAND_*_MARGIN_TICKS must satisfy min <= avg <= max")
    if band_min_margin >= band_max_margin:
        parser.error("MARKET_MAKER_BAND_MAX_MARGIN_TICKS must be greater than MARKET_MAKER_BAND_MIN_MARGIN_TICKS")
    band_min_size, band_avg_size, band_max_size = band_sizes(config)
    if band_min_size < Decimal("0") or band_avg_size <= Decimal("0") or band_max_size <= Decimal("0"):
        parser.error("MARKET_MAKER_BAND_*_SIZE must be non-negative with avg and max greater than zero")
    if band_min_size > band_avg_size or band_avg_size > band_max_size:
        parser.error("MARKET_MAKER_BAND_*_SIZE must satisfy min <= avg <= max")
    if config.min_price <= Decimal("0") or config.min_price >= Decimal("1"):
        parser.error("MARKET_MAKER_MIN_PRICE must be between 0 and 1")
    if config.max_price <= Decimal("0") or config.max_price >= Decimal("1"):
        parser.error("MARKET_MAKER_MAX_PRICE must be between 0 and 1")
    if config.min_price >= config.max_price:
        parser.error("MARKET_MAKER_MIN_PRICE must be less than MARKET_MAKER_MAX_PRICE")
    if config.max_collateral_per_market <= Decimal("0"):
        parser.error("MARKET_MAKER_MAX_COLLATERAL_PER_MARKET must be greater than zero")
    if config.max_loss_per_market <= Decimal("0"):
        parser.error("MARKET_MAKER_MAX_LOSS_PER_MARKET must be greater than zero")
    if config.max_inventory_per_token <= Decimal("0"):
        parser.error("MARKET_MAKER_MAX_INVENTORY_PER_TOKEN must be greater than zero")
    if config.max_inventory_per_market <= Decimal("0"):
        parser.error("MARKET_MAKER_MAX_INVENTORY_PER_MARKET must be greater than zero")
    if config.max_total_collateral <= Decimal("0"):
        parser.error("MARKET_MAKER_MAX_TOTAL_COLLATERAL must be greater than zero")
    if config.min_free_collateral < Decimal("0"):
        parser.error("MARKET_MAKER_MIN_FREE_COLLATERAL cannot be negative")
    if config.max_book_spread_ticks <= 0:
        parser.error("MARKET_MAKER_MAX_BOOK_SPREAD_TICKS must be greater than zero")
    if config.max_pre_post_move_ticks <= 0:
        parser.error("MARKET_MAKER_MAX_PRE_POST_MOVE_TICKS must be greater than zero")
    if config.max_open_orders_per_token <= 0:
        parser.error("MARKET_MAKER_MAX_OPEN_ORDERS_PER_TOKEN must be greater than zero")
    if config.min_top_depth < Decimal("0"):
        parser.error("MARKET_MAKER_MIN_TOP_DEPTH cannot be negative")
    if config.event_slug is not None and not config.event_slug.strip():
        parser.error("MARKET_MAKER_EVENT_SLUG cannot be empty")
    if config.cycles <= 0:
        parser.error("MARKET_MAKER_CYCLES must be greater than zero")
    if config.cancel_all and config.cancel_all_on_exit:
        parser.error("MARKET_MAKER_CANCEL_ALL and MARKET_MAKER_CANCEL_ALL_ON_EXIT are mutually exclusive")
    if (config.cancel_all or config.cancel_all_on_exit) and not config.live:
        parser.error("MARKET_MAKER_CANCEL_ALL and MARKET_MAKER_CANCEL_ALL_ON_EXIT require --live")
    if config.cancel_on_risk_breach and not config.live:
        parser.error("MARKET_MAKER_CANCEL_ON_RISK_BREACH requires --live")
    if config.pause_on_risk_breach and not config.live:
        parser.error("MARKET_MAKER_PAUSE_ON_RISK_BREACH requires --live")
    if config.max_data_age_secs <= 0:
        parser.error("MARKET_MAKER_MAX_DATA_AGE_SECS must be greater than zero")
    if config.live:
        if not config.private_key:
            parser.error("--live requires KUEST_PRIVATE_KEY or --private-key")
        if not config.deposit_wallet:
            parser.error("--live requires KUEST_DEPOSIT_WALLET or --deposit-wallet")
        if config.chain_id is None:
            parser.error("--live requires CHAIN_ID or --chain-id; use 137 for Polygon Mainnet")
        if config.chain_id not in (POLYGON, AMOY):
            parser.error(f"unsupported chain id {config.chain_id}; SDK supports {POLYGON} and {AMOY}")


def band_margin_ticks(config: Config) -> tuple[int, int, int]:
    min_margin = config.band_min_margin_ticks if config.band_min_margin_ticks is not None else config.edge_ticks
    avg_margin = config.band_avg_margin_ticks if config.band_avg_margin_ticks is not None else min_margin
    max_margin = (
        config.band_max_margin_ticks
        if config.band_max_margin_ticks is not None
        else min_margin + config.min_spread_ticks
    )
    return min_margin, avg_margin, max_margin


def band_sizes(config: Config) -> tuple[Decimal, Decimal, Decimal]:
    min_size = config.band_min_size if config.band_min_size is not None else config.order_size
    avg_size = config.band_avg_size if config.band_avg_size is not None else max(config.order_size, min_size)
    max_size = config.band_max_size if config.band_max_size is not None else max(avg_size, min_size)
    return min_size, avg_size, max_size


def _env_optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_str(name: str, default: str) -> str:
    return _env_optional_str(name) or default


def _env_optional_int(name: str) -> int | None:
    value = _env_optional_str(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _env_int(name: str, default: int) -> int:
    value = _env_optional_int(name)
    return default if value is None else value


def _env_decimal(name: str, default: Decimal) -> Decimal:
    value = _env_optional_str(name)
    if value is None:
        return default
    return _parse_decimal(value)


def _env_optional_decimal(name: str) -> Decimal | None:
    value = _env_optional_str(name)
    if value is None:
        return None
    return _parse_decimal(value)


def _env_bool(name: str, default: bool) -> bool:
    value = _env_optional_str(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def _env_choice(name: str, default: str, choices: list[str]) -> str:
    value = _env_optional_str(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}")
    return normalized


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"{value} is not a decimal") from error
    if not parsed.is_finite():
        raise argparse.ArgumentTypeError(f"{value} must be a finite decimal")
    return parsed


def _parse_path(value: str) -> Path:
    if value == "":
        raise argparse.ArgumentTypeError("path cannot be empty")
    return Path(value)
