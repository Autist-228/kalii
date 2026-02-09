import datetime
import logging
import sqlite3
from typing import Optional
from arbitrage_scanner import taker_fee_cents
from config import BANKROLL_CENTS, BET_PERCENT
from db import get_connection

logger = logging.getLogger("paper_trader")


class PaperTrader:
    def __init__(self, initial_balance: int = BANKROLL_CENTS):
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.open_positions: list[dict] = []
        self.closed_positions: list[dict] = []
        self.total_trades = 0
        self.total_profit = 0
        self.total_fees_paid = 0
        self.skipped_no_balance = 0
        self.skipped_no_liquidity = 0
        self.skipped_duplicate = 0

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
            logger.warning("Skipping: no contracts available (budget=%d¢, cost=%d¢/c, liq=%d)",
                           bet_size, cost_with_fees, available_qty)
            return None

        total_investment = contracts * total_cost_per
        total_fees = contracts * total_fees_per
        total_net_profit = contracts * net_profit_per

        self.balance -= (total_investment + total_fees)
        self.total_fees_paid += total_fees
        self.total_trades += 1

        position = {
            "id": self.total_trades,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "event_ticker": event_ticker,
            "event_title": signal.get("event_title", ""),
            "arb_type": signal["type"],
            "leg1_ticker": signal["market1_ticker"],
            "leg1_side": "yes" if "YES" in signal.get("market1_title", "") else "no",
            "leg1_price": signal["market1_price"],
            "leg2_ticker": signal["market2_ticker"],
            "leg2_side": "yes" if "YES" in signal.get("market2_title", "") else "no",
            "leg2_price": signal["market2_price"],
            "contracts": contracts,
            "total_cost": total_investment,
            "total_fees": total_fees,
            "net_profit_expected": total_net_profit,
            "net_arb_percent": signal.get("net_arb_percent", signal.get("arb_percent", 0)),
            "status": "open",
        }

        self.open_positions.append(position)
        self._log_paper_trade(position)

        logger.info(
            "PAPER BUY: %s | %d contracts | cost %d¢ + %d¢ fees | expected profit %d¢ | balance %d¢",
            event_ticker, contracts, total_investment, total_fees,
            total_net_profit, self.balance,
        )

        return position

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

        net_sell_total = net_sell_per * contracts
        exit_profit_total = exit_profit_per * contracts
        sell_fees_total = total_sell_fees_per * contracts

        self.balance += net_sell_total
        self.total_profit += exit_profit_total
        self.total_fees_paid += sell_fees_total

        position["status"] = "sold"
        position["exit_profit"] = exit_profit_total
        position["exit_type"] = "paired_sell"
        position["exit_timestamp"] = datetime.datetime.utcnow().isoformat()
        position["sell_leg1_price"] = leg1_bid
        position["sell_leg2_price"] = leg2_bid
        position["sell_fees"] = sell_fees_total

        self.open_positions.remove(position)
        self.closed_positions.append(position)

        self._update_paper_trade_exit(position)

        logger.info(
            "PAPER SELL: %s | %d contracts | sell %d¢+%d¢ - %d¢ fees = %d¢ | profit +%d¢ | balance %d¢",
            position["event_ticker"], contracts,
            leg1_bid, leg2_bid, sell_fees_total, net_sell_total,
            exit_profit_total, self.balance,
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

    def settle_position(self, event_ticker: str, winning_ticker: str):
        positions_to_close = [p for p in self.open_positions if p["event_ticker"] == event_ticker]
        for pos in positions_to_close:
            payout = pos["contracts"] * 100
            profit = payout - pos["total_cost"] - pos["total_fees"]
            pos["status"] = "settled"
            pos["exit_profit"] = profit
            pos["exit_type"] = "settlement"
            pos["exit_timestamp"] = datetime.datetime.utcnow().isoformat()

            self.balance += payout
            self.total_profit += profit
            self.open_positions.remove(pos)
            self.closed_positions.append(pos)

            self._update_paper_trade_exit(pos)

            logger.info(
                "SETTLED: %s | payout %d¢ | profit %d¢ | balance %d¢",
                event_ticker, payout, profit, self.balance,
            )

    def get_summary(self) -> dict:
        return {
            "initial_balance": self.initial_balance,
            "current_balance": self.balance,
            "pnl": self.balance - self.initial_balance + sum(
                p["contracts"] * 100 for p in self.open_positions
            ),
            "unrealized_pnl": sum(
                p["net_profit_expected"] for p in self.open_positions
            ),
            "total_trades": self.total_trades,
            "open_positions": len(self.open_positions),
            "closed_positions": len(self.closed_positions),
            "total_fees_paid": self.total_fees_paid,
            "total_realized_profit": self.total_profit,
            "skipped_no_balance": self.skipped_no_balance,
            "skipped_no_liquidity": self.skipped_no_liquidity,
            "skipped_duplicate": self.skipped_duplicate,
            "locked_in_positions": sum(
                p["total_cost"] + p["total_fees"] for p in self.open_positions
            ),
        }

    def print_summary(self):
        s = self.get_summary()
        print("\n" + "=" * 60)
        print(f"{'PAPER TRADING SUMMARY':^60}")
        print("=" * 60)
        print(f"  Initial balance:    {s['initial_balance']}¢ (${s['initial_balance']/100:.2f})")
        print(f"  Current balance:    {s['current_balance']}¢ (${s['current_balance']/100:.2f})")
        print(f"  Locked in trades:   {s['locked_in_positions']}¢ (${s['locked_in_positions']/100:.2f})")
        print(f"  Unrealized P&L:     {s['unrealized_pnl']}¢ (${s['unrealized_pnl']/100:.2f})")
        print(f"  Realized P&L:       {s['total_realized_profit']}¢ (${s['total_realized_profit']/100:.2f})")
        print(f"  Total fees paid:    {s['total_fees_paid']}¢ (${s['total_fees_paid']/100:.2f})")
        print(f"  Total trades:       {s['total_trades']}")
        print(f"  Open positions:     {s['open_positions']}")
        print(f"  Closed positions:   {s['closed_positions']}")
        print(f"  Skipped (no $):     {s['skipped_no_balance']}")
        print(f"  Skipped (no liq):   {s['skipped_no_liquidity']}")
        print(f"  Skipped (dup):      {s['skipped_duplicate']}")
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

    def _update_paper_trade_exit(self, position: dict):
        try:
            conn = get_connection()
            conn.execute("""
                UPDATE paper_trades
                SET status = ?, exit_timestamp = ?, exit_profit_cents = ?, exit_type = ?
                WHERE event_ticker = ? AND status = 'open'
                ORDER BY id DESC LIMIT 1
            """, (
                position["status"],
                position.get("exit_timestamp", ""),
                position.get("exit_profit", 0),
                position.get("exit_type", ""),
                position["event_ticker"],
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to update paper trade exit: %s", e)

    def _log_paper_trade(self, position: dict):
        try:
            conn = get_connection()
            conn.execute("""
                INSERT INTO paper_trades
                (timestamp, event_ticker, event_title, arb_type,
                 leg1_ticker, leg1_side, leg1_price, leg1_qty, leg1_fee,
                 leg2_ticker, leg2_side, leg2_price, leg2_qty, leg2_fee,
                 total_cost_cents, total_fees_cents, net_profit_cents,
                 net_arb_percent, contracts, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position["timestamp"],
                position["event_ticker"],
                position["event_title"],
                position["arb_type"],
                position["leg1_ticker"],
                position["leg1_side"],
                position["leg1_price"],
                position["contracts"],
                position["total_fees"] // 2,
                position["leg2_ticker"],
                position["leg2_side"],
                position["leg2_price"],
                position["contracts"],
                position["total_fees"] - position["total_fees"] // 2,
                position["total_cost"],
                position["total_fees"],
                position["net_profit_expected"],
                position["net_arb_percent"],
                position["contracts"],
                "open",
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to log paper trade: %s", e)
