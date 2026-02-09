import logging
import math
from typing import Optional
from config import TAKER_FEE_RATE
from db import log_signal

logger = logging.getLogger("arb_scanner")


def taker_fee_cents(price_cents: int, contracts: int = 1) -> int:
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    return math.ceil(TAKER_FEE_RATE * contracts * p * (1 - p) * 100)


def get_best_yes_ask(orderbook_no: list) -> Optional[int]:
    if not orderbook_no:
        return None
    best_no_bid = orderbook_no[-1][0]
    return 100 - best_no_bid


def get_best_yes_ask_with_qty(orderbook_no: list) -> tuple[Optional[int], int]:
    if not orderbook_no:
        return None, 0
    best_no_bid = orderbook_no[-1][0]
    qty = orderbook_no[-1][1]
    return 100 - best_no_bid, qty


def get_best_no_ask(orderbook_yes: list) -> Optional[int]:
    if not orderbook_yes:
        return None
    best_yes_bid = orderbook_yes[-1][0]
    return 100 - best_yes_bid


def get_best_no_ask_with_qty(orderbook_yes: list) -> tuple[Optional[int], int]:
    if not orderbook_yes:
        return None, 0
    best_yes_bid = orderbook_yes[-1][0]
    qty = orderbook_yes[-1][1]
    return 100 - best_yes_bid, qty


def check_yes_no_arb_single_market(
    market_ticker: str,
    market_title: str,
    orderbook: dict,
    event_ticker: str,
    event_title: str,
    min_arb_percent: float,
) -> Optional[dict]:
    yes_bids = orderbook.get("yes", [])
    no_bids = orderbook.get("no", [])

    best_yes_ask_price, yes_qty = get_best_yes_ask_with_qty(no_bids)
    best_no_ask_price, no_qty = get_best_no_ask_with_qty(yes_bids)

    if best_yes_ask_price is None or best_no_ask_price is None:
        return None

    if best_yes_ask_price <= 0 or best_no_ask_price <= 0:
        return None

    total_cost = best_yes_ask_price + best_no_ask_price
    if total_cost >= 100:
        return None

    fee1 = taker_fee_cents(best_yes_ask_price)
    fee2 = taker_fee_cents(best_no_ask_price)
    total_fees = fee1 + fee2
    gross_profit = 100 - total_cost
    net_profit = gross_profit - total_fees

    if net_profit <= 0:
        return None

    net_arb_percent = (net_profit / (total_cost + total_fees)) * 100
    if net_arb_percent < min_arb_percent:
        return None

    available_qty = min(yes_qty, no_qty)

    signal = {
        "type": "yes_no_single",
        "event_ticker": event_ticker,
        "event_title": event_title,
        "market1_ticker": market_ticker,
        "market1_title": f"{market_title} YES",
        "market1_price": best_yes_ask_price,
        "market2_ticker": market_ticker,
        "market2_title": f"{market_title} NO",
        "market2_price": best_no_ask_price,
        "total_cost": total_cost,
        "total_fees": total_fees,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "net_arb_percent": net_arb_percent,
        "profit_cents": net_profit,
        "arb_percent": net_arb_percent,
        "available_qty": available_qty,
    }

    log_signal(
        event_ticker=event_ticker,
        event_title=event_title,
        market1_ticker=market_ticker,
        market1_title=f"{market_title} YES",
        market1_yes_price=best_yes_ask_price,
        market2_ticker=market_ticker,
        market2_title=f"{market_title} NO",
        market2_yes_price=best_no_ask_price,
        total_cost_cents=total_cost,
        arb_profit_cents=net_profit,
        arb_percent=net_arb_percent,
        arb_type="yes_no_single",
    )

    return signal


