from __future__ import annotations

import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable

from .sdk_adapter import (
    AssetType,
    BalanceAllowanceParams,
    BUY,
    ClobClient,
    END_CURSOR,
    INITIAL_CURSOR,
    OpenOrderParams,
    OrderArgs,
    OrderBookSummary,
    OrderSummary,
    SELL,
    TradeParams,
)

from .config import Config, DiscoveryMode, band_margin_ticks, band_sizes
from .event_scope import condition_id_from_market, condition_ids_from_site_config
from .market_loss import MarketExposure, OutcomeExposure, ProposedOrder
from .pricing import ceil_to_tick, fair_price, floor_to_tick, is_tradeable_price
from .state import FillLedger, FillRecord, PauseState, SeenMarkets

CONDITIONAL_TOKEN_BASE_UNITS = Decimal("1000000")
CANCEL_ORDER_BATCH_SIZE = 50
CANCEL_VERIFY_ATTEMPTS = 5
CANCEL_VERIFY_DELAY_SECS = 2


@dataclass(frozen=True)
class MarketCandidate:
    market: dict[str, Any]
    is_new: bool


@dataclass
class CancelOpenOrdersSummary:
    markets_checked: int = 0
    tokens_checked: int = 0
    orders_found: int = 0
    canceled: int = 0
    not_canceled: int = 0
    remaining_open: int = 0


@dataclass(frozen=True)
class QuotePlan:
    market_key: str
    market_slug: str
    question: str
    token_id: str
    outcome: str
    fair_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    book_fetched_at: float
    buy_band: QuoteBand | None
    sell_band: QuoteBand | None

    def bands(self) -> list["QuoteBand"]:
        return [band for band in (self.buy_band, self.sell_band) if band is not None]


@dataclass(frozen=True)
class QuoteBand:
    side: str
    price: Decimal
    min_price: Decimal
    max_price: Decimal
    minimum_size: Decimal
    min_size: Decimal
    avg_size: Decimal
    max_size: Decimal

    def contains_price(self, price: Decimal) -> bool:
        return self.min_price <= price <= self.max_price

    def includes_order(self, order: Any) -> bool:
        side = _response_field(order, "side")
        price = _decimal_or_none(_response_field(order, "price"))
        return side == self.side and price is not None and self.contains_price(price)

    def cancel_priority(self, order: Any) -> Decimal:
        price = _decimal(_response_field(order, "price"), Decimal("0"))
        if self.side == BUY:
            return max(self.max_price - price, Decimal("0"))
        if self.side == SELL:
            return max(price - self.min_price, Decimal("0"))
        return Decimal("0")


@dataclass(frozen=True)
class TokenQuote:
    token_id: str
    outcome: str
    fair_price: Decimal
    book_fetched_at: float
    plan: QuotePlan | None
    skip_reason: str | None


class PreflightRiskAuditResult(str, Enum):
    CONTINUE = "continue"
    SKIP_CYCLE = "skip_cycle"
    STOP = "stop"


@dataclass(frozen=True)
class PreflightMarketSnapshot:
    market_key: str
    token_quotes: list[TokenQuote]
    market_state: "LiveMarketState"


@dataclass(frozen=True)
class PreflightRiskAudit:
    result: PreflightRiskAuditResult
    snapshots: list[PreflightMarketSnapshot]
    risk_budget: "RiskBudget | None" = None


@dataclass(frozen=True)
class QuoteInputs:
    fair_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(frozen=True)
class SubmittedOrder:
    side: str
    proposed_order: ProposedOrder
    order: Any
    collateral: Decimal = Decimal("0")


@dataclass(frozen=True)
class InventoryBuyRoom:
    token_position: Decimal
    market_inventory: Decimal
    room: Decimal


@dataclass
class RiskBudget:
    remaining_collateral: Decimal
    counted_open_buy_orders: dict[str, Decimal]

    @classmethod
    def new(cls, collateral_limit: Decimal) -> "RiskBudget":
        return cls(
            remaining_collateral=max(collateral_limit, Decimal("0")),
            counted_open_buy_orders={},
        )

    def remaining(self) -> Decimal:
        return max(self.remaining_collateral, Decimal("0"))

    def reserved_collateral(self) -> Decimal:
        return sum(self.counted_open_buy_orders.values(), Decimal("0"))

    def reserve_open_buy_order(self, order: Any) -> None:
        if _response_field(order, "side") != BUY:
            return
        order_id = open_order_id(order)
        if not order_id or order_id in self.counted_open_buy_orders:
            return
        collateral = open_order_remaining_size(order) * _decimal(_response_field(order, "price"), Decimal("0"))
        self.counted_open_buy_orders[order_id] = collateral
        self.remaining_collateral = max(self.remaining_collateral - collateral, Decimal("0"))

    def release_open_buy_order(self, order: Any) -> None:
        if _response_field(order, "side") != BUY:
            return
        order_id = open_order_id(order)
        collateral = self.counted_open_buy_orders.pop(order_id, None)
        if collateral is not None:
            self.remaining_collateral += collateral

    def reserve_new_collateral(self, collateral: Decimal) -> Decimal:
        reserved = min(max(collateral, Decimal("0")), self.remaining())
        self.remaining_collateral = max(self.remaining_collateral - reserved, Decimal("0"))
        return reserved


@dataclass(frozen=True)
class RiskBreach:
    kind: str
    value: Decimal
    limit: Decimal
    token_id: str | None = None

    def message(self) -> str:
        if self.kind == "token_inventory":
            return f"token {self.token_id} inventory {self.value} exceeds limit {self.limit}"
        if self.kind == "market_inventory":
            return f"market inventory {self.value} exceeds limit {self.limit}"
        if self.kind == "market_loss":
            return f"market loss {self.value} exceeds limit {self.limit}"
        if self.kind == "market_collateral":
            return f"market buy collateral {self.value} exceeds limit {self.limit}"
        return f"{self.kind} {self.value} exceeds limit {self.limit}"


class LiquidityRejectKind(str, Enum):
    MISSING_TWO_SIDED_BOOK = "missing_two_sided_book"
    INVALID_TICK = "invalid_tick"
    SPREAD_TOO_WIDE = "spread_too_wide"
    BID_DEPTH_TOO_LOW = "bid_depth_too_low"
    ASK_DEPTH_TOO_LOW = "ask_depth_too_low"


@dataclass(frozen=True)
class LiquidityRejectReason:
    kind: LiquidityRejectKind
    spread_ticks: Decimal | None = None
    max_spread_ticks: int | None = None
    depth: Decimal | None = None
    min_depth: Decimal | None = None

    def message(self) -> str:
        if self.kind == LiquidityRejectKind.MISSING_TWO_SIDED_BOOK:
            return "missing a valid two-sided book"
        if self.kind == LiquidityRejectKind.INVALID_TICK:
            return "book tick size is invalid"
        if self.kind == LiquidityRejectKind.SPREAD_TOO_WIDE:
            return f"spread is {self.spread_ticks} ticks above max {self.max_spread_ticks}"
        if self.kind == LiquidityRejectKind.BID_DEPTH_TOO_LOW:
            return f"best bid depth {self.depth} below minimum {self.min_depth}"
        if self.kind == LiquidityRejectKind.ASK_DEPTH_TOO_LOW:
            return f"best ask depth {self.depth} below minimum {self.min_depth}"
        return self.kind.value


@dataclass
class LiveTokenState:
    token_id: str
    fair_price: Decimal
    balance: Decimal
    balance_fetched_at: float
    open_orders: list[Any]
    open_orders_fetched_at: float
    cost_basis: Decimal | None = None
    position_reconcile_error: "PositionReconcileError | None" = None


@dataclass(frozen=True)
class PositionReconcileError:
    live_balance: Decimal
    ledger_position: Decimal
    difference: Decimal
    tolerance: Decimal


