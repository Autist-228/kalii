import datetime
import logging
import time
from typing import Optional
from arbitrage_scanner import taker_fee_cents
from config import BET_PERCENT
from kalshi_client import KalshiClient

logger = logging.getLogger("live_trader")

EVENT_COOLDOWN_SEC = 60
ORDER_MONITOR_INTERVAL = 3
ORDER_TIMEOUT_SEC = 60


class LiveTrader:
    def __init__(self, client: KalshiClient):
        self.client = client
        self.balance = 0
        self.initial_balance = 0
        self.open_positions: list[dict] = []
        self.closed_positions: list[dict] = []
        self.total_trades = 0
        self.total_profit = 0
        self.total_fees_paid = 0
        self.skipped_no_balance = 0
        self.skipped_no_liquidity = 0
        self.skipped_duplicate = 0
        self._attempted_events: dict[str, float] = {}
        self._refresh_balance()

    def _refresh_balance(self):
        try:
            data = self.client.get_balance()
            self.balance = data.get("balance", 0)
            if self.initial_balance == 0:
                self.initial_balance = self.balance
            logger.info("Balance: %d¢ ($%.2f)", self.balance, self.balance / 100)
        except Exception as e:
            logger.error("Failed to refresh balance: %s", e)

    def get_bet_size(self) -> int:
        return self.balance * BET_PERCENT // 100

    def has_open_position(self, event_ticker: str) -> bool:
        for pos in self.open_positions:
            if pos["event_ticker"] == event_ticker:
                return True
        return False

    def execute_paper_trade(self, signal: dict) -> Optional[dict]:
        event_ticker = signal["event_ticker"]

        if self.has_open_position(event_ticker):
            self.skipped_duplicate += 1
            return None

        now = time.time()
        last_attempt = self._attempted_events.get(event_ticker, 0)
        if now - last_attempt < EVENT_COOLDOWN_SEC:
            return None
        self._attempted_events[event_ticker] = now

        self._refresh_balance()
        bet_size = self.get_bet_size()
        if bet_size < 100:
            self.skipped_no_balance += 1
            logger.warning("Skipping: bet size %d¢ too small (balance %d¢)", bet_size, self.balance)
            return None

        total_cost_per = signal["total_cost"]
        total_fees_per = signal.get("total_fees", 0)
        net_profit_per = signal.get("net_profit", signal["profit_cents"])
        available_qty = signal.get("available_qty", 0)

        if total_cost_per <= 0:
            return None

        cost_with_fees = total_cost_per + total_fees_per
        max_contracts_by_budget = bet_size // cost_with_fees
        max_contracts_by_liquidity = available_qty if available_qty > 0 else 999
        contracts = min(max_contracts_by_budget, max_contracts_by_liquidity)

        if contracts <= 0:
            self.skipped_no_liquidity += 1
            return None

        leg1_side = "yes" if "YES" in signal.get("market1_title", "") else "no"
        leg2_side = "yes" if "YES" in signal.get("market2_title", "") else "no"
        leg1_ticker = signal["market1_ticker"]
        leg2_ticker = signal["market2_ticker"]
        leg1_price = signal["market1_price"]
        leg2_price = signal["market2_price"]

        logger.info("PLACING BOTH LEGS as GTC resting orders:")
        logger.info("  LEG 1: %s %s BUY @ %d¢ x%d",
                     leg1_ticker, leg1_side.upper(), leg1_price, contracts)
        logger.info("  LEG 2: %s %s BUY @ %d¢ x%d",
                     leg2_ticker, leg2_side.upper(), leg2_price, contracts)

        leg1_order = {}
        leg2_order = {}
        leg1_order_id = ""
        leg2_order_id = ""

        try:
            leg1_resp = self.client.place_order(
                ticker=leg1_ticker,
                side=leg1_side,
                action="buy",
                count=contracts,
                price=leg1_price,
                time_in_force="good_till_canceled",
            )
            leg1_order = leg1_resp.get("order", {})
            leg1_order_id = leg1_order.get("order_id", "")
            logger.info("LEG 1 PLACED: id=%s status=%s filled=%d/%d",
                         leg1_order_id, leg1_order.get("status", ""),
                         leg1_order.get("fill_count", 0), contracts)
        except Exception as e:
            logger.error("LEG 1 PLACE FAILED: %s", e)
            return None

        try:
            leg2_resp = self.client.place_order(
                ticker=leg2_ticker,
                side=leg2_side,
                action="buy",
                count=contracts,
                price=leg2_price,
                time_in_force="good_till_canceled",
            )
            leg2_order = leg2_resp.get("order", {})
            leg2_order_id = leg2_order.get("order_id", "")
            logger.info("LEG 2 PLACED: id=%s status=%s filled=%d/%d",
                         leg2_order_id, leg2_order.get("status", ""),
                         leg2_order.get("fill_count", 0), contracts)
        except Exception as e:
            logger.error("LEG 2 PLACE FAILED: %s — canceling leg 1", e)
            self._cancel_order_safe(leg1_order_id)
            return None

        leg1_filled = leg1_order.get("fill_count", 0)
        leg2_filled = leg2_order.get("fill_count", 0)

        if leg1_filled >= contracts and leg2_filled >= contracts:
            logger.info("BOTH LEGS FILLED INSTANTLY!")
        else:
            leg1_filled, leg2_filled = self._monitor_orders(
                leg1_order_id, leg2_order_id, contracts
            )

        if leg1_filled == 0 and leg2_filled == 0:
            logger.warning("NEITHER LEG FILLED after %ds — canceling both", ORDER_TIMEOUT_SEC)
            self._cancel_order_safe(leg1_order_id)
            self._cancel_order_safe(leg2_order_id)
            self.skipped_no_liquidity += 1
            return None

        matched = min(leg1_filled, leg2_filled)

        if matched == 0:
            if leg1_filled > 0:
                logger.warning("ONLY LEG 1 FILLED (%d) — canceling leg 2, emergency selling leg 1", leg1_filled)
                self._cancel_order_safe(leg2_order_id)
                self._emergency_sell(leg1_ticker, leg1_side, leg1_filled)
            else:
                logger.warning("ONLY LEG 2 FILLED (%d) — canceling leg 1, emergency selling leg 2", leg2_filled)
                self._cancel_order_safe(leg1_order_id)
                self._emergency_sell(leg2_ticker, leg2_side, leg2_filled)
            return None

        if leg1_filled > matched:
            excess = leg1_filled - matched
            logger.warning("LEG 1 has %d excess — selling", excess)
            self._emergency_sell(leg1_ticker, leg1_side, excess)
        if leg2_filled > matched:
            excess = leg2_filled - matched
            logger.warning("LEG 2 has %d excess — selling", excess)
            self._emergency_sell(leg2_ticker, leg2_side, excess)

        if leg1_filled < contracts:
            self._cancel_order_safe(leg1_order_id)
        if leg2_filled < contracts:
            self._cancel_order_safe(leg2_order_id)

        leg1_order_final = self._get_order_safe(leg1_order_id)
        leg2_order_final = self._get_order_safe(leg2_order_id)
        leg1_fees = leg1_order_final.get("taker_fees", 0)
        leg2_fees = leg2_order_final.get("taker_fees", 0)
        total_fees = leg1_fees + leg2_fees
        leg1_cost = leg1_order_final.get("taker_fill_cost", leg1_price * matched)
        leg2_cost = leg2_order_final.get("taker_fill_cost", leg2_price * matched)
        total_cost = leg1_cost + leg2_cost

        self._refresh_balance()
        self.total_trades += 1
        self.total_fees_paid += total_fees

        position = {
            "id": self.total_trades,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "event_ticker": event_ticker,
            "event_title": signal.get("event_title", ""),
            "arb_type": signal["type"],
            "leg1_ticker": leg1_ticker,
            "leg1_side": leg1_side,
            "leg1_price": leg1_price,
            "leg1_order_id": leg1_order_id,
            "leg2_ticker": leg2_ticker,
            "leg2_side": leg2_side,
            "leg2_price": leg2_price,
            "leg2_order_id": leg2_order_id,
            "contracts": matched,
            "total_cost": total_cost,
            "total_fees": total_fees,
            "net_profit_expected": matched * net_profit_per,
            "net_arb_percent": signal.get("net_arb_percent", 0),
            "status": "open",
        }

        self.open_positions.append(position)

        logger.info(
            "LIVE BUY OK: %s | %d contracts | cost %d¢ + %d¢ fees | expected +%d¢ | balance %d¢",
            event_ticker, matched, total_cost, total_fees,
            position["net_profit_expected"], self.balance,
        )

        return position

    def _monitor_orders(
        self, leg1_id: str, leg2_id: str, target: int
    ) -> tuple[int, int]:
        start = time.time()
        leg1_filled = 0
        leg2_filled = 0
        while time.time() - start < ORDER_TIMEOUT_SEC:
            time.sleep(ORDER_MONITOR_INTERVAL)
            try:
                o1 = self._get_order_safe(leg1_id)
                o2 = self._get_order_safe(leg2_id)
                leg1_filled = o1.get("fill_count", 0)
                leg2_filled = o2.get("fill_count", 0)
                elapsed = int(time.time() - start)
                logger.info("ORDER MONITOR [%ds]: leg1 %d/%d  leg2 %d/%d",
                             elapsed, leg1_filled, target, leg2_filled, target)
                if leg1_filled >= target and leg2_filled >= target:
                    logger.info("BOTH LEGS FULLY FILLED!")
                    return leg1_filled, leg2_filled
            except Exception as e:
                logger.error("Monitor poll error: %s", e)
        return leg1_filled, leg2_filled

    def _get_order_safe(self, order_id: str) -> dict:
        if not order_id:
            return {}
        try:
            data = self.client.get_order(order_id)
            return data.get("order", {})
        except Exception as e:
            logger.error("get_order(%s) failed: %s", order_id, e)
            return {}

    def _cancel_order_safe(self, order_id: str):
        if not order_id:
            return
        try:
            self.client.cancel_order(order_id)
            logger.info("Canceled order %s", order_id)
        except Exception as e:
            logger.warning("Cancel order %s failed (may already be filled/canceled): %s", order_id, e)

    def _emergency_sell(self, ticker: str, side: str, count: int):
        logger.warning("EMERGENCY SELL: %s %s x%d", ticker, side, count)
        try:
            price = 1
            result = self.client.place_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=count,
                price=price,
                time_in_force="immediate_or_cancel",
            )
            order = result.get("order", {})
            logger.info("EMERGENCY SELL: status=%s filled=%d",
                         order.get("status", ""), order.get("fill_count", 0))
        except Exception as e:
            logger.error("EMERGENCY SELL FAILED: %s — MANUAL ACTION NEEDED", e)

    def try_paired_exit(self, position: dict, leg1_bid: int, leg2_bid: int) -> Optional[dict]:
        if leg1_bid <= 0 or leg2_bid <= 0:
            return None

        contracts = position["contracts"]
        sell_revenue_per = leg1_bid + leg2_bid
        sell_fee1 = taker_fee_cents(leg1_bid)
        sell_fee2 = taker_fee_cents(leg2_bid)
        total_sell_fees_per = sell_fee1 + sell_fee2

        net_sell_per = sell_revenue_per - total_sell_fees_per
        buy_cost_per = (position["total_cost"] + position["total_fees"]) // contracts

        exit_profit_per = net_sell_per - buy_cost_per
        if exit_profit_per < 0:
            return None

        logger.info("EXIT OPPORTUNITY: %s | sell %d¢+%d¢ - %d¢ fees = +%d¢/c",
                     position["event_ticker"], leg1_bid, leg2_bid,
                     total_sell_fees_per, exit_profit_per)

        try:
            sell1 = self.client.place_order(
                ticker=position["leg1_ticker"],
                side=position["leg1_side"],
                action="sell",
                count=contracts,
                price=leg1_bid,
            )
            s1_order = sell1.get("order", {})
            s1_filled = s1_order.get("fill_count", 0)

            if s1_filled == 0:
                logger.warning("SELL LEG 1 not filled — skip exit")
                return None

            sell2 = self.client.place_order(
                ticker=position["leg2_ticker"],
                side=position["leg2_side"],
                action="sell",
                count=s1_filled,
                price=leg2_bid,
            )
            s2_order = sell2.get("order", {})
            s2_filled = s2_order.get("fill_count", 0)

            if s2_filled < s1_filled:
                logger.warning("SELL LEG 2 partial: %d/%d", s2_filled, s1_filled)

        except Exception as e:
            logger.error("SELL FAILED: %s", e)
            return None

        self._refresh_balance()

        actual_sell_fees = (s1_order.get("taker_fees", 0) + s2_order.get("taker_fees", 0))
        sold_contracts = min(s1_filled, s2_filled)
        exit_profit_total = exit_profit_per * sold_contracts

        position["status"] = "sold"
        position["exit_profit"] = exit_profit_total
        position["exit_type"] = "paired_sell"
        position["exit_timestamp"] = datetime.datetime.utcnow().isoformat()
        position["sell_leg1_price"] = leg1_bid
        position["sell_leg2_price"] = leg2_bid
        position["sell_fees"] = actual_sell_fees

        self.open_positions.remove(position)
        self.closed_positions.append(position)
        self.total_profit += exit_profit_total
        self.total_fees_paid += actual_sell_fees

        logger.info(
            "LIVE SELL OK: %s | %d contracts | sell %d¢+%d¢ | profit +%d¢ | balance %d¢",
            position["event_ticker"], sold_contracts,
            leg1_bid, leg2_bid, exit_profit_total, self.balance,
        )

        return position

    def check_exits_with_orderbooks(self, orderbooks: dict) -> list[dict]:
        exits = []
        for pos in list(self.open_positions):
            leg1_ob = orderbooks.get(pos["leg1_ticker"], {})
            leg2_ob = orderbooks.get(pos["leg2_ticker"], {})

            if pos["leg1_side"] == "yes":
                leg1_bids = leg1_ob.get("yes", [])
            else:
                leg1_bids = leg1_ob.get("no", [])

            if pos["leg2_side"] == "yes":
                leg2_bids = leg2_ob.get("yes", [])
            else:
                leg2_bids = leg2_ob.get("no", [])

            leg1_bid = leg1_bids[-1][0] if leg1_bids else 0
            leg2_bid = leg2_bids[-1][0] if leg2_bids else 0

            result = self.try_paired_exit(pos, leg1_bid, leg2_bid)
            if result:
                exits.append(result)

        return exits

    def check_exit_ws(self, ticker: str, bid_prices: dict) -> Optional[dict]:
        for pos in list(self.open_positions):
            if pos["leg1_ticker"] != ticker and pos["leg2_ticker"] != ticker:
                continue

            leg1_bid = bid_prices.get(pos["leg1_ticker"], {}).get("bid", 0)
            leg2_bid = bid_prices.get(pos["leg2_ticker"], {}).get("bid", 0)

            if leg1_bid <= 0 or leg2_bid <= 0:
                continue

            result = self.try_paired_exit(pos, leg1_bid, leg2_bid)
            if result:
                return result

        return None

    def print_summary(self):
        self._refresh_balance()
        locked = sum(p["total_cost"] + p["total_fees"] for p in self.open_positions)
        unrealized = sum(p["net_profit_expected"] for p in self.open_positions)

        print("\n" + "=" * 60)
        print(f"{'LIVE TRADING SUMMARY':^60}")
        print("=" * 60)
        print(f"  Initial balance:    {self.initial_balance}¢ (${self.initial_balance/100:.2f})")
        print(f"  Current balance:    {self.balance}¢ (${self.balance/100:.2f})")
        print(f"  Locked in trades:   {locked}¢ (${locked/100:.2f})")
        print(f"  Unrealized P&L:     {unrealized}¢ (${unrealized/100:.2f})")
        print(f"  Realized P&L:       {self.total_profit}¢ (${self.total_profit/100:.2f})")
        print(f"  Total fees paid:    {self.total_fees_paid}¢ (${self.total_fees_paid/100:.2f})")
        print(f"  Total trades:       {self.total_trades}")
        print(f"  Open positions:     {len(self.open_positions)}")
        print(f"  Closed positions:   {len(self.closed_positions)}")
        print(f"  Skipped (no $):     {self.skipped_no_balance}")
        print(f"  Skipped (no liq):   {self.skipped_no_liquidity}")
        print(f"  Skipped (dup):      {self.skipped_duplicate}")
        print("=" * 60)

        if self.open_positions:
            print(f"\n{'OPEN POSITIONS':^60}")
            print("-" * 60)
            for pos in self.open_positions:
                print(f"  #{pos['id']} {pos['event_title'][:40]}")
                print(f"    {pos['leg1_ticker']} {pos['leg1_side']}@{pos['leg1_price']}¢")
                print(f"    {pos['leg2_ticker']} {pos['leg2_side']}@{pos['leg2_price']}¢")
                print(f"    {pos['contracts']} contracts | cost {pos['total_cost']}¢ | fees {pos['total_fees']}¢")
                print(f"    Expected profit: +{pos['net_profit_expected']}¢")
            print("-" * 60)
        print()