def check_cross_market_arb(
    markets_with_orderbooks: list,
    event_ticker: str,
    event_title: str,
    min_arb_percent: float,
) -> list:
    signals = []
    n_markets = len(markets_with_orderbooks)

    if n_markets != 2:
        return signals

    m1 = markets_with_orderbooks[0]
    m2 = markets_with_orderbooks[1]
    m1_ob = m1["orderbook"]
    m2_ob = m2["orderbook"]

    m1_yes_ask, m1_qty = get_best_yes_ask_with_qty(m1_ob.get("no", []))
    m2_yes_ask, m2_qty = get_best_yes_ask_with_qty(m2_ob.get("no", []))

    if m1_yes_ask is not None and m2_yes_ask is not None:
        if m1_yes_ask > 0 and m2_yes_ask > 0:
            total = m1_yes_ask + m2_yes_ask
            if total < 100:
                fee1 = taker_fee_cents(m1_yes_ask)
                fee2 = taker_fee_cents(m2_yes_ask)
                total_fees = fee1 + fee2
                gross = 100 - total
                net = gross - total_fees
                if net > 0:
                    net_pct = (net / (total + total_fees)) * 100
                    if net_pct >= min_arb_percent:
                        available_qty = min(m1_qty, m2_qty)
                        sig = {
                            "type": "cross_market_yes",
                            "event_ticker": event_ticker,
                            "event_title": event_title,
                            "market1_ticker": m1["ticker"],
                            "market1_title": f"{m1['title']} YES",
                            "market1_price": m1_yes_ask,
                            "market2_ticker": m2["ticker"],
                            "market2_title": f"{m2['title']} YES",
                            "market2_price": m2_yes_ask,
                            "total_cost": total,
                            "total_fees": total_fees,
                            "gross_profit": gross,
                            "net_profit": net,
                            "net_arb_percent": net_pct,
                            "profit_cents": net,
                            "arb_percent": net_pct,
                            "available_qty": available_qty,
                        }
                        signals.append(sig)
                        log_signal(
                            event_ticker=event_ticker,
                            event_title=event_title,
                            market1_ticker=m1["ticker"],
                            market1_title=f"{m1['title']} YES",
                            market1_yes_price=m1_yes_ask,
                            market2_ticker=m2["ticker"],
                            market2_title=f"{m2['title']} YES",
                            market2_yes_price=m2_yes_ask,
                            total_cost_cents=total,
                            arb_profit_cents=net,
                            arb_percent=net_pct,
                            arb_type="cross_market_yes",
                        )
                        logger.info(
                            "ARB YES+YES: %s@%d¢ + %s@%d¢ = %d¢ | fees %d¢ | net %d¢ (%.2f%%) | qty %d",
                            m1["title"], m1_yes_ask, m2["title"], m2_yes_ask,
                            total, total_fees, net, net_pct, available_qty,
                        )

    m1_no_ask, m1_nq = get_best_no_ask_with_qty(m1_ob.get("yes", []))
    m2_no_ask, m2_nq = get_best_no_ask_with_qty(m2_ob.get("yes", []))

    if m1_no_ask is not None and m2_no_ask is not None:
        if m1_no_ask > 0 and m2_no_ask > 0:
            total = m1_no_ask + m2_no_ask
            if total < 100:
                fee1 = taker_fee_cents(m1_no_ask)
                fee2 = taker_fee_cents(m2_no_ask)
                total_fees = fee1 + fee2
                gross = 100 - total
                net = gross - total_fees
                if net > 0:
                    net_pct = (net / (total + total_fees)) * 100
                    if net_pct >= min_arb_percent:
                        available_qty = min(m1_nq, m2_nq)
                        sig = {
                            "type": "cross_market_no",
                            "event_ticker": event_ticker,
                            "event_title": event_title,
                            "market1_ticker": m1["ticker"],
                            "market1_title": f"{m1['title']} NO",
                            "market1_price": m1_no_ask,
                            "market2_ticker": m2["ticker"],
                            "market2_title": f"{m2['title']} NO",
                            "market2_price": m2_no_ask,
                            "total_cost": total,
                            "total_fees": total_fees,
                            "gross_profit": gross,
                            "net_profit": net,
                            "net_arb_percent": net_pct,
                            "profit_cents": net,
                            "arb_percent": net_pct,
                            "available_qty": available_qty,
                        }
                        signals.append(sig)
                        log_signal(
                            event_ticker=event_ticker,
                            event_title=event_title,
                            market1_ticker=m1["ticker"],
                            market1_title=f"{m1['title']} NO",
                            market1_yes_price=m1_no_ask,
                            market2_ticker=m2["ticker"],
                            market2_title=f"{m2['title']} NO",
                            market2_yes_price=m2_no_ask,
                            total_cost_cents=total,
                            arb_profit_cents=net,
                            arb_percent=net_pct,
                            arb_type="cross_market_no",
                        )
                        logger.info(
                            "ARB NO+NO: %s@%d¢ + %s@%d¢ = %d¢ | fees %d¢ | net %d¢ (%.2f%%) | qty %d",
                            m1["title"], m1_no_ask, m2["title"], m2_no_ask,
                            total, total_fees, net, net_pct, available_qty,
                        )

    return signals


def scan_event_for_arb(
    event_ticker: str,
    event_title: str,
    markets_with_orderbooks: list,
    min_arb_percent: float,
) -> list:
    all_signals = []

    for m in markets_with_orderbooks:
        sig = check_yes_no_arb_single_market(
            market_ticker=m["ticker"],
            market_title=m["title"],
            orderbook=m["orderbook"],
            event_ticker=event_ticker,
            event_title=event_title,
            min_arb_percent=min_arb_percent,
        )
        if sig:
            all_signals.append(sig)

    cross_signals = check_cross_market_arb(
        markets_with_orderbooks=markets_with_orderbooks,
        event_ticker=event_ticker,
        event_title=event_title,
        min_arb_percent=min_arb_percent,
    )
    all_signals.extend(cross_signals)

    return all_signals