@dataclass
class LiveMarketState:
    tokens: list[LiveTokenState]
    pending_orders: list[ProposedOrder]

    @classmethod
    def load(
        cls,
        client: ClobClient,
        token_quotes: list[TokenQuote],
        config: Config | None = None,
    ) -> "LiveMarketState":
        fill_ledger = FillLedger.load(config.fill_state_path) if config is not None else FillLedger()
        fill_ledger_changed = False
        tokens = []
        for token_quote in token_quotes:
            if config is not None:
                token_id = token_quote.token_id
                after_unix_secs = fill_ledger.latest_matched_at_unix_secs(token_id)
                if after_unix_secs is not None:
                    after_unix_secs = max(after_unix_secs - 1, 0)
                for trade in trades_for_token(client, token_id, after_unix_secs):
                    record = fill_record_from_trade(trade)
                    if fill_ledger.upsert(record):
                        fill_ledger_changed = True

            balance = conditional_balance(client, token_quote.token_id)
            balance_fetched_at = time.monotonic()
            fill_records = fill_ledger.records_for_token(token_quote.token_id)
            ledger_position = token_ledger_position(fill_records)
            position_reconcile_error = (
                position_reconcile_error_for(
                    balance,
                    ledger_position,
                    config.position_reconcile_tolerance,
                )
                if config is not None
                else None
            )
            cost_basis = token_cost_basis(fill_records, balance, token_quote.fair_price)
            open_orders = open_orders_for_token(client, token_quote.token_id)
            open_orders_fetched_at = time.monotonic()
            tokens.append(
                LiveTokenState(
                    token_id=token_quote.token_id,
                    fair_price=token_quote.fair_price,
                    balance=balance,
                    balance_fetched_at=balance_fetched_at,
                    open_orders=open_orders,
                    open_orders_fetched_at=open_orders_fetched_at,
                    cost_basis=cost_basis,
                    position_reconcile_error=position_reconcile_error,
                )
            )
        if config is not None:
            if fill_ledger.prune_to_max_records(config.fill_max_records):
                fill_ledger_changed = True
            if fill_ledger_changed:
                fill_ledger.save(config.fill_state_path)
        return cls(tokens=tokens, pending_orders=[])

    def exposure(self) -> MarketExposure:
        exposure = MarketExposure(
            outcomes=[
                OutcomeExposure(
                    token_id=token.token_id,
                    position=token.balance,
                    cost=token.cost_basis if token.cost_basis is not None else token.balance * token.fair_price,
                )
                for token in self.tokens
            ]
        )
        for token in self.tokens:
            for order in token.open_orders:
                proposed = proposed_order_from_open_order(order, token.token_id)
                if proposed is not None:
                    exposure.apply_order(proposed)
        for order in self.pending_orders:
            exposure.apply_order(order)
        return exposure

    def record_pending_order(self, order: ProposedOrder) -> None:
        self.pending_orders.append(order)

    def position_reconcile_reject_reason(self) -> str | None:
        for token_state in self.tokens:
            error = token_state.position_reconcile_error
            if error is None:
                continue
            return (
                f"token {token_state.token_id} live balance {error.live_balance} "
                f"differs from fill-ledger position {error.ledger_position} "
                f"by {error.difference} (tolerance {error.tolerance})"
            )
        return None

    def open_orders(self, token_id: str) -> list[Any]:
        token = self.token_state(token_id)
        return list(token.open_orders) if token is not None else []

    def token_state(self, token_id: str) -> LiveTokenState | None:
        for token in self.tokens:
            if token.token_id == token_id:
                return token
        return None

    def remove_open_orders(self, token_id: str, order_ids: set[str]) -> None:
        if not order_ids:
            return
        for token in self.tokens:
            if token.token_id == token_id:
                token.open_orders = [
                    order for order in token.open_orders if open_order_id(order) not in order_ids
                ]
                return

    def replace_open_orders(
        self,
        token_id: str,
        open_orders: list[Any],
        fetched_at: float | None = None,
    ) -> None:
        for token in self.tokens:
            if token.token_id == token_id:
                token.open_orders = list(open_orders)
                token.open_orders_fetched_at = time.monotonic() if fetched_at is None else fetched_at
                return

    def remove_pending_orders_now_open(self, refreshed_open_orders: list[Any]) -> None:
        if not self.pending_orders:
            return
        self.pending_orders = [
            pending_order
            for pending_order in self.pending_orders
            if not any(
                open_order_matches_proposed(open_order, pending_order)
                for open_order in refreshed_open_orders
            )
        ]

    def inventory_buy_room(
        self,
        token_id: str,
        staged_orders: list[ProposedOrder],
        config: Config,
    ) -> InventoryBuyRoom:
        token_position = self.long_inventory_for_token(token_id, staged_orders)
        market_inventory = self.long_market_inventory(staged_orders)
        token_room = max(config.max_inventory_per_token - token_position, Decimal("0"))
        market_room = max(config.max_inventory_per_market - market_inventory, Decimal("0"))
        return InventoryBuyRoom(
            token_position=token_position,
            market_inventory=market_inventory,
            room=min(token_room, market_room),
        )

    def long_inventory_for_token(
        self,
        token_id: str,
        staged_orders: list[ProposedOrder],
    ) -> Decimal:
        token = self.token_state(token_id)
        if token is None:
            return Decimal("0")
        return token_long_inventory(
            token.balance,
            token.open_orders,
            self.pending_orders,
            staged_orders,
            token_id,
        )

    def long_market_inventory(self, staged_orders: list[ProposedOrder]) -> Decimal:
        return sum(
            (
                token_long_inventory(
                    token.balance,
                    token.open_orders,
                    self.pending_orders,
                    staged_orders,
                    token.token_id,
                )
                for token in self.tokens
            ),
            Decimal("0"),
        )

    def risk_breaches(self, config: Config) -> list[RiskBreach]:
        breaches: list[RiskBreach] = []
        for token in self.tokens:
            inventory = self.long_inventory_for_token(token.token_id, [])
            if inventory > config.max_inventory_per_token:
                breaches.append(
                    RiskBreach(
                        kind="token_inventory",
                        token_id=token.token_id,
                        value=inventory,
                        limit=config.max_inventory_per_token,
                    )
                )

        market_inventory = self.long_market_inventory([])
        if market_inventory > config.max_inventory_per_market:
            breaches.append(
                RiskBreach(
                    kind="market_inventory",
                    value=market_inventory,
                    limit=config.max_inventory_per_market,
                )
            )

        loss = self.exposure().worst_loss()
        if loss > config.max_loss_per_market:
            breaches.append(
                RiskBreach(
                    kind="market_loss",
                    value=loss,
                    limit=config.max_loss_per_market,
                )
            )

        collateral = self.exposure().buy_collateral()
        if collateral > config.max_collateral_per_market:
            breaches.append(
                RiskBreach(
                    kind="market_collateral",
                    value=collateral,
                    limit=config.max_collateral_per_market,
                )
            )

        return breaches


class ShutdownRequested(Exception):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"received signal {signum}")


def run(config: Config) -> None:
    if config.clear_pause:
        cleared = PauseState.clear(config.pause_path)
        if cleared:
            print(f"cleared pause file {config.pause_path}")
        else:
            print(f"no pause file at {config.pause_path}")
        return

    public_client = ClobClient.public(config)
    live_client = authenticate(config) if config.live else None

    if config.cancel_all:
        cancel_scope_orders(public_client, require_live_client(live_client), config)
        return

    if config.cancel_all_on_exit:
        run_with_cancel_on_exit(public_client, require_live_client(live_client), config)
        return

    run_cycles(public_client, live_client, config)


def run_with_cancel_on_exit(public_client: ClobClient, live_client: ClobClient, config: Config) -> None:
    managed_scope: list[dict[str, Any]] = []
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_shutdown(signum: int, _frame: Any) -> None:
        raise ShutdownRequested(signum)

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        run_cycles(public_client, live_client, config, managed_scope)
    except ShutdownRequested as error:
        print(f"shutdown requested: {error}; canceling scoped open orders")
        cancel_scope_orders(public_client, live_client, config, managed_scope)
        raise SystemExit(128 + error.signum) from error
    except KeyboardInterrupt:
        print("shutdown requested: keyboard interrupt; canceling scoped open orders")
        cancel_scope_orders(public_client, live_client, config, managed_scope)
        raise
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def require_live_client(live_client: ClobClient | None) -> ClobClient:
    if live_client is None:
        raise RuntimeError("cancel-all requires a live authenticated client")
    return live_client


def run_cycles(
    public_client: ClobClient,
    live_client: ClobClient | None,
    config: Config,
    managed_scope: list[dict[str, Any]] | None = None,
) -> None:
    seen = SeenMarkets.load(config.state_path)

    for cycle in range(1, config.cycles + 1):
        if stop_if_paused(config):
            return

        candidates = discover_cycle_candidates(public_client, config, seen, cycle)
        if managed_scope is not None:
            replace_managed_scope(managed_scope, candidates)

        for candidate in candidates:
            marker = "new" if candidate.is_new else "seen"
            print(f"- [{marker}] {market_key(candidate.market)} :: {_market_question(candidate.market)}")

        if config.discover_only:
            continue

        preflight_snapshots: deque[PreflightMarketSnapshot] = deque()
        global_budget = RiskBudget.new(config.max_total_collateral)
        if live_client is not None:
            preflight = preflight_risk_audit(
                public_client,
                live_client,
                [candidate.market for candidate in candidates],
                config,
            )
            if preflight.result == PreflightRiskAuditResult.SKIP_CYCLE:
                if cycle < config.cycles:
                    time.sleep(config.refresh_secs)
                continue
            if preflight.result == PreflightRiskAuditResult.STOP:
                return
            preflight_snapshots = deque(preflight.snapshots)
            global_budget = preflight.risk_budget or global_budget

        for candidate in candidates:
            preflight_snapshot = None
            if live_client is not None:
                if not preflight_snapshots:
                    raise RuntimeError("missing preflight snapshot for live quote")
                preflight_snapshot = preflight_snapshots.popleft()
            quote_market(public_client, live_client, candidate.market, config, preflight_snapshot, global_budget)
            if stop_if_paused(config):
                return

        if cycle < config.cycles:
            time.sleep(config.refresh_secs)


