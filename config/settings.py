"""Settings management using environment variables."""

import os
import re
from pathlib import Path
from typing import List
import dotenv

env_path = Path(__file__).parent.parent / '.env'
dotenv.load_dotenv(dotenv_path=env_path)


class Settings:
    """Centralized configuration from environment variables."""

    # ==========================================
    # BROKER: OANDA
    # ==========================================
    OANDA_API_KEY: str = os.getenv('OANDA_API_KEY')
    OANDA_ACCOUNT_ID: str = os.getenv('OANDA_ACCOUNT_ID')
    OANDA_ENVIRONMENT: str = os.getenv('OANDA_ENVIRONMENT', 'practice')

    @property
    def oanda_api_url(self) -> str:
        if self.OANDA_ENVIRONMENT == 'live':
            return 'https://api-fxtrade.oanda.com'
        return 'https://api-fxpractice.oanda.com'

    @property
    def oanda_stream_url(self) -> str:
        if self.OANDA_ENVIRONMENT == 'live':
            return 'https://stream-fxtrade.oanda.com'
        return 'https://stream-fxpractice.oanda.com'

    # ==========================================
    # GROQ / LLM AGENT
    # ==========================================
    GROQ_API_KEY: str = os.getenv('GROQ_API_KEY', '')
    LLM_MODEL: str = os.getenv('LLM_MODEL', 'llama-3.3-70b-versatile')
    LLM_AGENT_WEIGHT: float = float(os.getenv('LLM_AGENT_WEIGHT', '1.5'))

    ANTHROPIC_API_KEY: str = os.getenv('ANTHROPIC_API_KEY', '')
    ANTHROPIC_LLM_MODEL: str = os.getenv('ANTHROPIC_LLM_MODEL', 'claude-haiku-4-5-20251001')

    REVIEWER_LLM_MODEL: str = os.getenv('REVIEWER_LLM_MODEL', 'llama-3.1-8b-instant')

    # ==========================================
    # VOTING ENGINE
    # ==========================================
    CONSENSUS_THRESHOLD: float = float(os.getenv('CONSENSUS_THRESHOLD', '0.60'))
    CANDLE_COUNT: int = int(os.getenv('CANDLE_COUNT', '100'))

    # ==========================================
    # RISK GUARDRAILS
    # ==========================================
    MAX_DAILY_DRAWDOWN: float = float(os.getenv('MAX_DAILY_DRAWDOWN', '0.05'))
    MAX_CONSECUTIVE_LOSSES: int = int(os.getenv('MAX_CONSECUTIVE_LOSSES', '5'))
    MAX_ORDERS_PER_MINUTE: int = int(os.getenv('MAX_ORDERS_PER_MINUTE', '10'))

    # Gold market hours: Sun 22:00–Fri 21:00 UTC
    WEEKEND_BLOCK_FRIDAY_UTC_HOUR: int = int(os.getenv('WEEKEND_BLOCK_FRIDAY_UTC_HOUR', '21'))
    WEEKEND_RESUME_SUNDAY_UTC_HOUR: int = int(os.getenv('WEEKEND_RESUME_SUNDAY_UTC_HOUR', '22'))

    # ==========================================
    # TRADING PARAMETERS
    # ==========================================
    INSTRUMENT: str = 'XAU_USD'
    TIMEFRAME: str = os.getenv('TIMEFRAME', 'H1')
    MAX_LEVERAGE: int = int(os.getenv('MAX_LEVERAGE', '20'))
    MAX_CONCURRENT_TRADES: int = int(os.getenv('MAX_CONCURRENT_TRADES', '2'))

    EXECUTION_INTERVAL_SECONDS: int = int(os.getenv('EXECUTION_INTERVAL_SECONDS', '3600'))
    MONITORING_INTERVAL_SECONDS: int = int(os.getenv('MONITORING_INTERVAL_SECONDS', '60'))

    # 1% NAV per trade (gold: tighter sizing)
    MAX_RISK_PER_TRADE: float = float(os.getenv('MAX_RISK_PER_TRADE', '0.01'))
    MAX_TOTAL_EXPOSURE: float = float(os.getenv('MAX_TOTAL_EXPOSURE', '0.80'))

    # Trade quality filters
    MIN_CONFLUENCES: int = int(os.getenv('MIN_CONFLUENCES', '3'))
    MIN_RR_RATIO: float = float(os.getenv('MIN_RR_RATIO', '1.5'))

    # LEARNING / EXPERIENCE BRAIN (shadow mode — observe-only)
    # Records entries/outcomes and injects a historical prior + reflection rules
    # into the analyst prompt as observational context. Does NOT change
    # confidence or gate trades. Flip to active influence later behind a flag.
    LEARNING_ENABLED: bool = os.getenv('LEARNING_ENABLED', 'true').lower() == 'true'
    LEARNING_MIN_SAMPLE: int = int(os.getenv('LEARNING_MIN_SAMPLE', '8'))
    LEARNING_RECALL_HOUR_WINDOW: int = int(os.getenv('LEARNING_RECALL_HOUR_WINDOW', '2'))
    LEARNING_REFLECTION_MIN_TRADES: int = int(os.getenv('LEARNING_REFLECTION_MIN_TRADES', '20'))
    LEARNING_REFLECTION_MAX_RULES: int = int(os.getenv('LEARNING_REFLECTION_MAX_RULES', '6'))

    # Trailing stop — gold uses fixed USD/oz points (pips); triggers are pip-based
    TRAILING_STOP_ACTIVATION_POINTS: float = float(os.getenv('TRAILING_STOP_ACTIVATION_POINTS', '7.0'))
    TRAILING_DISTANCE_POINTS: float = float(os.getenv('TRAILING_DISTANCE_POINTS', '7.0'))
    # ATR multipliers retained for reference/backtest A/B; not used by live pip-based triggers
    TRAILING_ATR_MULTIPLIER: float = float(os.getenv('TRAILING_ATR_MULTIPLIER', '1.5'))
    TRAILING_ACTIVATION_ATR_MULT: float = float(os.getenv('TRAILING_ACTIVATION_ATR_MULT', '2.0'))

    # Break-even — gold points (pips); triggers are pip-based
    BREAK_EVEN_ACTIVATION_POINTS: float = float(os.getenv('BREAK_EVEN_ACTIVATION_POINTS', '5.0'))
    BREAK_EVEN_BUFFER_POINTS: float = float(os.getenv('BREAK_EVEN_BUFFER_POINTS', '1.0'))
    # ATR multipliers retained for reference/backtest A/B; not used by live pip-based triggers
    BREAK_EVEN_ACTIVATION_ATR_MULT: float = float(os.getenv('BREAK_EVEN_ACTIVATION_ATR_MULT', '1.5'))
    BREAK_EVEN_BUFFER_ATR_MULT: float = float(os.getenv('BREAK_EVEN_BUFFER_ATR_MULT', '0.5'))

    # Partial take-profits
    PARTIAL_TP_ENABLED: bool = os.getenv('PARTIAL_TP_ENABLED', 'true').lower() == 'true'
    PARTIAL_TP_RATIO: float = float(os.getenv('PARTIAL_TP_RATIO', '0.5'))
    PARTIAL_TP_RR_TARGET: float = float(os.getenv('PARTIAL_TP_RR_TARGET', '1.5'))

    # ==========================================
    # MONITORING & ALERTS (Telegram)
    # ==========================================
    ALERT_ENABLED: bool = os.getenv('ALERT_ENABLED', 'false').lower() == 'true'
    TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '') or os.getenv('TELEGRAM_BOT', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')

    # ==========================================
    # LOGGING
    # ==========================================
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_TO_FILE: bool = os.getenv('LOG_TO_FILE', 'true').lower() == 'true'
    LOG_FILE_PATH: str = os.getenv('LOG_FILE_PATH', 'logs/trading_bot.log')

    # ==========================================
    # ECONOMIC CALENDAR
    # ==========================================
    JB_NEWS_API_KEY: str = os.getenv('JB_NEWS_API_KEY', '')
    FRED_API_KEY: str = os.getenv('FRED_API_KEY', '')
    NEWS_SUSPEND_BEFORE_MINUTES: int = int(os.getenv('NEWS_SUSPEND_BEFORE_MINUTES', '30'))
    NEWS_RESUME_AFTER_MINUTES: int = int(os.getenv('NEWS_RESUME_AFTER_MINUTES', '30'))
    EVENT_CACHE_TTL_HOURS: int = int(os.getenv('EVENT_CACHE_TTL_HOURS', '1'))
    NEWS_RISK_CLOSE_THRESHOLD: float = float(os.getenv('NEWS_RISK_CLOSE_THRESHOLD', '0.65'))
    NEWS_RISK_MINUTES_BEFORE: int = int(os.getenv('NEWS_RISK_MINUTES_BEFORE', '20'))
    NEWS_RISK_POLL_INTERVAL_SECONDS: int = int(os.getenv('NEWS_RISK_POLL_INTERVAL_SECONDS', '120'))

    # Gold-specific high-impact events
    HIGH_IMPACT_EVENTS: List[str] = os.getenv(
        'HIGH_IMPACT_EVENTS',
        'NFP,FOMC,GDP,CPI,PCE,Interest Rate,Federal Reserve,Fed Chair,Nonfarm'
    ).split(',')

    # ==========================================
    # CENTRAL BANK RATES (USD only — gold is priced in USD)
    # ==========================================
    CB_RATE_USD: float = float(os.getenv('CB_RATE_USD', '4.50'))

    # ==========================================
    # TRADING HOURS WINDOW
    # SGT 6pm–midnight = UTC 10:00–16:00 (Singapore is UTC+8, no DST)
    # ==========================================
    TRADING_WINDOW_ENABLED: bool = os.getenv('TRADING_WINDOW_ENABLED', 'true').lower() == 'true'
    TRADING_WINDOW_START_UTC: int = int(os.getenv('TRADING_WINDOW_START_UTC', '10'))
    TRADING_WINDOW_END_UTC: int = int(os.getenv('TRADING_WINDOW_END_UTC', '16'))

    # ==========================================
    # MULTI-TIMEFRAME CANDLE COUNTS
    # ==========================================
    H4_CANDLE_COUNT: int = int(os.getenv('H4_CANDLE_COUNT', '220'))
    M30_CANDLE_COUNT: int = int(os.getenv('M30_CANDLE_COUNT', '30'))
    M15_CANDLE_COUNT: int = int(os.getenv('M15_CANDLE_COUNT', '20'))
    M5_CANDLE_COUNT: int = int(os.getenv('M5_CANDLE_COUNT', '15'))

    MAX_TRADE_AGE_HOURS: float = float(os.getenv('MAX_TRADE_AGE_HOURS', '72.0'))

    # ==========================================
    # ATR & SL/TP PARAMETERS
    # ==========================================
    DEFAULT_ATR_POINTS: float = float(os.getenv('DEFAULT_ATR_POINTS', '15.0'))
    ATR_ADAPTIVE_RATIO_HIGH: float = float(os.getenv('ATR_ADAPTIVE_RATIO_HIGH', '1.5'))
    ATR_ADAPTIVE_RATIO_LOW: float = float(os.getenv('ATR_ADAPTIVE_RATIO_LOW', '0.8'))
    TP1_MULTIPLIER: float = float(os.getenv('TP1_MULTIPLIER', '1.5'))
    TP2_MULTIPLIER: float = float(os.getenv('TP2_MULTIPLIER', '2.0'))
    TP3_MULTIPLIER: float = float(os.getenv('TP3_MULTIPLIER', '3.0'))

    # ==========================================
    # LLM CALL PARAMETERS
    # ==========================================
    LLM_MIN_CALL_SPACING_SECONDS: int = int(os.getenv('LLM_MIN_CALL_SPACING_SECONDS', '10'))
    REVIEWER_MIN_CALL_SPACING_SECONDS: int = int(os.getenv('REVIEWER_MIN_CALL_SPACING_SECONDS', '5'))
    LLM_MAX_TOKENS: int = int(os.getenv('LLM_MAX_TOKENS', '256'))
    REVIEWER_MAX_TOKENS: int = int(os.getenv('REVIEWER_MAX_TOKENS', '200'))
    LLM_RAG_SAMPLE_COUNT: int = int(os.getenv('LLM_RAG_SAMPLE_COUNT', '3'))

    # ==========================================
    # CIRCUIT BREAKER (order executor)
    # ==========================================
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = int(os.getenv('CIRCUIT_BREAKER_FAILURE_THRESHOLD', '5'))
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = float(os.getenv('CIRCUIT_BREAKER_COOLDOWN_SECONDS', '60.0'))

    # ==========================================
    # ORDER EXECUTOR (retries + slippage)
    # ==========================================
    ORDER_MAX_RETRIES: int = int(os.getenv('ORDER_MAX_RETRIES', '3'))
    ORDER_RETRY_INITIAL_DELAY_SECONDS: float = float(os.getenv('ORDER_RETRY_INITIAL_DELAY_SECONDS', '1.0'))
    ORDER_RETRY_MAX_DELAY_SECONDS: float = float(os.getenv('ORDER_RETRY_MAX_DELAY_SECONDS', '30.0'))
    ORDER_RETRY_BACKOFF_MULTIPLIER: float = float(os.getenv('ORDER_RETRY_BACKOFF_MULTIPLIER', '2.0'))
    MAX_SLIPPAGE_POINTS: float = float(os.getenv('MAX_SLIPPAGE_POINTS', '2.0'))

    # ==========================================
    # MACRO CONTEXT THRESHOLDS
    # ==========================================
    MACRO_REAL_YIELD_BEARISH_THRESHOLD: float = float(os.getenv('MACRO_REAL_YIELD_BEARISH_THRESHOLD', '1.5'))
    MACRO_REAL_YIELD_BULLISH_THRESHOLD: float = float(os.getenv('MACRO_REAL_YIELD_BULLISH_THRESHOLD', '0.0'))

    # ==========================================
    # SYSTEM
    # ==========================================
    PAPER_TRADING_MODE: bool = os.getenv('PAPER_TRADING_MODE', 'true').lower() == 'true'
    DATA_CACHE_HOURS: int = int(os.getenv('DATA_CACHE_HOURS', '24'))

    @classmethod
    def validate(cls) -> bool:
        """Validate that required settings are present."""
        errors = []

        if not cls.OANDA_API_KEY:
            errors.append("OANDA_API_KEY is required")

        if not cls.OANDA_ACCOUNT_ID:
            errors.append("OANDA_ACCOUNT_ID is required")

        if not cls.GROQ_API_KEY:
            errors.append("GROQ_API_KEY is required (LLM agent will fall back to HOLD without it)")

        if cls.ALERT_ENABLED and (not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID):
            errors.append("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required when ALERT_ENABLED is true")

        if errors:
            print("Configuration Errors:")
            for error in errors:
                print(f"  - {error}")
            fatal = [e for e in errors if 'GROQ_API_KEY' not in e]
            return len(fatal) == 0

        return True

    @classmethod
    def display(cls):
        """Display current configuration (no secrets). Only in DEBUG mode."""
        if os.getenv('DEBUG', '').lower() not in ('1', 'true', 'yes'):
            return
        print("\nCurrent Configuration:")
        print(f"  Instrument: {cls.INSTRUMENT}")
        print(f"  Environment: {cls.OANDA_ENVIRONMENT}")
        print(f"  Paper Trading: {cls.PAPER_TRADING_MODE}")
        print(f"  Timeframe: {cls.TIMEFRAME}")
        print(f"  LLM Model: {cls.LLM_MODEL}")
        print(f"  Max Risk per Trade: {cls.MAX_RISK_PER_TRADE*100}%")
        print(f"  Min RR Ratio: {cls.MIN_RR_RATIO}")
        print(f"  Min Confluences: {cls.MIN_CONFLUENCES}")
        print(f"  Alerts Enabled: {cls.ALERT_ENABLED}\n")


settings = Settings()
