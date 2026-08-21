"""
Robinhood Crypto Trading API client.

Auth scheme (per Robinhood's official docs):
  - Every request is signed with Ed25519 using your API private key.
  - Message signed = api_key + timestamp + path + method + body
    (body is the raw JSON string for POST, empty string for GET)
  - Signature, api key, and timestamp go in headers:
      x-api-key, x-timestamp, x-signature

Get your API key + keypair from: Robinhood web (classic) -> Account ->
Crypto -> API. Robinhood shows you the private key ONCE at creation -
save it immediately in your password manager, not just in .env.

IMPORTANT: verify current endpoint paths against Robinhood's live docs
(https://docs.robinhood.com/crypto/trading/) before relying on this in
production - broker APIs do change paths/fields over time. The paths
below reflect the documented v1 structure as of this build.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import requests
from nacl.signing import SigningKey

BASE_URL = "https://trading.robinhood.com"


class RobinhoodAPIError(Exception):
    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"Robinhood API error {status_code}: {message}")
        self.status_code = status_code
        self.body = body


@dataclass
class RobinhoodCryptoClient:
    api_key: str
    private_key_b64: str  # base64-encoded Ed25519 private (seed) key

    def __post_init__(self):
        seed = base64.b64decode(self.private_key_b64)
        self._signing_key = SigningKey(seed)

    # ---------- low-level signed request ----------

    def _headers(self, method: str, path: str, body: str) -> dict:
        timestamp = str(int(time.time()))
        message = f"{self.api_key}{timestamp}{path}{method}{body}"
        signature = self._signing_key.sign(message.encode("utf-8")).signature
        return {
            "x-api-key": self.api_key,
            "x-timestamp": timestamp,
            "x-signature": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None) -> Any:
        body_str = json.dumps(json_body) if json_body is not None else ""
        headers = self._headers(method, path, body_str)
        url = BASE_URL + path

        resp = requests.request(
            method, url, headers=headers, params=params,
            data=body_str if json_body is not None else None,
            timeout=15,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise RobinhoodAPIError(resp.status_code, str(detail), detail)
        if not resp.text:
            return {}
        return resp.json()

    # ---------- account / holdings (read-only) ----------

    def get_account(self) -> dict:
        return self._request("GET", "/api/v1/crypto/trading/accounts/")

    def get_holdings(self, asset_codes: Optional[list[str]] = None) -> dict:
        params = {}
        if asset_codes:
            params["asset_code"] = asset_codes
        return self._request("GET", "/api/v1/crypto/trading/holdings/", params=params)

    def get_trading_pairs(self, symbols: Optional[list[str]] = None) -> dict:
        params = {}
        if symbols:
            params["symbol"] = symbols
        return self._request("GET", "/api/v1/crypto/trading/trading_pairs/", params=params)

    # ---------- market data (read-only) ----------

    def get_best_bid_ask(self, symbols: list[str]) -> dict:
        """symbols like ['ETH-USD']"""
        return self._request(
            "GET", "/api/v1/crypto/marketdata/best_bid_ask/",
            params={"symbol": symbols},
        )

    def get_estimated_price(self, symbol: str, side: str, quantities: list[str]) -> dict:
        """side: 'bid' | 'ask' | 'both'. quantities: list of quantity strings to price."""
        return self._request(
            "GET", "/api/v1/crypto/marketdata/estimated_price/",
            params={"symbol": symbol, "side": side, "quantity": quantities},
        )

    # ---------- orders (write) ----------

    def place_order(
        self,
        symbol: str,
        side: str,           # "buy" | "sell"
        order_type: str,     # "market" | "limit"
        asset_quantity: Optional[str] = None,   # e.g. quantity of ETH
        quote_amount: Optional[str] = None,     # e.g. dollar amount to spend/receive
        limit_price: Optional[str] = None,      # required for limit orders
        client_order_id: Optional[str] = None,
        time_in_force: str = "gtc",
    ) -> dict:
        """
        Places a crypto order. Exactly one of asset_quantity / quote_amount
        should be set for market orders (buy $X worth, or sell N ETH).
        Limit orders require asset_quantity + limit_price.
        """
        if order_type == "market":
            config: dict = {}
            if asset_quantity is not None:
                config["asset_quantity"] = asset_quantity
            if quote_amount is not None:
                config["quote_amount"] = quote_amount
            order_config_key = "market_order_config"
        elif order_type == "limit":
            if asset_quantity is None or limit_price is None:
                raise ValueError("limit orders require asset_quantity and limit_price")
            config = {
                "asset_quantity": asset_quantity,
                "limit_price": limit_price,
                "time_in_force": time_in_force,
            }
            order_config_key = "limit_order_config"
        else:
            raise ValueError(f"unknown order_type: {order_type}")

        payload = {
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "symbol": symbol,
            "type": order_type,
            order_config_key: config,
        }
        return self._request("POST", "/api/v1/crypto/trading/orders/", json_body=payload)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")

    def list_orders(self, limit: int = 20) -> dict:
        return self._request("GET", "/api/v1/crypto/trading/orders/", params={"limit": limit})

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/crypto/trading/orders/{order_id}/cancel/")