def stop_if_paused(config: Config) -> bool:
    pause = PauseState.load(config.pause_path)
    if pause is None:
        return False
    print(f"paused by {config.pause_path}: {pause.reason.strip()}")
    return True


def discover_cycle_candidates(
    public_client: ClobClient,
    config: Config,
    seen: SeenMarkets,
    cycle: int,
) -> list[MarketCandidate]:
    event_slug = config.event_slug.strip() if config.event_slug else None
    if event_slug:
        print(f"cycle {cycle}/{config.cycles}: discovering markets for event {event_slug}")
        markets = discover_event_markets(public_client, event_slug, config.max_pages)
        candidates = select_event_candidates(markets, config.max_markets)
        print(f"event {event_slug}: found {len(candidates)} tradable markets")
        return candidates

    print(f"cycle {cycle}/{config.cycles}: discovering markets")
    markets = discover_markets(public_client, config.discovery, config.max_pages)
    candidates = select_candidates(markets, seen, config.max_markets)
    seen.save(config.state_path)
    new_count = sum(1 for candidate in candidates if candidate.is_new)
    print(f"found {len(candidates)} tradable fork-scoped markets ({new_count} new)")
    return candidates


def replace_managed_scope(managed_scope: list[dict[str, Any]], candidates: list[MarketCandidate]) -> None:
    if not candidates:
        return
    managed_scope[:] = [candidate.market for candidate in candidates]


def authenticate(config: Config) -> ClobClient:
    return ClobClient.authenticated(config)


def discover_markets(client: ClobClient, mode: DiscoveryMode, max_pages: int) -> list[dict[str, Any]]:
    if mode == DiscoveryMode.AUTO:
        sampling = fetch_market_pages(client, DiscoveryMode.SAMPLING, max_pages)
        return sampling if sampling else fetch_market_pages(client, DiscoveryMode.SITE, max_pages)

    return fetch_market_pages(client, mode, max_pages)


def discover_event_markets(client: ClobClient, event_slug: str, max_pages: int) -> list[dict[str, Any]]:
    condition_ids = condition_ids_from_site_config(event_slug, max_pages)
    markets: list[dict[str, Any]] = []
    for condition_id in sorted(condition_ids):
        market = client.get_market(condition_id)
        if isinstance(market, dict):
            markets.append(market)
    return markets


def fetch_market_pages(client: ClobClient, mode: DiscoveryMode, max_pages: int) -> list[dict[str, Any]]:
    cursor = INITIAL_CURSOR
    markets: list[dict[str, Any]] = []

    for _ in range(max_pages):
        previous_cursor = cursor
        page = (
            client.get_sampling_markets(cursor)
            if mode == DiscoveryMode.SAMPLING
            else client.get_markets(cursor)
        )
        if not isinstance(page, dict):
            break

        data = page.get("data")
        if isinstance(data, list):
            markets.extend(market for market in data if isinstance(market, dict))

        next_cursor = str(page.get("next_cursor") or END_CURSOR)
        if next_cursor == END_CURSOR or next_cursor == previous_cursor:
            break
        cursor = next_cursor

    return markets


def select_candidates(
    markets: list[dict[str, Any]],
    seen: SeenMarkets,
    max_markets: int,
) -> list[MarketCandidate]:
    return _select_candidates(
        markets,
        max_markets,
        is_new=lambda market: seen.mark_new(market_key(market)),
    )


def select_event_candidates(
    markets: list[dict[str, Any]],
    max_markets: int,
) -> list[MarketCandidate]:
    return _select_candidates(markets, max_markets, is_new=lambda _: False)


def _select_candidates(
    markets: list[dict[str, Any]],
    max_markets: int,
    is_new: Callable[[dict[str, Any]], bool],
) -> list[MarketCandidate]:
    candidates = [
        MarketCandidate(market=market, is_new=is_new(market))
        for market in markets
        if is_tradable_market(market)
    ]
    candidates.sort(
        key=lambda candidate: (
            not candidate.is_new,
            not has_rewards(candidate.market),
            -_timestamp_sort_value(_field(candidate.market, "accepting_order_timestamp")),
            _market_slug(candidate.market),
        )
    )
    return candidates[:max_markets]


def is_tradable_market(market: dict[str, Any]) -> bool:
    return (
        _bool_field(market, "enable_order_book")
        and _bool_field(market, "active")
        and not _bool_field(market, "closed")
        and not _bool_field(market, "archived")
        and _bool_field(market, "accepting_orders")
        and bool(_market_tokens(market))
    )


def has_rewards(market: dict[str, Any]) -> bool:
    rewards = _dict_field(market, "rewards")
    rates = rewards.get("rates")
    min_size = _decimal(rewards.get("min_size"), Decimal("0"))
    return (isinstance(rates, list) and bool(rates)) or min_size > Decimal("0")


def market_key(market: dict[str, Any]) -> str:
    condition_id = condition_id_from_market(market)
    if condition_id:
        return condition_id
    return _market_slug(market)


def quote_market(
    public_client: ClobClient,
    live_client: ClobClient | None,
    market: dict[str, Any],
    config: Config,
    preflight_snapshot: PreflightMarketSnapshot | None = None,
    global_budget: RiskBudget | None = None,
) -> None:
    if global_budget is None:
        global_budget = RiskBudget.new(config.max_total_collateral)
    market_budget = RiskBudget.new(config.max_collateral_per_market)
    snapshot = preflight_snapshot_for_market(preflight_snapshot, market_key(market))
    if snapshot is not None:
        token_quotes = snapshot.token_quotes
        preflight_market_state = snapshot.market_state
    else:
        token_quotes = build_market_token_quotes(public_client, market, config)
        preflight_market_state = None

    for token_quote in token_quotes:
        if token_quote.plan is None:
            reason = token_quote.skip_reason or "no safe quote at configured edge/sides"
            print(f"skip {_market_slug(market)} {token_quote.outcome}: {reason}")
            continue

        print_plan(token_quote.plan, config.live)

    if live_client is None:
        return

    market_state = (
        preflight_market_state
        if preflight_market_state is not None
        else LiveMarketState.load(live_client, token_quotes, config)
    )
    stale_reason = preflight_stale_data_reason(token_quotes, market_state, time.monotonic(), config)
    if stale_reason is not None:
        print(f"skip live quote {_market_slug(market)}: stale live data ({stale_reason})")
        return
    position_reason = market_state.position_reconcile_reject_reason()
    if position_reason is not None:
        print(f"skip live quote {_market_slug(market)}: position reconciliation failed ({position_reason})")
        return

    for token_quote in token_quotes:
        if stop_if_paused(config):
            return
        if token_quote.plan is not None:
            post_quote_plan(
                live_client,
                token_quote.plan,
                config,
                market_state,
                public_client,
                global_budget,
                market_budget,
            )
            if stop_if_paused(config):
                return


