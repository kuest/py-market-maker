"""Small boundary between the market-maker strategy and the unified SDK.

The strategy keeps its existing CLOB sampling and market discovery semantics;
the unified SDK handles trading, account data, and order signing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any
import httpx

from market_sdk import PublicClient, SecureClient, configured_environment
from market_sdk.models import Market
from market_sdk.site_scope import SiteMarketScope, get_site_market_scope, has_configured_site_scope

from .config import Config

BUY = "BUY"
SELL = "SELL"
END_CURSOR = "LTE="
INITIAL_CURSOR = "MA=="


def _wallet_deployed(config: Config) -> bool:
    response = httpx.get(
        f"{config.relayer_host.rstrip('/')}/deployed",
        params={"address": config.deposit_wallet}, timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("deployed"), bool):
        raise ValueError("Relayer wallet deployment response is invalid")
    return payload["deployed"]


def _in_site_scope(market: dict[str, Any], scope: SiteMarketScope) -> bool:
    condition_id = market.get("condition_id")
    if isinstance(condition_id, str) and condition_id.lower() in scope.condition_ids:
        return True
    tokens = market.get("tokens")
    return isinstance(tokens, list) and any(
        isinstance(token, dict)
        and str(token.get("token_id", "")).lower() in scope.token_ids
        for token in tokens
    )


class AssetType(str, Enum):
    COLLATERAL = "COLLATERAL"
    CONDITIONAL = "CONDITIONAL"


@dataclass(frozen=True)
class BalanceAllowanceParams:
    asset_type: AssetType
    token_id: str | None = None


@dataclass(frozen=True)
class OpenOrderParams:
    asset_id: str


@dataclass(frozen=True)
class TradeParams:
    asset_id: str
    after: int | None = None


@dataclass(frozen=True)
class OrderArgs:
    token_id: str
    price: Decimal
    size: Decimal
    side: str
    post_only: bool = False


@dataclass(frozen=True)
class OrderSummary:
    price: str
    size: str


@dataclass(frozen=True)
class OrderBookSummary:
    bids: list[OrderSummary]
    asks: list[OrderSummary]
    tick_size: str
    timestamp: str | None = None
    last_trade_price: str | None = None


def sdk_environment(config: Config):
    if config.chain_id is not None and config.chain_id != 137:
        raise ValueError("The configured unified SDK currently supports Polygon Mainnet (137) only")
    return configured_environment(
        clob_url=config.clob_host,
        clob_ws_url=config.clob_ws_host,
        relayer_url=config.relayer_host,
        gamma_url=config.gamma_host,
        data_url=config.data_host,
        rpc_url=config.rpc_url,
    )


def _market_record(market: Market) -> dict[str, Any]:
    """Keep only supported CTF binary markets and required trading fields."""
    if market.version not in (None, "v1"):
        return {}
    if not market.condition_id:
        return {}
    outcomes = (market.outcomes.yes, market.outcomes.no)
    if any(not outcome.token_id for outcome in outcomes):
        return {}
    return {
        "condition_id": str(market.condition_id),
        "slug": market.slug or "",
        "question": market.question or "",
        "active": market.state.active is True,
        "closed": market.state.closed is True,
        "archived": market.state.archived is True,
        "accepting_orders": market.state.accepting_orders is True,
        "enable_order_book": market.state.enable_order_book is True,
        "tokens": [
            {
                "token_id": str(outcome.token_id),
                "outcome": outcome.label,
                "price": str(outcome.price) if outcome.price is not None else None,
            }
            for outcome in outcomes
        ],
        "rewards": {
            "rates": list(market.rewards.clob_rewards or ()),
            "min_size": market.rewards.rewards_min_size or Decimal("0"),
        },
    }


class ClobClient:
    """Strategy-facing adapter: unified SDK for orders; CLOB for discovery."""

    def __init__(self, sdk: PublicClient | SecureClient, config: Config | None = None):
        self.sdk = sdk
        self.config = config

    @classmethod
    def public(cls, config: Config) -> ClobClient:
        return cls(PublicClient(environment=sdk_environment(config)), config)

    @classmethod
    def authenticated(cls, config: Config) -> ClobClient:
        if not config.private_key or not config.deposit_wallet:
            raise ValueError("Live mode requires a private key and Deposit Wallet")
        if not _wallet_deployed(config):
            raise ValueError("The configured Deposit Wallet is not deployed; this bot will not deploy it")
        return cls(SecureClient.create(
            private_key=config.private_key,
            wallet=config.deposit_wallet,
            environment=sdk_environment(config),
        ), config)

    def _secure(self) -> SecureClient:
        if not isinstance(self.sdk, SecureClient):
            raise RuntimeError("Authenticated SDK client required")
        return self.sdk

    def _market_page(self, cursor: str, *, sampling: bool) -> dict[str, Any]:
        if self.config is None:
            raise RuntimeError("CLOB discovery requires endpoint configuration")
        scope = get_site_market_scope() if has_configured_site_scope() else None
        path = "sampling-markets" if sampling else "markets"
        for _ in range(50):
            response = httpx.get(
                f"{self.config.clob_host.rstrip('/')}/{path}",
                params={"next_cursor": cursor}, timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not isinstance(payload.get("next_cursor"), str):
                raise ValueError(f"CLOB {path} page has an invalid response")
            records = [item for item in payload["data"] if isinstance(item, dict)]
            if scope is not None:
                records = [item for item in records if _in_site_scope(item, scope)]
            next_cursor = payload["next_cursor"]
            if scope is None or records or next_cursor in (END_CURSOR, cursor):
                return {"data": records, "next_cursor": next_cursor}
            cursor = next_cursor
        raise RuntimeError(f"CLOB {path} site scope exceeded 50 pages")

    def get_sampling_markets(self, cursor: str) -> dict[str, Any]:
        return self._market_page(cursor, sampling=True)

    def get_markets(self, cursor: str) -> dict[str, Any]:
        return self._market_page(cursor, sampling=False)

    def get_market(self, condition_id: str) -> dict[str, Any]:
        if self.config is None:
            raise RuntimeError("CLOB discovery requires endpoint configuration")
        if has_configured_site_scope() and condition_id.lower() not in get_site_market_scope().condition_ids:
            return {}
        response = httpx.get(
            f"{self.config.clob_host.rstrip('/')}/markets/{condition_id}", timeout=10,
        )
        response.raise_for_status()
        market = response.json()
        if not isinstance(market, dict):
            raise ValueError("CLOB market response must be an object")
        return market

    def get_order_book(self, token_id: str):
        return self.sdk.get_order_book(token_id=token_id)

    def get_orders(self, params: OpenOrderParams) -> list[Any]:
        return list(self._secure().list_open_orders(asset_id=params.asset_id).iter_items())

    def get_trades(self, params: TradeParams, *, next_cursor: str | None) -> dict[str, Any]:
        paginator = self._secure().list_account_trades(
            asset_id=params.asset_id,
            after=str(params.after) if params.after is not None else None,
        )
        page = (
            paginator if next_cursor in (None, INITIAL_CURSOR)
            else paginator.from_cursor(next_cursor)
        ).first_page()
        return {"data": list(page.items), "next_cursor": page.next_cursor if page.has_more else END_CURSOR}

    def get_balance_allowance(self, params: BalanceAllowanceParams):
        return self._secure().get_balance_allowance(
            asset_type=params.asset_type.value,
            token_id=params.token_id,
        )

    def create_order(self, args: OrderArgs):
        return self._secure().create_limit_order(
            token_id=args.token_id,
            price=str(args.price),
            size=str(args.size),
            side=args.side,
            post_only=args.post_only,
        )

    def post_order(self, signed_order: Any) -> dict[str, Any]:
        result = self._secure().post_order(signed_order)
        if result.ok:
            return {"success": True, "orderID": str(result.order_id), "status": result.status}
        return {"success": False, "errorMsg": result.message, "status": result.code}

    def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        result = self._secure().cancel_orders(order_ids=order_ids)
        return {"canceled": list(result.canceled), "not_canceled": list(result.not_canceled)}
