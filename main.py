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
    ALLOWED_CATEGORIES,
    EVENT_CACHE_SEC,
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
        self._cached_events: dict[str, dict] = {}
        self._events_fetched_at: float = 0

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
        skipped_category = 0
        for event in all_events:
            if not event.get("mutually_exclusive"):
                continue

            category = event.get("category", "")
            if category not in ALLOWED_CATEGORIES:
                skipped_category += 1
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
            "Found %d sports 2-market events (skipped %d non-sports)",
            len(two_market_events), skipped_category,
        )
        return two_market_events

    def prefilter_events(self, events: dict) -> dict:
        candidates = {}
        for event_ticker, event_data in events.items():
            m1, m2 = event_data["markets"]
            ya1 = m1.get("yes_ask")
            ya2 = m2.get("yes_ask")
            na1 = m1.get("no_ask")
            na2 = m2.get("no_ask")

            yes_sum_ok = False
            if ya1 and ya2 and ya1 > 0 and ya2 > 0:
                yes_sum_ok = ya1 + ya2 <= 105

            no_sum_ok = False
            if na1 and na2 and na1 > 0 and na2 > 0:
                no_sum_ok = na1 + na2 <= 105

            missing_data = (ya1 is None or ya1 <= 0 or ya2 is None or ya2 <= 0)

            if yes_sum_ok or no_sum_ok or missing_data:
                candidates[event_ticker] = event_data
        return candidates

    def scan_all_events_rest(self) -> list[dict]:
        self.scan_count += 1
        now = time.time()
        if not self._cached_events or (now - self._events_fetched_at) >= EVENT_CACHE_SEC:
            self._cached_events = self.discover_2market_events()
            self._events_fetched_at = now
        else:
            logger.info("Using cached event list (%d events, %.0fs old)",
                        len(self._cached_events), now - self._events_fetched_at)
        events = self._cached_events

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

        exits = self.paper_trader.check_exits_with_orderbooks(self.orderbook_cache)
        for ex in exits:
            self.print_exit(ex)

        elapsed = time.time() - self.start_time
        open_ct = len(self.paper_trader.open_positions)
        closed_ct = len(self.paper_trader.closed_positions)
        logger.info(
            "Scan #%d done | %d candidates | %d signals | %d exits | open %d | closed %d | %.0fs elapsed | balance %d¢",
            self.scan_count, len(candidates), len(all_signals), len(exits),
            open_ct, closed_ct, elapsed, self.paper_trader.balance,
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

    def print_exit(self, pos: dict):
        print("\n" + "*" * 70)
        print(f"{'POSITION SOLD':^70}")
        print("*" * 70)
        print(f"  Event:    {pos['event_title']}")
        print(f"  Bought:   {pos['leg1_ticker']} {pos['leg1_side']}@{pos['leg1_price']}¢ + {pos['leg2_ticker']} {pos['leg2_side']}@{pos['leg2_price']}¢")
        print(f"  Sold:     @{pos.get('sell_leg1_price', '?')}¢ + @{pos.get('sell_leg2_price', '?')}¢")
        print(f"  Profit:   +{pos.get('exit_profit', 0)}¢ ({pos['contracts']} contracts)")
        print(f"  Balance:  {self.paper_trader.balance}¢")
        print("*" * 70 + "\n")


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

    def _make_ws_signal(arb_type: str, event_ticker: str, t1: str, t2: str,
                        label1: str, label2: str, p1: int, p2: int,
                        total: int, total_fees: int, gross: int, net: int, net_pct: float):
        m1_info = bot.market_info.get(t1, {})
        m2_info = bot.market_info.get(t2, {})
        sig = {
            "type": arb_type,
            "event_ticker": event_ticker,
            "event_title": m1_info.get("event_title", event_ticker),
            "market1_ticker": t1,
            "market1_title": f"{m1_info.get('title', t1)} {label1}",
            "market1_price": p1,
            "market2_ticker": t2,
            "market2_title": f"{m2_info.get('title', t2)} {label2}",
            "market2_price": p2,
            "total_cost": total,
            "total_fees": total_fees,
            "gross_profit": gross,
            "net_profit": net,
            "net_arb_percent": net_pct,
            "profit_cents": net,
            "arb_percent": net_pct,
            "available_qty": 999,
        }
        log_signal(
            event_ticker=event_ticker,
            event_title=m1_info.get("event_title", event_ticker),
            market1_ticker=t1,
            market1_title=f"{m1_info.get('title', t1)} {label1}",
            market1_yes_price=p1,
            market2_ticker=t2,
            market2_title=f"{m2_info.get('title', t2)} {label2}",
            market2_yes_price=p2,
            total_cost_cents=total,
            arb_profit_cents=net,
            arb_percent=net_pct,
            arb_type=arb_type,
        )
        bot.total_signals += 1
        bot.print_signal(sig)
        bot.paper_trader.execute_paper_trade(sig)

    def _check_pair(arb_type: str, event_ticker: str,
                    t1: str, t2: str, label1: str, label2: str,
                    p1: int, p2: int):
        total = p1 + p2
        if total >= 100:
            return
        fee1 = taker_fee_cents(p1)
        fee2 = taker_fee_cents(p2)
        total_fees = fee1 + fee2
        gross = 100 - total
        net = gross - total_fees
        if net <= 0:
            return
        net_pct = (net / (total + total_fees)) * 100
        if net_pct < MIN_ARB_PERCENT:
            return
        _make_ws_signal(arb_type, event_ticker, t1, t2, label1, label2,
                        p1, p2, total, total_fees, gross, net, net_pct)

    async def on_ticker(msg: dict):
        ticker = msg.get("market_ticker", "")
        yes_ask = msg.get("yes_ask")
        no_ask = msg.get("no_ask")
        yes_bid = msg.get("yes_bid")
        no_bid = msg.get("no_bid")

        existing = ticker_prices.get(ticker, {})
        if yes_ask is not None:
            existing["yes_ask"] = yes_ask
        if no_ask is not None:
            existing["no_ask"] = no_ask
        if yes_bid is not None:
            existing["yes_bid"] = yes_bid
        if no_bid is not None:
            existing["no_bid"] = no_bid
        ticker_prices[ticker] = existing

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

        t1, t2 = event_markets[0], event_markets[1]
        tp1 = ticker_prices.get(t1, {})
        tp2 = ticker_prices.get(t2, {})

        ya1 = tp1.get("yes_ask")
        ya2 = tp2.get("yes_ask")
        if ya1 and ya2 and ya1 > 0 and ya2 > 0:
            _check_pair("ws_cross_yes", event_ticker, t1, t2, "YES", "YES", ya1, ya2)

        na1 = tp1.get("no_ask")
        na2 = tp2.get("no_ask")
        if na1 and na2 and na1 > 0 and na2 > 0:
            _check_pair("ws_cross_no", event_ticker, t1, t2, "NO", "NO", na1, na2)

        for t, tp in [(t1, tp1), (t2, tp2)]:
            ya = tp.get("yes_ask")
            na = tp.get("no_ask")
            if ya and na and ya > 0 and na > 0:
                _check_pair("ws_yes_no", event_ticker, t, t, "YES", "NO", ya, na)

        bid_data = {}
        for t in event_markets:
            tp = ticker_prices.get(t, {})
            yb = tp.get("yes_bid", 0)
            if yb and yb > 0:
                bid_data[t] = {"bid": yb}
        if bid_data:
            exit_result = bot.paper_trader.check_exit_ws(ticker, bid_data)
            if exit_result:
                bot.print_exit(exit_result)

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
    logger.info("Categories: %s", ", ".join(ALLOWED_CATEGORIES))
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