def preflight_risk_audit(
    public_client: ClobClient,
    live_client: ClobClient,
    markets: list[dict[str, Any]],
    config: Config,
) -> PreflightRiskAudit:
    if not markets:
        return PreflightRiskAudit(
            PreflightRiskAuditResult.CONTINUE,
            [],
            RiskBudget.new(config.max_total_collateral),
        )

    print(f"preflight risk audit: checking {len(markets)} markets")
    snapshots: list[PreflightMarketSnapshot] = []
    global_budget = RiskBudget.new(config.max_total_collateral)
    scanned_markets: list[tuple[dict[str, Any], LiveMarketState]] = []
    for market in markets:
        pause = PauseState.load(config.pause_path)
        if pause is not None:
            print(f"preflight risk audit stopped by {config.pause_path}: {pause.reason.strip()}")
            return PreflightRiskAudit(PreflightRiskAuditResult.STOP, snapshots, global_budget)

        try:
            token_quotes = build_market_token_quotes(public_client, market, config)
        except Exception as error:
            print(f"preflight risk audit skip {_market_slug(market)}: failed to fetch order books ({error})")
            return PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, snapshots, global_budget)

        try:
            market_state = LiveMarketState.load(live_client, token_quotes, config)
        except Exception as error:
            print(f"preflight risk audit skip {_market_slug(market)}: failed to fetch live state ({error})")
            return PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, snapshots, global_budget)

        stale_reason = preflight_stale_data_reason(token_quotes, market_state, time.monotonic(), config)
        if stale_reason is not None:
            print(f"preflight risk audit skip {_market_slug(market)}: stale live data ({stale_reason})")
            return PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, snapshots, global_budget)

        breaches = market_state.risk_breaches(config)
        if breaches:
            print_preflight_risk_breaches(market, breaches)
            if config.cancel_on_risk_breach:
                cancel_risk_increasing_market_orders(live_client, market, config, market_state)
            if config.pause_on_risk_breach:
                reason = preflight_breach_pause_reason(market, breaches)
                PauseState.save_reason(config.pause_path, reason)
                print(f"wrote pause file {config.pause_path}: {reason}")
                return PreflightRiskAudit(PreflightRiskAuditResult.STOP, snapshots, global_budget)
            return PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, snapshots, global_budget)

        position_reason = market_state.position_reconcile_reject_reason()
        if position_reason is not None:
            print(
                f"preflight risk audit skip {_market_slug(market)}: "
                f"position reconciliation failed ({position_reason})"
            )
            return PreflightRiskAudit(PreflightRiskAuditResult.SKIP_CYCLE, snapshots, global_budget)

        scanned_markets.append((market, market_state))
        for token_state in market_state.tokens:
            for order in token_state.open_orders:
                global_budget.reserve_open_buy_order(order)

        snapshots.append(
            PreflightMarketSnapshot(
                market_key=market_key(market),
                token_quotes=token_quotes,
                market_state=market_state,
            )
        )

    used_collateral = global_budget.reserved_collateral()
    if used_collateral > config.max_total_collateral:
        print(
            f"preflight risk breach: total open buy collateral {used_collateral} "
            f"exceeds limit {config.max_total_collateral}"
        )
        if config.cancel_on_risk_breach:
            for market, market_state in scanned_markets:
                cancel_risk_increasing_market_orders(live_client, market, config, market_state)
        if config.pause_on_risk_breach:
            reason = (
                f"preflight risk breach: total open buy collateral {used_collateral} "
                f"exceeds limit {config.max_total_collateral}"
            )
            PauseState.save_reason(config.pause_path, reason)
            print(f"wrote pause file {config.pause_path}: {reason}")
        return PreflightRiskAudit(PreflightRiskAuditResult.STOP, snapshots, global_budget)

    return PreflightRiskAudit(PreflightRiskAuditResult.CONTINUE, snapshots, global_budget)


def preflight_snapshot_for_market(
    snapshot: PreflightMarketSnapshot | None,
    expected_market_key: str,
) -> PreflightMarketSnapshot | None:
    if snapshot is None:
        return None
    if snapshot.market_key != expected_market_key:
        raise RuntimeError(
            f"preflight snapshot market mismatch: expected {expected_market_key}, got {snapshot.market_key}"
        )
    return snapshot


def build_market_token_quotes(
    public_client: ClobClient,
    market: dict[str, Any],
    config: Config,
) -> list[TokenQuote]:
    token_quotes: list[TokenQuote] = []
    for token in _market_tokens(market):
        token_id = _token_id(token)
        if not token_id:
            continue

        book = public_client.get_order_book(token_id)
        book_fetched_at = time.monotonic()
        token_quotes.append(build_token_quote(market, token, book, book_fetched_at, config))

    return token_quotes


def build_token_quote(
    market: dict[str, Any],
    token: dict[str, Any],
    book: OrderBookSummary,
    book_fetched_at: float,
    config: Config,
) -> TokenQuote:
    quote_inputs = build_quote_inputs(token, book)
    liquidity_skip = None
    if should_enforce_liquidity_quality(config):
        reject_reason = liquidity_quality_reject_reason(book, config)
        if reject_reason is not None:
            liquidity_skip = f"liquidity quality check failed: {reject_reason.message()}"
    plan = None if liquidity_skip else build_quote_plan(market, token, book, book_fetched_at, quote_inputs, config)
    return TokenQuote(
        token_id=_token_id(token),
        outcome=_token_outcome(token),
        fair_price=quote_inputs.fair_price,
        book_fetched_at=book_fetched_at,
        plan=plan,
        skip_reason=liquidity_skip if plan is None else None,
    )


def build_quote_inputs(token: dict[str, Any], book: OrderBookSummary) -> QuoteInputs:
    best_bid_price = best_bid(book.bids or [])
    best_ask_price = best_ask(book.asks or [])
    return QuoteInputs(
        fair_price=fair_price(
            best_bid_price,
            best_ask_price,
            _decimal(_field(token, "price", "p"), Decimal("0")),
            _decimal_or_none(book.last_trade_price),
        ),
        best_bid=best_bid_price,
        best_ask=best_ask_price,
    )


def build_quote_plan(
    market: dict[str, Any],
    token: dict[str, Any],
    book: OrderBookSummary,
    book_fetched_at: float,
    quote_inputs: QuoteInputs,
    config: Config,
) -> QuotePlan | None:
    tick = _decimal(book.tick_size, Decimal("0.01"))
    buy_band = build_quote_band(market, BUY, quote_inputs, tick, config) if config.quote_sides.includes_buy() else None
    sell_band = build_quote_band(market, SELL, quote_inputs, tick, config) if config.quote_sides.includes_sell() else None

    if not config.allow_single_sided and (buy_band is None or sell_band is None):
        return None

    if buy_band is not None and sell_band is not None:
        tick_spread = tick * Decimal(config.min_spread_ticks)
        if sell_band.price - buy_band.price < tick_spread:
            buy_band = None
            sell_band = None

    if buy_band is None and sell_band is None:
        return None

    return QuotePlan(
        market_key=market_key(market),
        market_slug=_market_slug(market),
        question=_market_question(market),
        token_id=_token_id(token),
        outcome=_token_outcome(token),
        fair_price=quote_inputs.fair_price,
        best_bid=quote_inputs.best_bid,
        best_ask=quote_inputs.best_ask,
        book_fetched_at=book_fetched_at,
        buy_band=buy_band,
        sell_band=sell_band,
    )


def build_quote_band(
    market: dict[str, Any],
    side: str,
    quote_inputs: QuoteInputs,
    tick: Decimal,
    config: Config,
) -> QuoteBand | None:
    if tick <= Decimal("0"):
        return None

    min_margin_ticks, avg_margin_ticks, max_margin_ticks = band_margin_ticks(config)
    min_size, avg_size, max_size = band_sizes(config)
    min_margin = tick * Decimal(min_margin_ticks)
    avg_margin = tick * Decimal(avg_margin_ticks)
    max_margin = tick * Decimal(max_margin_ticks)

    if side == BUY:
        price = floor_to_tick(quote_inputs.fair_price - avg_margin, tick)
        min_price = floor_to_tick(quote_inputs.fair_price - max_margin, tick)
        max_price = floor_to_tick(quote_inputs.fair_price - min_margin, tick)
        if quote_inputs.best_ask is not None and price >= quote_inputs.best_ask:
            return None
    elif side == SELL:
        price = ceil_to_tick(quote_inputs.fair_price + avg_margin, tick)
        min_price = ceil_to_tick(quote_inputs.fair_price + min_margin, tick)
        max_price = ceil_to_tick(quote_inputs.fair_price + max_margin, tick)
        if quote_inputs.best_bid is not None and price <= quote_inputs.best_bid:
            return None
    else:
        return None

    min_price = max(min_price, tick, config.min_price)
    max_price = min(max_price, Decimal("1") - tick, config.max_price)
    if not is_tradeable_price(price, tick) or not (config.min_price <= price <= config.max_price):
        return None
    if min_price > max_price:
        return None

    return QuoteBand(
        side=side,
        price=price,
        min_price=min_price,
        max_price=max_price,
        minimum_size=order_size(market, Decimal("0"), config),
        min_size=order_size(market, min_size, config),
        avg_size=order_size(market, avg_size, config),
        max_size=order_size(market, max_size, config),
    )


def order_size(market: dict[str, Any], requested_size: Decimal, config: Config) -> Decimal:
    size = max(requested_size, _decimal(_field(market, "minimum_order_size"), Decimal("0")))
    if config.respect_reward_min_size:
        size = max(size, _decimal(_dict_field(market, "rewards").get("min_size"), Decimal("0")))
    return size


def should_enforce_liquidity_quality(config: Config) -> bool:
    return config.live and config.require_two_sided_live


def liquidity_quality_reject_reason(
    book: OrderBookSummary,
    config: Config,
) -> LiquidityRejectReason | None:
    return liquidity_reject_reason(
        book.bids or [],
        book.asks or [],
        _decimal(book.tick_size, Decimal("0")),
        config.max_book_spread_ticks,
        config.min_top_depth,
    )


