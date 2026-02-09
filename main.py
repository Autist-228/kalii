import asyncio
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from config import (
    KALSHI_API_KEY_ID,
    KALSHI_PRIVATE_KEY_PATH,
    MIN_ARB_PERCENT,
    SCAN_INTERVAL_SEC,
    ORDERBOOK_FETCH_WORKERS,
    BANKROLL_CENTS,
    BET_PERCENT,
)
from kalshi_client import KalshiClient
from kalshi_ws import KalshiWebSocket
from arbitrage_scanner import scan_event_for_arb, get_best_yes_ask
from db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("arb_bot.log"),
    ],
)
logger = logging.getLogger("main")


class ArbBot:
    def __init__(self):
        self.client = KalshiClient()
        self.orderbook_cache: dict[str, dict] = {}
        self.market_info: dict[str, dict] = {}
        self.event_markets: dict[str, list[str]] = {}
        self.total_signals = 0
        self.scan_count = 0

    def fetch_orderbook(self, ticker: str) -> tuple[str, dict]:
        try:
            data = self.client.get_orderbook(ticker)
            return ticker, data.get("orderbook", {})
        except Exception as e:
            logger.debug("Failed to fetch orderbook for %s: %s", ticker, e)
            return ticker, {}

    def discover_multi_market_events(self) -> dict[str, list[dict]]:
        logger.info("Discovering mutually exclusive events with 2+ markets...")
        all_events = self.client.get_all_live_events()
        logger.info("Found %d open events total", len(all_events))

        multi_events = {}
        for event in all_events:
            if not event.get("mutually_exclusive"):
                continue

            event_ticker = event.get("event_ticker", "")
            markets = event.get("markets", [])

            if not markets:
                continue

            active_markets = [m for m in markets if m.get("status") in ("open", "active")]

            if len(active_markets) >= 2:
                multi_events[event_ticker] = {
                    "title": event.get("title", ""),
                    "category": event.get("category", ""),
                    "mutually_exclusive": True,
                    "markets": active_markets,
                }
                for m in active_markets:
                    self.market_info[m["ticker"]] = {
                        "title": m.get("title", ""),
                        "event_ticker": event_ticker,
                        "yes_price": m.get("yes_price"),
                        "no_price": m.get("no_price"),
                    }

        logger.info(
            "Found %d mutually exclusive events with 2+ markets",
            len(multi_events),
        )
        return multi_events

    def prefilter_events(self, multi_events: dict) -> dict:
        candidates = {}
        for event_ticker, event_data in multi_events.items():
            markets = event_data["markets"]
            n = len(markets)

            if n == 2:
                m1, m2 = markets[0], markets[1]
                ya1 = m1.get("yes_ask") or 999
                ya2 = m2.get("yes_ask") or 999
                if ya1 + ya2 <= 102:
                    candidates[event_ticker] = event_data
            else:
                total_yes_ask = 0
                all_have_price = True
                for m in markets:
                    ya = m.get("yes_ask")
                    if ya is None:
                        all_have_price = False
                        break
                    total_yes_ask += ya
                if all_have_price and total_yes_ask <= 105:
                    candidates[event_ticker] = event_data

        return candidates

    def scan_all_events_rest(self):
        self.scan_count += 1
        multi_events = self.discover_multi_market_events()

        if not multi_events:
            logger.info("No multi-market events found. Waiting...")
            return

        candidates = self.prefilter_events(multi_events)

        if not candidates:
            logger.info(
                "Scan #%d | %d events checked | 0 candidates after prefilter | 0 signals",
                self.scan_count, len(multi_events),
            )
            return

        all_tickers = []
        for event_data in candidates.values():
            for m in event_data["markets"]:
                all_tickers.append(m["ticker"])

        logger.info(
            "Scan #%d | %d events → %d candidates → fetching %d orderbooks...",
            self.scan_count, len(multi_events), len(candidates), len(all_tickers),
        )

        with ThreadPoolExecutor(max_workers=ORDERBOOK_FETCH_WORKERS) as pool:
            results = list(pool.map(self.fetch_orderbook, all_tickers))

        for ticker, ob in results:
            if ob:
                self.orderbook_cache[ticker] = ob

        scan_signals = 0
        for event_ticker, event_data in candidates.items():
            markets_with_ob = []
            for m in event_data["markets"]:
                ticker = m["ticker"]
                ob = self.orderbook_cache.get(ticker, {})
                if ob:
                    markets_with_ob.append({
                        "ticker": ticker,
                        "title": m.get("title", ""),
                        "orderbook": ob,
                    })

            if len(markets_with_ob) >= 2:
                signals = scan_event_for_arb(
                    event_ticker=event_ticker,
                    event_title=event_data["title"],
                    markets_with_orderbooks=markets_with_ob,
                    min_arb_percent=MIN_ARB_PERCENT,
                )
                for sig in signals:
                    scan_signals += 1
                    self.total_signals += 1
                    self.print_signal(sig)

        bet_size = BANKROLL_CENTS * BET_PERCENT // 100
        logger.info(
            "Scan #%d complete | %d candidates | %d signals | %d total | bankroll %d¢",
            self.scan_count,
            len(candidates),
            scan_signals,
            self.total_signals,
            BANKROLL_CENTS,
        )

    def print_signal(self, sig: dict):
        arb_type = sig["type"]
        event = sig["event_title"]
        m1 = sig["market1_title"]
        m2 = sig["market2_title"]
        p1 = sig["market1_price"]
        p2 = sig["market2_price"]
        total = sig["total_cost"]
        profit = sig["profit_cents"]
        pct = sig["arb_percent"]

        bet_size = BANKROLL_CENTS * BET_PERCENT // 100
        contracts = bet_size // total if total > 0 else 0
        expected_profit = contracts * profit

        print("\n" + "=" * 70)
        print(f"{'ARB SIGNAL':^70}")
        print("=" * 70)
        print(f"  Type:     {arb_type}")
        print(f"  Event:    {event}")
        print(f"  Leg 1:    {m1} @ {p1}¢")
        print(f"  Leg 2:    {m2} @ {p2}¢")
        print(f"  Total:    {total}¢ (buy both)")
        print(f"  Payout:   100¢")
        print(f"  Profit:   {profit}¢ per contract ({pct:.2f}%)")
        print(f"  Bet size: {bet_size}¢ → {contracts} contracts → +{expected_profit}¢ profit")
        print("=" * 70 + "\n")


