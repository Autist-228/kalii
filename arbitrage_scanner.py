import logging
from typing import Optional
from itertools import combinations
from db import log_signal

logger = logging.getLogger("arb_scanner")


def get_best_yes_ask(orderbook_no: list) -> Optional[int]:
    if not orderbook_no:
        return None
    best_no_bid = orderbook_no[-1][0]
    return 100 - best_no_bid


def get_best_yes_bid(orderbook_yes: list) -> Optional[int]:
    if not orderbook_yes:
        return None
    return orderbook_yes[-1][0]


def get_best_no_ask(orderbook_yes: list) -> Optional[int]:
    if not orderbook_yes:
        return None
    best_yes_bid = orderbook_yes[-1][0]
    return 100 - best_yes_bid


def get_best_no_bid(orderbook_no: list) -> Optional[int]:
    if not orderbook_no:
        return None
    return orderbook_no[-1][0]


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

    best_yes_ask_price = get_best_yes_ask(no_bids)
    best_no_ask_price = get_best_no_ask(yes_bids)

    if best_yes_ask_price is None or best_no_ask_price is None:
        return None

    total_cost = best_yes_ask_price + best_no_ask_price

    if total_cost >= 100:
        return None

    profit_cents = 100 - total_cost
    arb_percent = (profit_cents / total_cost) * 100

    if arb_percent < min_arb_percent:
        return None

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
        "profit_cents": profit_cents,
        "arb_percent": arb_percent,
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
        arb_profit_cents=profit_cents,
        arb_percent=arb_percent,
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

    if n_markets < 2:
        return signals

    if n_markets == 2:
        m1 = markets_with_orderbooks[0]
        m2 = markets_with_orderbooks[1]
        m1_ob = m1["orderbook"]
        m2_ob = m2["orderbook"]

        m1_yes_ask = get_best_yes_ask(m1_ob.get("no", []))
        m2_yes_ask = get_best_yes_ask(m2_ob.get("no", []))

        if m1_yes_ask is not None and m2_yes_ask is not None:
            total = m1_yes_ask + m2_yes_ask
            if total < 100:
                profit = 100 - total
                arb_pct = (profit / total) * 100
                if arb_pct >= min_arb_percent:
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
                        "profit_cents": profit,
                        "arb_percent": arb_pct,
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
                        arb_profit_cents=profit,
                        arb_percent=arb_pct,
                        arb_type="cross_market_yes",
                    )
                    logger.info(
                        "2-MARKET ARB (YES+YES): %s@%d¢ + %s@%d¢ = %d¢ | profit %d¢ (%.2f%%)",
                        m1["title"], m1_yes_ask, m2["title"], m2_yes_ask, total, profit, arb_pct,
                    )

        m1_no_ask = get_best_no_ask(m1_ob.get("yes", []))
        m2_no_ask = get_best_no_ask(m2_ob.get("yes", []))

        if m1_no_ask is not None and m2_no_ask is not None:
            total = m1_no_ask + m2_no_ask
            if total < 100:
                profit = 100 - total
                arb_pct = (profit / total) * 100
                if arb_pct >= min_arb_percent:
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
                        "profit_cents": profit,
                        "arb_percent": arb_pct,
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
                        arb_profit_cents=profit,
                        arb_percent=arb_pct,
                        arb_type="cross_market_no",
                    )
                    logger.info(
                        "2-MARKET ARB (NO+NO): %s@%d¢ + %s@%d¢ = %d¢ | profit %d¢ (%.2f%%)",
                        m1["title"], m1_no_ask, m2["title"], m2_no_ask, total, profit, arb_pct,
                    )

        return signals

    yes_asks = []
    for m in markets_with_orderbooks:
        ask = get_best_yes_ask(m["orderbook"].get("no", []))
        if ask is not None:
            yes_asks.append((m, ask))

    if len(yes_asks) == n_markets:
        total_all_yes = sum(a for _, a in yes_asks)
        if total_all_yes < 100:
            profit = 100 - total_all_yes
            arb_pct = (profit / total_all_yes) * 100
            if arb_pct >= min_arb_percent:
                detail = " + ".join(
                    f"{m['title']}@{p}¢" for m, p in yes_asks
                )

                sig = {
                    "type": "multi_market_all_yes",
                    "event_ticker": event_ticker,
                    "event_title": event_title,
                    "market1_ticker": "ALL",
                    "market1_title": f"Buy YES on all {n_markets} markets",
                    "market1_price": total_all_yes,
                    "market2_ticker": "ALL",
                    "market2_title": detail,
                    "market2_price": 0,
                    "total_cost": total_all_yes,
                    "profit_cents": profit,
                    "arb_percent": arb_pct,
                }
                signals.append(sig)

                log_signal(
                    event_ticker=event_ticker,
                    event_title=event_title,
                    market1_ticker="ALL",
                    market1_title=f"All {n_markets} YES",
                    market1_yes_price=total_all_yes,
                    market2_ticker="ALL",
                    market2_title=detail,
                    market2_yes_price=0,
                    total_cost_cents=total_all_yes,
                    arb_profit_cents=profit,
                    arb_percent=arb_pct,
                    arb_type="multi_market_all_yes",
                )

                logger.info(
                    "MULTI-MARKET ARB (ALL %d YES): %s = %d¢ | profit %d¢ (%.2f%%)",
                    n_markets, detail, total_all_yes, profit, arb_pct,
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