def liquidity_reject_reason(
    bids: list[OrderSummary],
    asks: list[OrderSummary],
    tick: Decimal,
    max_spread_ticks: int,
    min_top_depth: Decimal,
) -> LiquidityRejectReason | None:
    if tick <= Decimal("0"):
        return LiquidityRejectReason(LiquidityRejectKind.INVALID_TICK)

    bid = best_bid(bids)
    ask = best_ask(asks)
    if bid is None or ask is None or bid <= Decimal("0") or ask <= bid:
        return LiquidityRejectReason(LiquidityRejectKind.MISSING_TWO_SIDED_BOOK)

    spread_ticks = (ask - bid) / tick
    if spread_ticks > Decimal(max_spread_ticks):
        return LiquidityRejectReason(
            LiquidityRejectKind.SPREAD_TOO_WIDE,
            spread_ticks=spread_ticks,
            max_spread_ticks=max_spread_ticks,
        )

    bid_depth = top_depth(bids, bid)
    if bid_depth < min_top_depth:
        return LiquidityRejectReason(
            LiquidityRejectKind.BID_DEPTH_TOO_LOW,
            depth=bid_depth,
            min_depth=min_top_depth,
        )

    ask_depth = top_depth(asks, ask)
    if ask_depth < min_top_depth:
        return LiquidityRejectReason(
            LiquidityRejectKind.ASK_DEPTH_TOO_LOW,
            depth=ask_depth,
            min_depth=min_top_depth,
        )

    return None


def top_depth(levels: list[OrderSummary], price: Decimal) -> Decimal:
    return sum(
        (
            _decimal(level.size, Decimal("0"))
            for level in levels
            if _decimal_or_none(level.price) == price
        ),
        Decimal("0"),
    )


def print_plan(plan: QuotePlan, live: bool) -> None:
    mode = "live" if live else "dry-run"
    print(
        f"{mode}: {plan.market_key} :: {plan.market_slug} :: {plan.question} :: "
        f"{plan.outcome} ({plan.token_id}) fair={plan.fair_price} "
        f"bid={plan.best_bid} ask={plan.best_ask} buy={format_band(plan.buy_band)} "
        f"sell={format_band(plan.sell_band)}"
    )


def format_band(band: QuoteBand | None) -> str:
    if band is None:
        return "none"
    return (
        f"price={band.price} band=[{band.min_price}, {band.max_price}] "
        f"size={band.min_size}/{band.avg_size}/{band.max_size}"
    )


def post_quote_plan(
    client: ClobClient,
    plan: QuotePlan,
    config: Config,
    market_state: LiveMarketState,
    public_client: ClobClient | None = None,
    global_budget: RiskBudget | None = None,
    market_budget: RiskBudget | None = None,
) -> None:
    if global_budget is None:
        global_budget = RiskBudget.new(config.max_total_collateral)
    if market_budget is None:
        market_budget = RiskBudget.new(config.max_collateral_per_market)
    open_orders = market_state.open_orders(plan.token_id)
    stale_reason = stale_live_data_reason(plan, market_state, time.monotonic(), config)
    if stale_reason is not None:
        print(f"skip placing {plan.market_slug} {plan.outcome}: stale live data ({stale_reason})")
        return
    position_reason = market_state.position_reconcile_reject_reason()
    if position_reason is not None:
        print(f"skip placing {plan.market_slug} {plan.outcome}: position reconciliation failed ({position_reason})")
        return

    if config.cancel_before_quote:
        orders_to_cancel = cancellable_orders(open_orders, plan, config.max_open_orders_per_token)
        order_ids = [open_order_id(order) for order in orders_to_cancel if open_order_id(order)]
        if order_ids and skip_live_action_if_paused(plan, config, "canceling stale orders"):
            return
        response = client.cancel_orders(order_ids) if order_ids else {}
        canceled = _response_list(response, "canceled")
        not_canceled = _response_list(response, "not_canceled")
        if order_ids or not_canceled:
            print(
                f"canceled band orders for {plan.token_id}: "
                f"requested={len(order_ids)} canceled={len(canceled)} not_canceled={len(not_canceled)}"
            )
        if not_canceled:
            print(f"skip placing {plan.market_slug} {plan.outcome}: some band orders could not be canceled")
            return
        if order_ids:
            canceled_ids = {open_order_id(order) for order in orders_to_cancel if open_order_id(order)}
            open_orders = open_orders_for_token(client, plan.token_id)
            market_state.replace_open_orders(plan.token_id, open_orders)
            if any(open_order_id(order) in canceled_ids for order in open_orders):
                print(f"skip placing {plan.market_slug} {plan.outcome}: canceled order state is still unstable")
                return
            for order in orders_to_cancel:
                global_budget.release_open_buy_order(order)

    stale_reason = stale_live_data_reason(plan, market_state, time.monotonic(), config)
    if stale_reason is not None:
        print(f"skip placing {plan.market_slug} {plan.outcome}: stale live data ({stale_reason})")
        return

    for order in open_orders:
        global_budget.reserve_open_buy_order(order)
        market_budget.reserve_open_buy_order(order)

    breaches = market_state.risk_breaches(config)
    if breaches:
        print_risk_breaches(plan, breaches)
        if config.cancel_on_risk_breach and risk_breach_applies_to_token(breaches, plan.token_id):
            refreshed_orders = cancel_risk_increasing_orders(client, plan, config, open_orders)
            if refreshed_orders is not None:
                market_state.replace_open_orders(plan.token_id, refreshed_orders)
        if config.pause_on_risk_breach:
            reason = risk_breach_pause_reason(plan, breaches)
            PauseState.save_reason(config.pause_path, reason)
            print(f"wrote pause file {config.pause_path}: {reason}")
        return

    token_state = market_state.token_state(plan.token_id)
    token_balance = token_state.balance if token_state is not None else Decimal("0")
    locked_collateral = open_buy_collateral(open_orders)
    locked_tokens = open_sell_size(open_orders)
    free_collateral = max(collateral_balance(client) - locked_collateral - config.min_free_collateral, Decimal("0"))
    free_tokens = max(token_balance - locked_tokens, Decimal("0"))
    new_order_slots = max(config.max_open_orders_per_token - len(open_orders), 0)
    planned_orders: list[SubmittedOrder] = []
    for band in plan.bands():
        if new_order_slots <= 0:
            break
        open_size = band_open_size(open_orders, band)
        missing_size = band_missing_size(band, open_size)
        if missing_size is None:
            continue

        staged_orders = [planned_order.proposed_order for planned_order in planned_orders]
        if band.side == BUY:
            inventory_room = market_state.inventory_buy_room(plan.token_id, staged_orders, config)
            adjusted_size = inventory_adjusted_buy_size(missing_size, band.minimum_size, inventory_room.room)
            if adjusted_size is None:
                print(
                    f"skip {plan.market_slug} {plan.outcome} buy: "
                    f"inventory room {inventory_room.room} below minimum order size {band.minimum_size} "
                    f"(token position {inventory_room.token_position}, "
                    f"market inventory {inventory_room.market_inventory})"
                )
                continue
            if adjusted_size < missing_size:
                print(
                    f"cap {plan.market_slug} {plan.outcome} buy size from {missing_size} to {adjusted_size}: "
                    f"token position {inventory_room.token_position} / max {config.max_inventory_per_token}, "
                    f"market inventory {inventory_room.market_inventory} / max {config.max_inventory_per_market}"
                )
            missing_size = adjusted_size
            collateral = missing_size * band.price
            affordable_size = affordable_buy_size(
                missing_size,
                band.price,
                free_collateral,
                global_budget,
                market_budget,
            )
            if affordable_size < missing_size:
                print(
                    f"skip {plan.market_slug} {plan.outcome} buy: "
                    f"risk budget/free collateral leaves size {affordable_size} below band target {missing_size}"
                )
                continue
        else:
            sell_size = min(missing_size, free_tokens)
            if sell_size < missing_size:
                print(
                    f"skip {plan.market_slug} {plan.outcome} sell: "
                    f"free token balance leaves size {sell_size} below band target {missing_size}"
                )
                continue
            missing_size = sell_size
            collateral = Decimal("0")

        proposed = ProposedOrder(
            token_id=plan.token_id,
            side=band.side,
            price=band.price,
            size=missing_size,
        )
        if market_loss_exceeds_cap(plan, proposed, market_state, config, staged_orders):
            continue
        order = client.create_order(
            OrderArgs(
                token_id=plan.token_id,
                price=band.price,
                size=missing_size,
                side=band.side,
                post_only=config.post_only,
            )
        )
        planned_orders.append(
            SubmittedOrder(
                side=band.side,
                proposed_order=proposed,
                order=order,
                collateral=collateral,
            )
        )
        new_order_slots -= 1

    if planned_orders and public_client is not None:
        move_reason = pre_post_move_reject_reason(public_client, plan, config)
        if move_reason is not None:
            print(f"skip posting {plan.market_slug} {plan.outcome}: pre-post price movement ({move_reason})")
            return

    if planned_orders and skip_live_action_if_paused(plan, config, "posting orders"):
        return

    responses = [
        (planned_order, client.post_order(planned_order.order))
        for planned_order in planned_orders
    ]
    print_post_responses(plan, responses)
    apply_post_responses(client, plan, responses, market_state, global_budget, market_budget)


