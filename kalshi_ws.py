import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets

from kalshi_auth import load_private_key_from_file, load_private_key_from_string, create_signature
from config import KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_WS_URL

logger = logging.getLogger("kalshi_ws")


class KalshiWebSocket:
    def __init__(
        self,
        api_key_id: str = KALSHI_API_KEY_ID,
        private_key_path: str = KALSHI_PRIVATE_KEY_PATH,
        ws_url: str = KALSHI_WS_URL,
        private_key_pem: Optional[str] = None,
    ):
        self.api_key_id = api_key_id
        self.ws_url = ws_url
        if private_key_pem:
            self.private_key = load_private_key_from_string(private_key_pem)
        else:
            self.private_key = load_private_key_from_file(private_key_path)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.message_id = 1
        self.subscriptions: dict[int, dict] = {}
        self.on_ticker: Optional[Callable] = None
        self.on_orderbook_snapshot: Optional[Callable] = None
        self.on_orderbook_delta: Optional[Callable] = None
        self.on_trade: Optional[Callable] = None
        self._running = False

    def _get_auth_headers(self) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        signature = create_signature(self.private_key, timestamp_ms, "GET", path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    async def connect(self):
        headers = self._get_auth_headers()
        self.ws = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )
        logger.info("WebSocket connected to %s", self.ws_url)

    async def subscribe_ticker(self, market_tickers: Optional[list] = None):
        msg = {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {"channels": ["ticker"]},
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
        await self.ws.send(json.dumps(msg))
        self.message_id += 1

    async def subscribe_orderbook(self, market_tickers: list):
        msg = {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_tickers,
            },
        }
        await self.ws.send(json.dumps(msg))
        self.message_id += 1

    async def subscribe_trades(self, market_tickers: Optional[list] = None):
        msg = {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {"channels": ["trade"]},
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
        await self.ws.send(json.dumps(msg))
        self.message_id += 1

    async def _process_message(self, raw: str):
        data = json.loads(raw)
        msg_type = data.get("type")

        if msg_type == "ticker":
            if self.on_ticker:
                await self.on_ticker(data.get("msg", {}))
        elif msg_type == "orderbook_snapshot":
            if self.on_orderbook_snapshot:
                await self.on_orderbook_snapshot(data.get("msg", {}))
        elif msg_type == "orderbook_delta":
            if self.on_orderbook_delta:
                await self.on_orderbook_delta(data.get("msg", {}))
        elif msg_type == "trade":
            if self.on_trade:
                await self.on_trade(data.get("msg", {}))
        elif msg_type == "subscribed":
            sid = data.get("msg", {}).get("sid")
            channel = data.get("msg", {}).get("channel")
            logger.info("Subscribed to %s (sid=%s)", channel, sid)
        elif msg_type == "error":
            error_msg = data.get("msg", {})
            logger.error("WebSocket error: %s", error_msg)

    async def listen(self):
        self._running = True
        while self._running:
            try:
                if self.ws is None:
                    await self.connect()
                async for message in self.ws:
                    await self._process_message(message)
            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket disconnected: %s. Reconnecting in 3s...", e)
                await asyncio.sleep(3)
                self.ws = None
            except Exception as e:
                logger.error("WebSocket error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)
                self.ws = None

    async def close(self):
        self._running = False
        if self.ws:
            await self.ws.close()