async def run_rest_scanner(bot: ArbBot):
    logger.info("Starting REST polling scanner (interval=%ds)...", SCAN_INTERVAL_SEC)
    while True:
        try:
            bot.scan_all_events_rest()
        except Exception as e:
            logger.error("Scan error: %s", e)
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def run_ws_scanner(bot: ArbBot):
    ws = KalshiWebSocket()

    ticker_prices: dict[str, dict] = {}
    event_market_count: dict[str, int] = {}

    async def on_ticker(msg: dict):
        ticker = msg.get("market_ticker", "")
        yes_ask = msg.get("yes_ask")
        no_ask = msg.get("no_ask")

        if yes_ask is not None:
            ticker_prices[ticker] = {"yes_ask": yes_ask, "no_ask": no_ask}

        info = bot.market_info.get(ticker)
        if not info:
            return

        event_ticker = info["event_ticker"]
        event_markets = [
            t for t, i in bot.market_info.items()
            if i["event_ticker"] == event_ticker
        ]
        n_markets = len(event_markets)

        if n_markets < 2:
            return

        yes_asks = []
        for t in event_markets:
            tp = ticker_prices.get(t, {})
            ya = tp.get("yes_ask")
            if ya is not None:
                yes_asks.append((t, ya))

        if n_markets == 2 and len(yes_asks) == 2:
            t1, a1 = yes_asks[0]
            t2, a2 = yes_asks[1]
            total = a1 + a2
            if total < 100:
                profit = 100 - total
                arb_pct = (profit / total) * 100
                if arb_pct >= MIN_ARB_PERCENT:
                    m1_info = bot.market_info.get(t1, {})
                    m2_info = bot.market_info.get(t2, {})
                    sig = {
                        "type": "ws_cross_market_yes",
                        "event_ticker": event_ticker,
                        "event_title": event_ticker,
                        "market1_ticker": t1,
                        "market1_title": f"{m1_info.get('title', t1)} YES",
                        "market1_price": a1,
                        "market2_ticker": t2,
                        "market2_title": f"{m2_info.get('title', t2)} YES",
                        "market2_price": a2,
                        "total_cost": total,
                        "profit_cents": profit,
                        "arb_percent": arb_pct,
                    }
                    bot.total_signals += 1
                    bot.print_signal(sig)

        elif n_markets > 2 and len(yes_asks) == n_markets:
            total = sum(a for _, a in yes_asks)
            if total < 100:
                profit = 100 - total
                arb_pct = (profit / total) * 100
                if arb_pct >= MIN_ARB_PERCENT:
                    detail = " + ".join(
                        f"{bot.market_info.get(t,{}).get('title',t)}@{a}¢"
                        for t, a in yes_asks
                    )
                    sig = {
                        "type": "ws_multi_market_all_yes",
                        "event_ticker": event_ticker,
                        "event_title": event_ticker,
                        "market1_ticker": "ALL",
                        "market1_title": f"Buy YES on all {n_markets} markets",
                        "market1_price": total,
                        "market2_ticker": "ALL",
                        "market2_title": detail,
                        "market2_price": 0,
                        "total_cost": total,
                        "profit_cents": profit,
                        "arb_percent": arb_pct,
                    }
                    bot.total_signals += 1
                    bot.print_signal(sig)

    ws.on_ticker = on_ticker

    logger.info("Starting WebSocket scanner for live ticker data...")
    try:
        await ws.connect()
        await ws.subscribe_ticker()
        await ws.listen()
    except Exception as e:
        logger.error("WebSocket scanner error: %s", e)


async def main():
    init_db()

    logger.info("=" * 60)
    logger.info("KALSHI ARBITRAGE SCANNER")
    logger.info("=" * 60)
    logger.info("Mode: SIGNAL ONLY (no execution)")
    logger.info("Min arb: %.1f%%", MIN_ARB_PERCENT)
    logger.info("Bankroll: %d¢ ($%.2f)", BANKROLL_CENTS, BANKROLL_CENTS / 100)
    logger.info("Bet size: %d%% = %d¢", BET_PERCENT, BANKROLL_CENTS * BET_PERCENT // 100)
    logger.info("=" * 60)

    bot = ArbBot()

    try:
        balance_data = bot.client.get_balance()
        balance = balance_data.get("balance", 0)
        logger.info("Account balance: %d¢ ($%.2f)", balance, balance / 100)
    except Exception as e:
        logger.warning("Could not fetch balance (auth issue?): %s", e)
        logger.info("Continuing with public data only...")

    await asyncio.gather(
        run_rest_scanner(bot),
        run_ws_scanner(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