def cancellable_orders(
    open_orders: list[Any],
    plan: QuotePlan,
    max_open_orders_per_token: int | None = None,
) -> list[Any]:
    cancellable: list[Any] = []
    cancellable_ids: set[str] = set()

    for order in open_orders:
        if order_should_cancel(order, plan):
            order_id = open_order_id(order)
            if order_id and order_id not in cancellable_ids:
                cancellable_ids.add(order_id)
                cancellable.append(order)

    for band in plan.bands():
        matching_orders = [
            order
            for order in open_orders
            if open_order_id(order) not in cancellable_ids and band.includes_order(order)
        ]
        matching_size = sum((open_order_remaining_size(order) for order in matching_orders), Decimal("0"))
        if matching_size <= band.max_size:
            continue

        band_amount = matching_size
        matching_orders.sort(key=lambda order: (-band.cancel_priority(order), open_order_created_at(order)))
        for order in matching_orders:
            if band_amount <= band.avg_size:
                break
            order_id = open_order_id(order)
            if not order_id or order_id in cancellable_ids:
                continue
            cancellable_ids.add(order_id)
            cancellable.append(order)
            band_amount = max(band_amount - open_order_remaining_size(order), Decimal("0"))

    if max_open_orders_per_token is not None:
        kept = [
            order
            for order in open_orders
            if open_order_id(order) not in cancellable_ids
        ]
        if len(kept) > max_open_orders_per_token:
            kept.sort(key=open_order_created_at)
            for order in kept[max_open_orders_per_token:]:
                order_id = open_order_id(order)
                if order_id and order_id not in cancellable_ids:
                    cancellable_ids.add(order_id)
                    cancellable.append(order)

    return cancellable


def order_should_cancel(order: Any, plan: QuotePlan) -> bool:
    side = _response_field(order, "side")
    if side == BUY:
        return plan.buy_band is None or not plan.buy_band.includes_order(order)
    if side == SELL:
        return plan.sell_band is None or not plan.sell_band.includes_order(order)
    return True


def band_open_size(open_orders: list[Any], band: QuoteBand) -> Decimal:
    return sum(
        (open_order_remaining_size(order) for order in open_orders if band.includes_order(order)),
        Decimal("0"),
    )


def band_missing_size(band: QuoteBand, open_size: Decimal) -> Decimal | None:
    if open_size >= band.min_size:
        return None
    return max(band.avg_size - open_size, Decimal("0"))


def inventory_adjusted_buy_size(
    requested_size: Decimal,
    minimum_size: Decimal,
    inventory_room: Decimal,
) -> Decimal | None:
    size = min(requested_size, inventory_room)
    if size <= Decimal("0") or size < minimum_size:
        return None
    return size


def affordable_buy_size(
    requested_size: Decimal,
    price: Decimal,
    free_collateral: Decimal,
    global_budget: RiskBudget,
    market_budget: RiskBudget,
) -> Decimal:
    if requested_size <= Decimal("0") or price <= Decimal("0"):
        return Decimal("0")
    requested_collateral = requested_size * price
    affordable_collateral = min(
        requested_collateral,
        free_collateral,
        global_budget.remaining(),
        market_budget.remaining(),
    )
    return affordable_collateral / price


def open_buy_collateral(open_orders: list[Any]) -> Decimal:
    return sum(
        (
            open_order_remaining_size(order) * _decimal(_response_field(order, "price"), Decimal("0"))
            for order in open_orders
            if _response_field(order, "side") == BUY
        ),
        Decimal("0"),
    )


def open_sell_size(open_orders: list[Any]) -> Decimal:
    return sum(
        (
            open_order_remaining_size(order)
            for order in open_orders
            if _response_field(order, "side") == SELL
        ),
        Decimal("0"),
    )


def token_long_inventory(
    balance: Decimal,
    open_orders: list[Any],
    pending_orders: list[ProposedOrder],
    staged_orders: list[ProposedOrder],
    token_id: str,
) -> Decimal:
    return (
        max(balance, Decimal("0"))
        + buy_order_size_for_token(open_orders, token_id)
        + pending_buy_size_for_token(pending_orders, token_id)
        + pending_buy_size_for_token(staged_orders, token_id)
    )


def buy_order_size_for_token(orders: list[Any], token_id: str) -> Decimal:
    return sum(
        (
            open_order_remaining_size(order)
            for order in orders
            if open_order_token_id(order, token_id) == token_id and _response_field(order, "side") == BUY
        ),
        Decimal("0"),
    )


def pending_buy_size_for_token(orders: list[ProposedOrder], token_id: str) -> Decimal:
    return sum(
        (order.size for order in orders if order.token_id == token_id and order.side == BUY),
        Decimal("0"),
    )


def print_risk_breaches(plan: QuotePlan, breaches: list[RiskBreach]) -> None:
    for breach in breaches:
        print(f"risk breach {plan.market_slug} {plan.outcome}: {breach.message()}")


def print_preflight_risk_breaches(market: dict[str, Any], breaches: list[RiskBreach]) -> None:
    for breach in breaches:
        print(f"preflight risk breach {_market_slug(market)}: {breach.message()}")


def risk_breach_pause_reason(plan: QuotePlan, breaches: list[RiskBreach]) -> str:
    messages = "; ".join(breach.message() for breach in breaches)
    return f"risk breach {plan.market_slug} {plan.outcome}: {messages}"


def preflight_breach_pause_reason(market: dict[str, Any], breaches: list[RiskBreach]) -> str:
    messages = "; ".join(breach.message() for breach in breaches)
    return f"preflight risk breach {_market_slug(market)}: {messages}"


def risk_breach_applies_to_token(breaches: list[RiskBreach], token_id: str) -> bool:
    return any(
        breach.kind != "token_inventory" or breach.token_id == token_id
        for breach in breaches
    )


def cancel_risk_increasing_orders(
    client: ClobClient,
    plan: QuotePlan,
    config: Config,
    open_orders: list[Any],
) -> list[Any] | None:
    order_ids = [
        order_id
        for order in open_orders
        if _response_field(order, "side") == BUY
        for order_id in [open_order_id(order)]
        if order_id
    ]
    if not order_ids:
        return None

    if skip_live_action_if_paused(plan, config, "canceling risk-increasing orders"):
        return None

    response = client.cancel_orders(order_ids)
    canceled = _response_list(response, "canceled")
    not_canceled = _response_list(response, "not_canceled")
    print(
        f"risk breach cancel {plan.market_slug} {plan.outcome}: "
        f"canceled={len(canceled)} not_canceled={len(not_canceled)}"
    )
    return open_orders_for_token(client, plan.token_id)


def cancel_risk_increasing_market_orders(
    client: ClobClient,
    market: dict[str, Any],
    config: Config,
    market_state: LiveMarketState,
) -> None:
    order_ids = [
        order_id
        for token_state in market_state.tokens
        for order in token_state.open_orders
        if _response_field(order, "side") == BUY
        for order_id in [open_order_id(order)]
        if order_id
    ]
    if not order_ids:
        return

    canceled_count = 0
    not_canceled_count = 0
    for batch in batched(order_ids, CANCEL_ORDER_BATCH_SIZE):
        if skip_market_live_action_if_paused(market, config, "canceling preflight risk-increasing orders"):
            return
        response = client.cancel_orders(batch)
        canceled_count += len(_response_list(response, "canceled"))
        not_canceled_count += len(_response_list(response, "not_canceled"))

    print(
        f"preflight risk cancel {_market_slug(market)}: "
        f"canceled={canceled_count} not_canceled={not_canceled_count}"
    )


def skip_live_action_if_paused(plan: QuotePlan, config: Config, action: str) -> bool:
    pause = PauseState.load(config.pause_path)
    if pause is None:
        return False
    print(
        f"skip {action} for {plan.market_slug} {plan.outcome}: "
        f"pause active at {config.pause_path} ({pause.reason.strip()})"
    )
    return True


def skip_market_live_action_if_paused(market: dict[str, Any], config: Config, action: str) -> bool:
    pause = PauseState.load(config.pause_path)
    if pause is None:
        return False
    print(
        f"skip {action} for {_market_slug(market)}: "
        f"pause active at {config.pause_path} ({pause.reason.strip()})"
    )
    return True


