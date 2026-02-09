import asyncio
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from config import (
    MIN_ARB_PERCENT,
    SCAN_INTERVAL_SEC,
    ORDERBOOK_FETCH_WORKERS,
    BANKROLL_CENTS,
    BET_PERCENT,
)
from kalshi_client import KalshiClient
from kalshi_ws import KalshiWebSocket
from arbitrage_scanner import scan_event_for_arb, taker_fee_cents
from paper_trader import PaperTrader
from db import init_db, log_signal

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
    def __init__(self, paper_trader: PaperTrader):
        self.client = KalshiClient()
        self.paper_trader = paper_trader
        self.orderbook_cache: dict[str, dict] = {}
        self.market_info: dict[str, dict] = {}
        self.total_signals = 0
        self.scan_count = 0
        self.start_time = time.time()

    def fetch_orderbook(self, ticker: str) -> tuple[str, dict]:
        try:
            data = self.client.get_orderbook(ticker)
            return ticker, data.get("orderbook", {})
        except Exception as e:
            logger.debug("Failed to fetch orderbook for %s: %s", ticker, e)
            return ticker, {}

    def discover_2market_events(self) -> dict[str, dict]:
        logger.info("Discovering mutually exclusive 2-market events...")
        all_events = self.client.get_all_live_events()
        logger.info("Found %d open events total", len(all_events))

        two_market_events = {}
        for event in all_events:
            if not event.get("mutually_exclusive"):
                continue

            event_ticker = event.get("event_ticker", "")
            markets = event.get("markets", [])
            if not markets:
                continue

            active_markets = [m for m in markets if m.get("status") in ("open", "active")]

            if len(active_markets) == 2:
                two_market_events[event_ticker] = {
                    "title": event.get("title", ""),
                    "category": event.get("category", ""),
                    "mutually_exclusive": True,
                    "markets": active_markets,
                }
                for m in active_markets:
                    self.market_info[m["ticker"]] = {
                        "title": m.get("title", ""),
                        "event_ticker": event_ticker,
                        "event_title": event.get("title", ""),
                    }

        logger.info(
            "Found %d mutually exclusive 2-market events",
            len(two_market_events),
        )
        return two_market_events

    def prefilter_events(self, events: dict) -> dict:
        candidates = {}
        for event_ticker, event_data in events.items():
            m1, m2 = event_data["markets"]
            ya1 = m1.get("yes_ask")
            ya2 = m2.get("yes_ask")
            if ya1 is None or ya2 is None:
                continue
            if ya1 <= 0 or ya2 <= 0:
                continue
            if ya1 + ya2 <= 102:
                candidates[event_ticker] = event_data
        return candidates

    def scan_all_events_rest(self) -> list[dict]:
        self.scan_count += 1
        events = self.discover_2market_events()

        if not events:
            logger.info("No 2-market events found.")
            return []

        candidates = self.prefilter_events(events)

        if not candidates:
            logger.info(
                "Scan #%d | %d events | 0 candidates | 0 signals",
                self.scan_count, len(events),
            )
            return []

        all_tickers = []
        for event_data in candidates.values():
            for m in event_data["markets"]:
                all_tickers.append(m["ticker"])

        logger.info(
            "Scan #%d | %d events → %d candidates → %d orderbooks...",
            self.scan_count, len(events), len(candidates), len(all_tickers),
        )

        with ThreadPoolExecutor(max_workers=ORDERBOOK_FETCH_WORKERS) as pool:
            results = list(pool.map(self.fetch_orderbook, all_tickers))

        for ticker, ob in results:
            if ob:
                self.orderbook_cache[ticker] = ob

        all_signals = []
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

            if len(markets_with_ob) == 2:
                signals = scan_event_for_arb(
                    event_ticker=event_ticker,
                    event_title=event_data["title"],
                    markets_with_orderbooks=markets_with_ob,
                    min_arb_percent=MIN_ARB_PERCENT,
                )
                for sig in signals:
                    self.total_signals += 1
                    self.print_signal(sig)
                    self.paper_trader.execute_paper_trade(sig)
                    all_signals.append(sig)

        elapsed = time.time() - self.start_time
        logger.info(
            "Scan #%d done | %d candidates | %d signals this scan | %d total | %.0fs elapsed | balance %d¢",
            self.scan_count, len(candidates), len(all_signals),
            self.total_signals, elapsed, self.paper_trader.balance,
        )

        return all_signals

    def print_signal(self, sig: dict):
        total = sig["total_cost"]
        net = sig.get("net_profit", sig["profit_cents"])
        fees = sig.get("total_fees", 0)
        pct = sig.get("net_arb_percent", sig["arb_percent"])
        qty = sig.get("available_qty", 0)

        bet_size = self.paper_trader.get_bet_size()
        cost_with_fees = total + fees
        contracts = min(bet_size // cost_with_fees, qty) if cost_with_fees > 0 and qty > 0 else 0
        expected = contracts * net

        print("\n" + "=" * 70)
        print(f"{'ARB SIGNAL (after fees)':^70}")
        print("=" * 70)
        print(f"  Event:    {sig['event_title']}")
        print(f"  Leg 1:    {sig['market1_title']} @ {sig['market1_price']}¢")
        print(f"  Leg 2:    {sig['market2_title']} @ {sig['market2_price']}¢")
        print(f"  Total:    {total}¢ + {fees}¢ fees = {cost_with_fees}¢")
        print(f"  Net:      +{net}¢/contract ({pct:.2f}%) | qty={qty}")
        print(f"  Paper:    {contracts} contracts → +{expected}¢")
        print("=" * 70 + "\n")


async def run_rest_scanner(bot: ArbBot, duration_sec: int = 0):
    logger.info("Starting REST scanner (interval=%ds)...", SCAN_INTERVAL_SEC)
    start = time.time()
    while True:
        try:
            bot.scan_all_events_rest()
        except Exception as e:
            logger.error("Scan error: %s", e)
        if duration_sec > 0 and (time.time() - start) >= duration_sec:
            logger.info("REST scanner duration reached (%ds). Stopping.", duration_sec)
            break
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def run_ws_scanner(bot: ArbBot, duration_sec: int = 0):
    ws = KalshiWebSocket()
    ticker_prices: dict[str, dict] = {}
    start = time.time()

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

        if len(event_markets) != 2:
            return

        yes_asks = []
        for t in event_markets:
            tp = ticker_prices.get(t, {})
            ya = tp.get("yes_ask")
            if ya is not None and ya > 0:
                yes_asks.append((t, ya))

        if len(yes_asks) != 2:
            return

        t1, a1 = yes_asks[0]
        t2, a2 = yes_asks[1]
        total = a1 + a2
        if total >= 100:
            return

        fee1 = taker_fee_cents(a1)
        fee2 = taker_fee_cents(a2)
        total_fees = fee1 + fee2
        gross = 100 - total
        net = gross - total_fees

        if net <= 0:
            return

        net_pct = (net / (total + total_fees)) * 100
        if net_pct < MIN_ARB_PERCENT:
            return

        m1_info = bot.market_info.get(t1, {})
        m2_info = bot.market_info.get(t2, {})
        sig = {
            "type": "ws_cross_market_yes",
            "event_ticker": event_ticker,
            "event_title": m1_info.get("event_title", event_ticker),
            "market1_ticker": t1,
            "market1_title": f"{m1_info.get('title', t1)} YES",
            "market1_price": a1,
            "market2_ticker": t2,
            "market2_title": f"{m2_info.get('title', t2)} YES",
            "market2_price": a2,
            "total_cost": total,
            "total_fees": total_fees,
            "gross_profit": gross,
            "net_profit": net,
            "net_arb_percent": net_pct,
            "profit_cents": net,
            "arb_percent": net_pct,
            "available_qty": 1,
        }
        log_signal(
            event_ticker=event_ticker,
            event_title=m1_info.get("event_title", event_ticker),
            market1_ticker=t1,
            market1_title=f"{m1_info.get('title', t1)} YES",
            market1_yes_price=a1,
            market2_ticker=t2,
            market2_title=f"{m2_info.get('title', t2)} YES",
            market2_yes_price=a2,
            total_cost_cents=total,
            arb_profit_cents=net,
            arb_percent=net_pct,
            arb_type="ws_cross_market_yes",
        )
        bot.total_signals += 1
        bot.print_signal(sig)
        bot.paper_trader.execute_paper_trade(sig)

        bot.paper_trader.check_early_exit(t1, a1)
        bot.paper_trader.check_early_exit(t2, a2)

    ws.on_ticker = on_ticker

    logger.info("Starting WebSocket scanner...")
    try:
        await ws.connect()
        await ws.subscribe_ticker()
        if duration_sec > 0:
            try:
                await asyncio.wait_for(ws.listen(), timeout=duration_sec)
            except asyncio.TimeoutError:
                logger.info("WS scanner duration reached (%ds). Stopping.", duration_sec)
        else:
            await ws.listen()
    except Exception as e:
        logger.error("WebSocket scanner error: %s", e)
    finally:
        await ws.close()


async def main():
    init_db()

    duration = 0
    if len(sys.argv) > 1:
        try:
            duration = int(sys.argv[1])
        except ValueError:
            pass

    paper = PaperTrader(initial_balance=BANKROLL_CENTS)

    logger.info("=" * 60)
    logger.info("KALSHI ARBITRAGE BOT — PAPER TRADING")
    logger.info("=" * 60)
    logger.info("Mode: PAPER TRADING (no real money)")
    logger.info("Min net arb: %.1f%% (after fees)", MIN_ARB_PERCENT)
    logger.info("Bankroll: %d¢ ($%.2f)", BANKROLL_CENTS, BANKROLL_CENTS / 100)
    logger.info("Bet size: %d%% = %d¢", BET_PERCENT, BANKROLL_CENTS * BET_PERCENT // 100)
    logger.info("Only scanning 2-market mutually exclusive events")
    if duration:
        logger.info("Duration: %d seconds", duration)
    logger.info("=" * 60)

    bot = ArbBot(paper_trader=paper)

    try:
        balance_data = bot.client.get_balance()
        balance = balance_data.get("balance", 0)
        logger.info("Real account balance: %d¢ ($%.2f)", balance, balance / 100)
    except Exception as e:
        logger.warning("Could not fetch balance: %s", e)

    try:
        await asyncio.gather(
            run_rest_scanner(bot, duration_sec=duration),
            run_ws_scanner(bot, duration_sec=duration),
        )
    except KeyboardInterrupt:
        pass
    finally:
        paper.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
