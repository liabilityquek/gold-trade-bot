"""XAU_USD instrument metadata.

Gold (XAUUSD) specifics:
- 1 unit = 1 troy ounce
- Price in USD/oz (e.g. 3285.42)
- Pip value: $1.00 per unit (1 point = $1/oz)
- Pip decimal: 2 (prices quoted to 2 decimal places)
- Spread: ~$0.30–$0.50/oz typical on Oanda practice
- Min trade: 1 unit
- No pip multiplier needed (quote currency is already USD)
"""

INSTRUMENT = 'XAU_USD'

INSTRUMENT_INFO = {
    'XAU_USD': {
        'display_name': 'Gold (XAU/USD)',
        'base_currency': 'XAU',
        'quote_currency': 'USD',
        'pip_value': 1.0,          # $1 per unit per point
        'pip_decimal': 2,          # prices to 2 dp (3285.42)
        'min_spread': 0.30,        # typical min spread $/oz
        'max_spread': 0.50,        # typical max spread $/oz (widen during news)
        'news_spread_threshold': 0.60,  # spread above this = no re-entry
        'min_trade_units': 1,
        'max_trade_units': 5000,
        'leverage': 20,
        'market_hours': {
            'open_day': 6,   # Sunday (Mon=0, ... Sun=6)
            'open_hour_utc': 22,
            'close_day': 4,  # Friday
            'close_hour_utc': 21,
        },
    }
}


def get_pip_value(units: int = 1) -> float:
    """Return pip value in USD for given position size."""
    return INSTRUMENT_INFO['XAU_USD']['pip_value'] * units


def fmt_price(price: float) -> str:
    """Format gold price to 2 decimal places."""
    return f"{price:.2f}"