def market_loss_exceeds_cap(
    plan: QuotePlan,
    proposed_order: ProposedOrder,
    market_state: LiveMarketState,
    config: Config,
    staged_orders: list[ProposedOrder] | None = None,
) -> bool:
    exposure = market_state.exposure()
    for staged_order in staged_orders or []:
        exposure.apply_order(staged_order)
    projected_loss = exposure.projected_loss(proposed_order)
    if projected_loss <= config.max_loss_per_market:
        return False

    print(
        f"skip {plan.market_slug} {plan.outcome} {proposed_order.side}: "
        f"projected market loss {projected_loss} exceeds cap {config.max_loss_per_market}"
    )
    return True


def open_orders_for_token(client: ClobClient, token_id: str) -> list[Any]:
    orders = client.get_orders(OpenOrderParams(asset_id=token_id))
    return orders if isinstance(orders, list) else []


def cancel_scope_orders(
    public_client: ClobClient,
    live_client: ClobClient,
    config: Config,
    managed_scope: list[dict[str, Any]] | None = None,
) -> None:
    markets = list(managed_scope) if managed_scope is not None else discover_cancel_markets(public_client, config)
    if not markets:
        print("cancel-all: no scoped markets found")
        return

    print(f"cancel-all: checking {len(markets)} scoped markets")
    summary = cancel_open_orders_for_markets(live_client, markets)
    print_cancel_summary(summary)
    if summary.remaining_open > 0:
        raise RuntimeError(f"cancel-all left {summary.remaining_open} scoped open orders after verification")


def discover_cancel_markets(public_client: ClobClient, config: Config) -> list[dict[str, Any]]:
    event_slug = config.event_slug.strip() if config.event_slug else None
    if event_slug:
        markets = discover_event_markets(public_client, event_slug, config.max_pages)
        return [candidate.market for candidate in select_event_candidates(markets, config.max_markets)]

    seen = SeenMarkets.load(config.state_path)
    markets = discover_markets(public_client, config.discovery, config.max_pages)
    return [candidate.market for candidate in select_candidates(markets, seen, config.max_markets)]


def cancel_open_orders_for_markets(
    client: ClobClient,
    markets: list[dict[str, Any]],
) -> CancelOpenOrdersSummary:
    token_ids = managed_token_ids(markets)
    summary = CancelOpenOrdersSummary(
        markets_checked=len(markets),
        tokens_checked=len(token_ids),
    )

    for token_id in token_ids:
        open_orders = open_orders_for_token(client, token_id)
        if not open_orders:
            continue

        print(f"cancel-all: token {token_id} has {len(open_orders)} open orders")
        summary.orders_found += len(open_orders)
        order_ids = [order_id for order in open_orders for order_id in [open_order_id(order)] if order_id]
        for batch in batched(order_ids, CANCEL_ORDER_BATCH_SIZE):
            response = client.cancel_orders(batch)
            canceled = _response_list(response, "canceled")
            not_canceled = _response_list(response, "not_canceled")
            summary.canceled += len(canceled)
            summary.not_canceled += len(not_canceled)
            print(
                f"cancel-all: token {token_id} requested={len(batch)} "
                f"canceled={len(canceled)} not_canceled={len(not_canceled)}"
            )

    if summary.orders_found > 0:
        for attempt in range(1, CANCEL_VERIFY_ATTEMPTS + 1):
            summary.remaining_open = remaining_open_orders_for_tokens(client, token_ids)
            if summary.remaining_open == 0:
                break
            if attempt < CANCEL_VERIFY_ATTEMPTS:
                print(
                    f"cancel-all: {summary.remaining_open} scoped open orders remain; "
                    f"waiting {CANCEL_VERIFY_DELAY_SECS}s before verification retry"
                )
                time.sleep(CANCEL_VERIFY_DELAY_SECS)

    return summary


def managed_token_ids(markets: list[dict[str, Any]]) -> list[str]:
    token_ids: list[str] = []
    seen: set[str] = set()
    for market in markets:
        for token in _market_tokens(market):
            token_id = _token_id(token)
            if token_id and token_id not in seen:
                seen.add(token_id)
                token_ids.append(token_id)
    return token_ids


def remaining_open_orders_for_tokens(client: ClobClient, token_ids: list[str]) -> int:
    return sum(len(open_orders_for_token(client, token_id)) for token_id in token_ids)


def print_cancel_summary(summary: CancelOpenOrdersSummary) -> None:
    print(
        "cancel-all: summary "
        f"markets={summary.markets_checked} tokens={summary.tokens_checked} "
        f"orders_found={summary.orders_found} canceled={summary.canceled} "
        f"not_canceled={summary.not_canceled} remaining_open={summary.remaining_open}"
    )


def batched(items: list[str], batch_size: int) -> list[list[str]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def conditional_balance(client: ClobClient, token_id: str) -> Decimal:
    response = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    )
    balance = _decimal(_response_field(response, "balance"), Decimal("0"))
    return balance / CONDITIONAL_TOKEN_BASE_UNITS


def collateral_balance(client: ClobClient) -> Decimal:
    response = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    balance = _decimal(_response_field(response, "balance"), Decimal("0"))
    return balance / CONDITIONAL_TOKEN_BASE_UNITS


def trades_for_token(
    client: ClobClient,
    token_id: str,
    after_unix_secs: int | None,
) -> list[Any]:
    params = TradeParams(asset_id=token_id, after=after_unix_secs)
    cursor: str | None = INITIAL_CURSOR
    trades: list[Any] = []

    while cursor is not None:
        page = client.get_trades(params, next_cursor=cursor)
        trades.extend(trade for trade in _response_list(page, "data") if is_filled_trade(trade))
        next_cursor = str(_response_field(page, "next_cursor", "nextCursor") or END_CURSOR)
        if next_cursor == END_CURSOR or next_cursor == cursor:
            break
        cursor = next_cursor

    return trades


def is_filled_trade(trade: Any) -> bool:
    status = str(_response_field(trade, "status") or "").lower()
    return status in {"matched", "mined", "confirmed"} or status.endswith((".matched", ".mined", ".confirmed"))


def fill_record_from_trade(trade: Any) -> FillRecord:
    return FillRecord(
        id=str(_response_field(trade, "id", "trade_id", "tradeId")),
        token_id=str(_response_field(trade, "asset_id", "token_id", "assetId", "tokenId")),
        market=str(_response_field(trade, "market", "condition_id", "conditionId") or ""),
        side=side_label(_response_field(trade, "side")),
        size=_decimal(_response_field(trade, "size"), Decimal("0")),
        price=_decimal(_response_field(trade, "price"), Decimal("0")),
        status=str(_response_field(trade, "status") or ""),
        matched_at_unix_secs=trade_matched_at_unix_secs(trade),
    )


def side_label(side: Any) -> str:
    normalized = str(side or "").upper()
    if normalized == BUY or normalized.endswith(f".{BUY}"):
        return BUY
    if normalized == SELL or normalized.endswith(f".{SELL}"):
        return SELL
    return "UNKNOWN"


