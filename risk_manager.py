"""
risk_manager.py — Position sizing for 1% risk per trade on XAU_USD.
XAU_USD on Oanda: 1 unit = 1 troy ounce. P&L in account currency (USD).
"""


def calculate_units(account_nav: float, entry_price: float, sl_price: float,
                    risk_pct: float = 0.01) -> int:
    """
    Calculate units to trade to risk exactly risk_pct of NAV.

    risk_amount = NAV * risk_pct
    pip_distance = |entry - sl|   (in USD/oz, since XAU_USD quote is USD)
    units = risk_amount / pip_distance

    Returns integer units (minimum 1).
    """
    if entry_price <= 0 or sl_price <= 0:
        raise ValueError("Entry and SL prices must be positive")

    risk_amount  = account_nav * risk_pct
    pip_distance = abs(entry_price - sl_price)

    if pip_distance < 0.01:
        raise ValueError(f"SL distance too tight ({pip_distance:.4f}) — likely a parsing error")

    units = risk_amount / pip_distance
    return max(1, int(units))


def format_risk_summary(account_nav, entry_price, sl_price, units, direction, tp1, tp2=None, tp3=None):
    pip_risk = abs(entry_price - sl_price)
    risk_usd = units * pip_risk
    rr1 = abs(tp1 - entry_price) / pip_risk if pip_risk else 0
    rr2 = abs(tp2 - entry_price) / pip_risk if tp2 and pip_risk else None
    rr3 = abs(tp3 - entry_price) / pip_risk if tp3 and pip_risk else None

    lines = [
        f"Direction : {direction}",
        f"Entry     : {entry_price:.2f}",
        f"SL        : {sl_price:.2f} ({pip_risk:.2f} pts)",
        f"TP1       : {tp1:.2f} (R:R {rr1:.2f})",
    ]
    if tp2:
        lines.append(f"TP2       : {tp2:.2f} (R:R {rr2:.2f})")
    if tp3:
        lines.append(f"TP3       : {tp3:.2f} (R:R {rr3:.2f})")
    lines += [
        f"Units     : {units} oz",
        f"Risk      : ${risk_usd:.2f} (1% of ${account_nav:,.2f} NAV)",
    ]
    return "\n".join(lines)


MAX_CONCURRENT_TRADES = 2
MAX_DAILY_LOSS_PCT    = 0.03   # Stop trading if daily loss exceeds 3% NAV


def within_risk_limits(open_trades: list, daily_loss_pct: float) -> tuple[bool, str]:
    if len(open_trades) >= MAX_CONCURRENT_TRADES:
        return False, f"Max concurrent trades reached ({MAX_CONCURRENT_TRADES})"
    if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
        return False, f"Daily loss limit hit ({daily_loss_pct*100:.1f}% >= {MAX_DAILY_LOSS_PCT*100:.0f}%)"
    return True, "ok"
