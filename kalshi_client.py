import json
import uuid
import requests
from typing import Optional
from kalshi_auth import load_private_key_from_file, load_private_key_from_string, get_auth_headers
from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_BASE_URL, API_PATH_PREFIX


class KalshiClient:
    def __init__(
        self,
        api_key_id: str = KALSHI_API_KEY_ID,
        private_key_path: str = KALSHI_PRIVATE_KEY_PATH,
        base_url: str = KALSHI_BASE_URL,
        private_key_pem: Optional[str] = None,
    ):
        self.api_key_id = api_key_id
        self.base_url = base_url
        if private_key_pem:
            self.private_key = load_private_key_from_string(private_key_pem)
        else:
            self.private_key = load_private_key_from_file(private_key_path)

    def _headers(self, method: str, path: str) -> dict:
        return get_auth_headers(self.private_key, self.api_key_id, method, path)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        full_path = f"{API_PATH_PREFIX}{path}"
        url = f"{self.base_url}{full_path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query:
                url = f"{url}?{query}"
        headers = self._headers("GET", full_path)
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        full_path = f"{API_PATH_PREFIX}{path}"
        url = f"{self.base_url}{full_path}"
        headers = self._headers("POST", full_path)
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code}: {resp.text}",
                response=resp,
            )
        return resp.json()

    def _delete(self, path: str) -> dict:
        full_path = f"{API_PATH_PREFIX}{path}"
        url = f"{self.base_url}{full_path}"
        headers = self._headers("DELETE", full_path)
        resp = requests.delete(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _get_public(self, path: str, params: Optional[dict] = None) -> dict:
        full_path = f"{API_PATH_PREFIX}{path}"
        url = f"{self.base_url}{full_path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query:
                url = f"{url}?{query}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_balance(self) -> dict:
        return self._get("/portfolio/balance")

    def get_events(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        with_nested_markets: bool = True,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        params = {
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
            "limit": str(limit),
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get_public("/events", params)

    def get_markets(
        self,
        event_ticker: Optional[str] = None,
        status: str = "open",
        cursor: Optional[str] = None,
        limit: int = 200,
    ) -> dict:
        params = {"status": status, "limit": str(limit)}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get_public("/markets", params)

    def get_market(self, ticker: str) -> dict:
        return self._get_public(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        return self._get_public(f"/markets/{ticker}/orderbook")

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> dict:
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        return self._get_public(f"/events/{event_ticker}", params)

    def get_all_live_events(self) -> list:
        all_events = []
        cursor = None
        while True:
            data = self.get_events(status="open", cursor=cursor)
            events = data.get("events", [])
            if not events:
                break
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
        return all_events

    def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: int,
        client_order_id: Optional[str] = None,
        time_in_force: str = "fill_or_kill",
    ) -> dict:
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())
        body: dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "client_order_id": client_order_id,
            "time_in_force": time_in_force,
        }
        if side == "yes":
            body["yes_price"] = price
        else:
            body["no_price"] = price
        return self._post("/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        return self._get(f"/portfolio/orders/{order_id}")

    def get_positions(self, cursor: Optional[str] = None, limit: int = 200) -> dict:
        params = {"limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        return self._get("/portfolio/positions", params)

    def get_all_markets_for_event(self, event_ticker: str) -> list:
        all_markets = []
        cursor = None
        while True:
            data = self.get_markets(event_ticker=event_ticker, cursor=cursor)
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
        return all_markets