def trade_matched_at_unix_secs(trade: Any) -> int:
    value = _response_field(trade, "match_time", "matchTime", "matched_at", "matchedAt", "timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if value is not None:
        raw = str(value)
        try:
            return int(raw)
        except ValueError:
            try:
                return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    return 0


def token_cost_basis(
    fill_records: list[FillRecord],
    live_balance: Decimal,
    fallback_fair_price: Decimal,
) -> Decimal:
    if live_balance <= Decimal("0"):
        return Decimal("0")

    position = Decimal("0")
    cost = Decimal("0")
    for record in fill_records:
        if record.side == BUY:
            position += record.size
            cost += record.size * record.price
        elif record.side == SELL and position > Decimal("0"):
            sell_size = min(record.size, position)
            avg_cost = cost / position
            cost = max(cost - avg_cost * sell_size, Decimal("0"))
            position = max(position - sell_size, Decimal("0"))

    if position <= Decimal("0"):
        return live_balance * fallback_fair_price

    avg_cost = cost / position
    if live_balance <= position:
        return live_balance * avg_cost
    return cost + (live_balance - position) * fallback_fair_price


def token_ledger_position(fill_records: list[FillRecord]) -> Decimal:
    position = Decimal("0")
    for record in fill_records:
        if record.side == BUY:
            position += record.size
        elif record.side == SELL:
            position -= record.size
    return position


def position_reconcile_error_for(
    live_balance: Decimal,
    ledger_position: Decimal,
    tolerance: Decimal,
) -> PositionReconcileError | None:
    difference = abs(live_balance - ledger_position)
    if difference <= tolerance:
        return None
    return PositionReconcileError(
        live_balance=live_balance,
        ledger_position=ledger_position,
        difference=difference,
        tolerance=tolerance,
    )


def proposed_order_from_open_order(order: Any, default_token_id: str) -> ProposedOrder | None:
    side = _response_field(order, "side")
    if side not in (BUY, SELL):
        return None

    price = _decimal_or_none(_response_field(order, "price"))
    if price is None:
        return None

    size = open_order_remaining_size(order)
    if size <= Decimal("0"):
        return None

    token_id = str(_response_field(order, "asset_id", "token_id", "tokenId") or default_token_id)
    return ProposedOrder(token_id=token_id, side=side, price=price, size=size)


def open_order_remaining_size(order: Any) -> Decimal:
    original_size = _decimal(
        _response_field(order, "original_size", "originalSize", "size"),
        Decimal("0"),
    )
    size_matched = _decimal(
        _response_field(order, "size_matched", "sizeMatched"),
        Decimal("0"),
    )
    return max(original_size - size_matched, Decimal("0"))


def pre_post_move_reject_reason(
    public_client: ClobClient,
    plan: QuotePlan,
    config: Config,
) -> str | None:
    book = public_client.get_order_book(plan.token_id)
    liquidity_reason = pre_post_liquidity_reject_reason(book, config)
    if liquidity_reason is not None:
        return f"refreshed book liquidity check failed: {liquidity_reason.message()}"

    refreshed_fair = fair_price(
        best_bid(book.bids or []),
        best_ask(book.asks or []),
        plan.fair_price,
        _decimal_or_none(book.last_trade_price),
    )
    return price_move_reject_reason(
        plan.fair_price,
        refreshed_fair,
        _decimal(book.tick_size, Decimal("0")),
        config.max_pre_post_move_ticks,
    )


def pre_post_liquidity_reject_reason(
    book: OrderBookSummary,
    config: Config,
) -> LiquidityRejectReason | None:
    if not should_enforce_liquidity_quality(config):
        return None
    return liquidity_quality_reject_reason(book, config)


def price_move_reject_reason(
    planned_fair: Decimal,
    refreshed_fair: Decimal,
    tick: Decimal,
    max_move_ticks: int,
) -> str | None:
    if tick <= Decimal("0"):
        return "refreshed book tick size is invalid"

    move_ticks = abs(refreshed_fair - planned_fair) / tick
    if move_ticks > Decimal(max_move_ticks):
        return (
            f"fair moved {move_ticks} ticks from {planned_fair} to {refreshed_fair}; "
            f"max {max_move_ticks}"
        )
    return None


def open_order_id(order: Any) -> str:
    value = _response_field(order, "id", "order_id", "orderID")
    return str(value) if value is not None else ""


def open_order_token_id(order: Any, default_token_id: str) -> str:
    value = _response_field(order, "asset_id", "token_id", "tokenId", "assetId")
    return str(value) if value is not None else default_token_id


def response_item_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    value = _response_field(item, "id", "order_id", "orderID")
    return str(value) if value is not None else ""


def apply_post_responses(
    client: ClobClient,
    plan: QuotePlan,
    responses: list[tuple[SubmittedOrder, Any]],
    market_state: LiveMarketState,
    global_budget: RiskBudget,
    market_budget: RiskBudget,
) -> None:
    has_failed_response = False
    for submitted_order, response in responses:
        has_failed_response |= apply_post_response(
            submitted_order,
            response,
            market_state,
            global_budget,
            market_budget,
        )

    if has_failed_response:
        refreshed_orders = open_orders_for_token(client, plan.token_id)
        market_state.remove_pending_orders_now_open(refreshed_orders)
        market_state.replace_open_orders(plan.token_id, refreshed_orders)
        print(f"post state refreshed for {plan.market_slug} {plan.outcome} after rejected order response")


def apply_post_response(
    submitted_order: SubmittedOrder,
    response: Any,
    market_state: LiveMarketState,
    global_budget: RiskBudget,
    market_budget: RiskBudget,
) -> bool:
    if not response_success(response):
        return True

    market_state.record_pending_order(submitted_order.proposed_order)
    if submitted_order.side == BUY:
        global_budget.reserve_new_collateral(submitted_order.collateral)
        market_budget.reserve_new_collateral(submitted_order.collateral)
    return False


def stale_live_data_reason(
    plan: QuotePlan,
    market_state: LiveMarketState,
    now: float,
    config: Config,
) -> str | None:
    max_age_secs = float(config.max_data_age_secs)
    token_state = market_state.token_state(plan.token_id)
    if token_state is None:
        return f"missing live state for token {plan.token_id}"
    return (
        stale_input_reason("order book", plan.book_fetched_at, now, max_age_secs)
        or stale_input_reason("open orders", token_state.open_orders_fetched_at, now, max_age_secs)
        or stale_input_reason("token balance", token_state.balance_fetched_at, now, max_age_secs)
    )


def preflight_stale_data_reason(
    token_quotes: list[TokenQuote],
    market_state: LiveMarketState,
    now: float,
    config: Config,
) -> str | None:
    max_age_secs = float(config.max_data_age_secs)
    for token_quote in token_quotes:
        token_state = market_state.token_state(token_quote.token_id)
        if token_state is None:
            return f"missing live state for token {token_quote.token_id}"
        reason = (
            stale_input_reason("order book", token_quote.book_fetched_at, now, max_age_secs)
            or stale_input_reason("open orders", token_state.open_orders_fetched_at, now, max_age_secs)
            or stale_input_reason("token balance", token_state.balance_fetched_at, now, max_age_secs)
        )
        if reason is not None:
            return f"token {token_quote.token_id} {reason}"
    return None


def stale_input_reason(
    input_name: str,
    fetched_at: float,
    now: float,
    max_age_secs: float,
) -> str | None:
    age = max(now - fetched_at, 0.0)
    if age <= max_age_secs:
        return None
    return f"{input_name} age {int(age * 1000)}ms exceeds max {int(max_age_secs * 1000)}ms"


def open_order_matches_proposed(order: Any, proposed_order: ProposedOrder) -> bool:
    token_id = str(_response_field(order, "asset_id", "token_id", "tokenId") or "")
    side = _response_field(order, "side")
    price = _decimal_or_none(_response_field(order, "price"))
    remaining_size = open_order_remaining_size(order)
    return (
        token_id == proposed_order.token_id
        and side == proposed_order.side
        and price == proposed_order.price
        and Decimal("0") < remaining_size <= proposed_order.size
    )


def open_order_created_at(order: Any) -> float:
    return _timestamp_sort_value(_response_field(order, "created_at", "createdAt"))


def print_post_responses(plan: QuotePlan, responses: list[tuple[SubmittedOrder, Any]]) -> None:
    for submitted_order, response in responses:
        print(
            f"posted {plan.market_slug} {plan.outcome} side={submitted_order.side} "
            f"order_id={_response_field(response, 'orderID', 'order_id', 'id')} "
            f"success={_response_field(response, 'success')} "
            f"status={_response_field(response, 'status')} "
            f"error={_response_field(response, 'errorMsg', 'error_msg', 'error')}"
        )


def best_bid(levels: list[OrderSummary]) -> Decimal | None:
    prices = [_decimal_or_none(level.price) for level in levels]
    prices = [price for price in prices if price is not None]
    return max(prices) if prices else None


def best_ask(levels: list[OrderSummary]) -> Decimal | None:
    prices = [_decimal_or_none(level.price) for level in levels]
    prices = [price for price in prices if price is not None]
    return min(prices) if prices else None


def _market_slug(market: dict[str, Any]) -> str:
    return str(_field(market, "market_slug", "slug") or "unknown")


def _market_question(market: dict[str, Any]) -> str:
    return str(_field(market, "question", "title") or "")


def _market_tokens(market: dict[str, Any]) -> list[dict[str, Any]]:
    tokens = _field(market, "tokens", "outcomes")
    if not isinstance(tokens, list):
        return []
    return [token for token in tokens if isinstance(token, dict)]


def _token_id(token: dict[str, Any]) -> str:
    value = _field(token, "token_id", "tokenId", "asset_id", "assetId", "t")
    return str(value) if value is not None else ""


def _token_outcome(token: dict[str, Any]) -> str:
    return str(_field(token, "outcome", "name", "label") or "")


def _field(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = source.get(name)
        if value is not None:
            return value
    return None


def _dict_field(source: dict[str, Any], name: str) -> dict[str, Any]:
    value = source.get(name)
    return value if isinstance(value, dict) else {}


def _bool_field(source: dict[str, Any], name: str) -> bool:
    value = source.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _decimal(value: Any, default: Decimal) -> Decimal:
    parsed = _decimal_or_none(value)
    return default if parsed is None else parsed


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _response_field(response: Any, *names: str) -> Any:
    for name in names:
        if isinstance(response, dict) and name in response:
            return response.get(name)
        if hasattr(response, name):
            return getattr(response, name)
    return None


def _response_list(response: Any, name: str) -> list[Any]:
    if isinstance(response, list):
        return response
    value = _response_field(response, name)
    return value if isinstance(value, list) else []


def response_success(response: Any) -> bool:
    value = _response_field(response, "success")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _timestamp_sort_value(value: Any) -> float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0
