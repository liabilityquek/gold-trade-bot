"""Logging configuration and utilities for the Gold trading bot."""

import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import colorlog

from config.settings import settings


def setup_logger(
    name: str = 'gold_trading_bot',
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """Set up a logger with console and optional file handlers."""
    logger = logging.getLogger(name)
    logger.handlers = []

    level = log_level or settings.LOG_LEVEL
    logger.setLevel(getattr(logging, level.upper()))

    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)

    console_format = colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    if settings.LOG_TO_FILE:
        log_path = Path(log_file or settings.LOG_FILE_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)

        file_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


class TradeLogger:
    """Specialized logger for trade execution audit trail."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or get_logger('trade_audit')

        audit_path = Path('logs') / 'trade_audit.log'
        audit_path.parent.mkdir(parents=True, exist_ok=True)

        audit_handler = logging.FileHandler(audit_path, encoding='utf-8')
        audit_handler.setLevel(logging.INFO)

        audit_format = logging.Formatter(
            '%(asctime)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        audit_handler.setFormatter(audit_format)

        existing_paths = {
            h.baseFilename for h in self.logger.handlers
            if isinstance(h, logging.FileHandler)
        }
        if str(audit_path.resolve()) not in existing_paths:
            self.logger.addHandler(audit_handler)

    def log_decision(self, action: str, reason: str, data: Optional[dict] = None):
        message = f"XAU_USD | {action} | {reason}"
        if data:
            message += f" | {data}"
        self.logger.info(message)

    def log_trade_execution(
        self,
        side: str,
        units: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        trade_id: Optional[str] = None,
    ):
        message = (
            f"XAU_USD | EXECUTE | {side} {units} oz @ {entry_price:.2f} | "
            f"SL: {stop_loss:.2f} | TP: {take_profit:.2f}"
        )
        if trade_id:
            message += f" | ID: {trade_id}"
        self.logger.info(message)

    def log_trade_close(
        self,
        trade_id: str,
        close_price: float,
        pnl: float,
        reason: str,
    ):
        pnl_sign = "+" if pnl >= 0 else ""
        message = (
            f"XAU_USD | CLOSE | ID: {trade_id} @ {close_price:.2f} | "
            f"P/L: {pnl_sign}${pnl:.2f} | {reason}"
        )
        self.logger.info(message)
