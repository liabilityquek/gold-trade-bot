"""Entry point for the multi-agent gold trading bot (XAU/USD).

Usage:
    python src/main.py --test              # Component check, print DecisionResult, no trades
    python src/main.py --live              # Full trading loop
    python src/main.py --live --dry-run   # Full loop but skip actual order placement
    python src/main.py --live --interval 3600      # Override cycle interval (seconds)
    python src/main.py --live --cycle 1 --dry-run  # Single cycle
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from src.monitoring.logger import get_logger
from src.monitoring.alerts import AlertManager
from src.broker.oanda import OandaBroker
from src.risk.kill_switch import KillSwitch
from src.voting.engine import DecisionEngine
from src.execution.engine import TradingEngine
from src.news.event_monitor import EventMonitor, EventImpact
from src.news.news_watcher import NewsWatcher

_INSTRUMENT = 'XAU_USD'


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-agent gold trading bot (XAU/USD)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test", action="store_true", help="Component test mode")
    mode.add_argument("--live", action="store_true", help="Live trading loop")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual order placement")
    parser.add_argument("--interval", type=int, default=None, help="Cycle interval in seconds")
    parser.add_argument("--cycle", type=int, default=None, help="Stop after N cycles")
    return parser.parse_args()


def run_test(broker: OandaBroker, decision_engine: DecisionEngine, logger) -> bool:
    """Verify components and print DecisionResult for XAU/USD. No trades placed."""
    logger.info("=" * 60)
    logger.info("TEST MODE — gold bot component verification")
    logger.info("=" * 60)

    if not settings.validate():
        logger.error("Settings validation failed")
        return False
    logger.info("Settings: OK")

    if not broker.connect():
        logger.error("Broker connection failed")
        return False
    logger.info("Broker: connected")

    account = broker.get_account_info()
    if not account:
        logger.error("Could not fetch account info")
        return False
    logger.info(f"Account: balance=${account.balance:.2f} | NAV=${account.nav:.2f}")

    logger.info(f"\n--- {_INSTRUMENT} ---")
    try:
        candles = broker.get_historical_candles(
            _INSTRUMENT, granularity=settings.TIMEFRAME, count=settings.CANDLE_COUNT
        )
        if not candles:
            logger.warning(f"{_INSTRUMENT}: no candle data")
            return False

        price_info = broker.get_current_price(_INSTRUMENT)
        if not price_info:
            logger.warning(f"{_INSTRUMENT}: no price data")
            return False

        price = (price_info['bid'] + price_info['ask']) / 2

        # Fetch M15 candles — only used by the downstream momentum veto
        htf_candles: dict = {}
        try:
            m15_data = broker.get_historical_candles(
                _INSTRUMENT, granularity='M15', count=settings.M15_CANDLE_COUNT
            ) or []
            if m15_data:
                htf_candles['M15'] = m15_data
        except Exception:
            pass

        result = decision_engine.run_decision(_INSTRUMENT, candles, price, htf_candles=htf_candles)

        print(f"\n{_INSTRUMENT} DecisionResult:")
        print(f"  Final signal    : {result.final_signal.value}")
        print(f"  Confidence      : {result.confidence:.4f}")
        print(f"  Setup type      : {result.setup_type}")
        print(f"  Confluences     : {result.confluence_count}/{settings.MIN_CONFLUENCES} "
              f"[{', '.join(result.confluence_types)}]")
        print(f"  Reasoning       : {result.llm_reasoning}")
        print(f"  Current price   : {price:.2f} USD/oz")

        # Print key indicators
        ind = result.indicators
        trend_signal = ind.get('trend_signal', 'N/A')
        adx_val      = ind.get('adx', 'N/A')
        rsi_val      = ind.get('rsi', 'N/A')
        atr_val      = ind.get('atr', 'N/A')
        trend        = ind.get('trend', 'N/A')
        di_plus      = ind.get('di_plus', 'N/A')
        di_minus     = ind.get('di_minus', 'N/A')

        print(f"\n  Trend Analysis (H1):")
        print(f"    Signal         : {trend_signal}")
        print(f"    EMA trend      : {trend}")
        print(f"    ADX            : {adx_val}")
        print(f"    +DI / -DI      : {di_plus} / {di_minus}")
        print(f"\n  Indicators (H1):")
        print(f"    RSI            : {rsi_val}")
        print(f"    ATR            : {atr_val} USD/oz")

    except Exception as exc:
        logger.error(f"{_INSTRUMENT}: test failed: {exc}")
        return False

    logger.info("\nAll components OK")
    return True


def main():
    args = parse_args()
    logger = get_logger("main")

    logger.info("Gold trading bot (XAU/USD) starting")

    settings.validate()

    broker = OandaBroker(logger)
    alert_manager = AlertManager(logger)
    kill_switch = KillSwitch(logger)
    event_monitor = EventMonitor(logger)
    decision_engine = DecisionEngine(logger)

    if args.test:
        if not broker.connect():
            logger.error("Cannot connect to broker — aborting test")
            sys.exit(1)
        success = run_test(broker, decision_engine, logger)
        sys.exit(0 if success else 1)

    # Live / dry-run mode
    if not broker.connect():
        logger.error("Cannot connect to broker — aborting")
        sys.exit(1)

    engine = TradingEngine(
        broker=broker,
        decision_engine=decision_engine,
        alert_manager=alert_manager,
        kill_switch=kill_switch,
        logger=logger,
        dry_run=args.dry_run,
        event_monitor=event_monitor,
    )
    news_watcher = NewsWatcher(
        event_monitor=event_monitor,
        broker=broker,
        alert_manager=alert_manager,
        get_trades_snapshot_fn=engine.get_known_trades_snapshot,
        on_trade_closed_fn=engine.remove_known_trade,
        logger=logger,
    )
    engine.news_watcher = news_watcher

    # Telegram calendar helper
    def _get_calendar_text() -> str:
        events = event_monitor.get_upcoming_events(
            hours_ahead=24,
            hours_behind=0,
            min_impact=EventImpact.MEDIUM,
        )
        if not events:
            return "Gold Calendar\n\nNo upcoming gold-relevant events for the rest of today."
        lines = ["Gold Calendar (next 24h)\n"]
        for e in sorted(events, key=lambda x: x.minutes_until):
            if e.minutes_until < 0:
                continue
            time_str = e.time.strftime("%H:%M UTC")
            h = int(e.minutes_until // 60)
            m = int(e.minutes_until % 60)
            countdown = f"{h}h {m}m" if h > 0 else f"{m}m"
            parts = [f"F:{e.forecast}"] if e.forecast not in ("0.0", "0", "", "None") else []
            parts += [f"P:{e.previous}"] if e.previous not in ("0.0", "0", "", "None") else []
            data_str = " | ".join(parts)
            line = (
                f"{time_str} (in {countdown})\n"
                f"{e.currency} — {e.event_name}\n"
                f"Impact: {e.impact.value.upper()}"
            )
            if data_str:
                line += f" | {data_str}"
            lines.append(line)
        return "\n\n".join(lines)

    alert_manager.start_command_poller(
        kill_switch=kill_switch,
        get_status_fn=engine.get_status,
        get_calendar_fn=_get_calendar_text,
        get_credits_fn=decision_engine.get_llm_provider_status,
        get_analyst_fn=decision_engine.get_analyst_summary,
        get_reviewer_fn=decision_engine.get_reviewer_summary,
    )

    try:
        engine.start(
            interval_seconds=args.interval,
            max_cycles=args.cycle,
        )
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — stopping engine")
        engine.stop()


if __name__ == "__main__":
    main()
