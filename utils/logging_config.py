# utils/logging_config.py
"""
Structured Logging Configuration for Sentient Trader

Provides JSON-structured logging for all services with:
- Consistent log format across services
- Request/event tracing
- Metric-friendly log structure
- Environment-based log levels

STANDARD STRUCTURED FIELDS:
All event loggers include these fields where applicable:
- symbol: Stock ticker symbol (e.g., "AAPL")
- data_quality: Data quality enum ("GOOD", "PARTIAL", "STALE", "MISSING")
- confidence: Confidence score (0.0-1.0)
- decision_reason: Why a decision was made or skipped

LOG LEVELS:
- DEBUG: Cache hits, skipped items, detailed tracing
- INFO: Normal operations (decisions made, data fetched)
- WARNING: Recoverable issues (validation failures, cache misses, fallbacks)
- ERROR: Failures that need attention

CRITICAL: Logging NEVER causes crashes - all operations wrapped in try/except.
"""

import os
import json
import logging
import sys
from datetime import datetime
from typing import Any, Dict, Optional


# Environment configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")  # 'json' or 'text'
SERVICE_NAME = os.getenv("SERVICE_NAME", "sentient-trader")


class StructuredLogFormatter(logging.Formatter):
    """
    JSON-structured log formatter for observability.
    
    CRASH-SAFE: All formatting wrapped in try/except to ensure logging
    never causes application failures.
    
    Output format:
    {
        "timestamp": "2025-01-15T10:30:00.000Z",
        "level": "INFO",
        "service": "strategy-worker",
        "logger": "strategy_worker",
        "message": "Decision made",
        "context": {
            "symbol": "AAPL",
            "decision": "BUY",
            "confidence": 0.85,
            "data_quality": "GOOD",
            "decision_reason": "Strong sentiment + bullish indicators"
        }
    }
    """
    
    def format(self, record: logging.LogRecord) -> str:
        # CRITICAL: Wrap entire format in try/except - logging must NEVER crash
        try:
            log_entry = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "service": SERVICE_NAME,
                "logger": record.name,
                "message": self._safe_get_message(record),
            }
            
            # Add extra context if available (safely)
            context = getattr(record, 'context', None)
            if context:
                # Ensure all context values are JSON-serializable
                log_entry["context"] = self._safe_serialize_context(context)
            
            # Add exception info if present
            if record.exc_info:
                try:
                    log_entry["exception"] = self.formatException(record.exc_info)
                except Exception:
                    log_entry["exception"] = "<exception formatting failed>"
            
            # Add location for DEBUG and ERROR levels
            if record.levelno >= logging.ERROR or record.levelno <= logging.DEBUG:
                log_entry["location"] = {
                    "file": getattr(record, 'filename', 'unknown'),
                    "line": getattr(record, 'lineno', 0),
                    "function": getattr(record, 'funcName', 'unknown')
                }
            
            return json.dumps(log_entry, ensure_ascii=False, default=str)
        except Exception as e:
            # FALLBACK: If JSON formatting fails, return basic text
            return f"{datetime.utcnow().isoformat()}Z {record.levelname} [{record.name}] {record.getMessage()} [LOG_FORMAT_ERROR: {e}]"
    
    def _safe_get_message(self, record: logging.LogRecord) -> str:
        """Safely get log message, handling formatting errors."""
        try:
            return record.getMessage()
        except Exception:
            return str(record.msg)
    
    def _safe_serialize_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Safely serialize context dict, converting non-serializable values to strings."""
        if not isinstance(context, dict):
            return {"value": str(context)}
        
        safe_context = {}
        for key, value in context.items():
            try:
                # Test if value is JSON serializable
                json.dumps(value)
                safe_context[key] = value
            except (TypeError, ValueError):
                # Convert to string if not serializable
                safe_context[key] = str(value)
        return safe_context


class TextLogFormatter(logging.Formatter):
    """Human-readable text formatter for local development."""
    
    FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    
    def __init__(self):
        super().__init__(fmt=self.FORMAT, datefmt="%Y-%m-%d %H:%M:%S")


class StructuredLogger(logging.LoggerAdapter):
    """
    Logger adapter that supports structured context logging.
    
    Usage:
        logger = get_structured_logger("my_service")
        logger.info("Processing message", context={"symbol": "AAPL", "confidence": 0.9})
    """
    
    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        # Extract context from kwargs and add to extra
        context = kwargs.pop('context', None)
        if context:
            kwargs.setdefault('extra', {})['context'] = context
        return msg, kwargs


def configure_logging(service_name: Optional[str] = None, 
                     log_level: Optional[str] = None,
                     log_format: Optional[str] = None) -> None:
    """
    Configure logging for a service.
    
    Args:
        service_name: Name of the service (for log tagging)
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_format: Output format ('json' or 'text')
    """
    global SERVICE_NAME
    
    if service_name:
        SERVICE_NAME = service_name
    
    level = getattr(logging, log_level or LOG_LEVEL, logging.INFO)
    fmt = log_format or LOG_FORMAT
    
    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    
    if fmt == "json":
        handler.setFormatter(StructuredLogFormatter())
    else:
        handler.setFormatter(TextLogFormatter())
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove existing handlers and add our configured one
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    
    # Quiet noisy libraries
    logging.getLogger("kafka").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def get_structured_logger(name: str) -> StructuredLogger:
    """
    Get a structured logger for a module.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        StructuredLogger instance
    """
    logger = logging.getLogger(name)
    return StructuredLogger(logger, {})


# Convenience logging functions with context
def log_event(logger: logging.Logger, event: str, level: str = "INFO", **context) -> None:
    """
    Log a structured event with context.
    
    CRASH-SAFE: Wrapped in try/except to ensure logging never causes failures.
    
    Args:
        logger: Logger instance
        event: Event name/message
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        **context: Additional context key-value pairs including:
            - symbol: Stock ticker symbol
            - data_quality: "GOOD", "PARTIAL", "STALE", "MISSING"
            - confidence: Confidence score (0.0-1.0)
            - decision_reason: Why a decision was made
    """
    try:
        log_level = getattr(logging, level.upper(), logging.INFO)
        record = logger.makeRecord(
            logger.name, 
            log_level,
            "", 0, event, (), None
        )
        record.context = context if context else None
        logger.handle(record)
    except Exception:
        # FALLBACK: If structured logging fails, try basic logging
        try:
            logger.log(logging.INFO, f"{event} [context={context}]")
        except Exception:
            # Ultimate fallback: print to stderr
            import sys
            print(f"LOG_FALLBACK: {event}", file=sys.stderr)


# Pre-defined event loggers for common operations
class TradeEventLogger:
    """
    Structured logger for trade-related events.
    
    CRASH-SAFE: All methods wrapped in try/except.
    
    Standard fields included:
    - symbol: Stock ticker
    - confidence: Confidence score (0.0-1.0)
    - decision_reason: Why decision was made/skipped
    - data_quality: Quality of underlying data
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def log_decision(self, symbol: str, decision: str, confidence: float, 
                     mode: str = "full", effective_sentiment: float = 0.0,
                     decision_reason: str = "", data_quality: str = "GOOD",
                     **extra) -> None:
        """
        Log a trading decision.
        
        Args:
            symbol: Stock ticker symbol
            decision: BUY, SELL, or HOLD
            confidence: Confidence score (0.0-1.0)
            mode: Decision mode ("full" or "news_only")
            effective_sentiment: Weighted sentiment score
            decision_reason: Human-readable explanation
            data_quality: Data quality ("GOOD", "PARTIAL", "STALE", "MISSING")
        """
        try:
            context = {
                "event_type": "trade_decision",
                "symbol": symbol,
                "decision": decision,
                "confidence": round(float(confidence), 3) if confidence else 0.0,
                "mode": mode,
                "effective_sentiment": round(float(effective_sentiment), 3) if effective_sentiment else 0.0,
                "decision_reason": decision_reason or f"{decision} based on {mode} mode",
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Trade decision: {symbol} {decision}", "INFO", **context)
        except Exception:
            # CRASH-SAFE: Never let logging crash the application
            pass
    
    def log_skip(self, symbol: str, reason: str, confidence: float = 0.0, 
                 data_quality: str = "UNKNOWN", **extra) -> None:
        """
        Log a skipped trade decision.
        
        Uses DEBUG level for normal skips (low confidence, index symbols).
        Uses WARNING level for recoverable issues (missing data, validation failures).
        """
        try:
            # Determine log level based on reason
            # WARNING for recoverable issues that might need attention
            warning_reasons = ["missing", "invalid", "error", "failed", "stale"]
            level = "WARNING" if any(r in reason.lower() for r in warning_reasons) else "DEBUG"
            
            context = {
                "event_type": "trade_skip",
                "symbol": symbol,
                "decision_reason": reason,  # Standardized field name
                "confidence": round(float(confidence), 3) if confidence else 0.0,
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Skipped {symbol}: {reason}", level, **context)
        except Exception:
            pass
    
    def log_warmup(self, symbol: str, bars_have: int, bars_need: int, 
                   ready: bool = False, **extra) -> None:
        """
        Log warmup status for a symbol.
        
        Uses WARNING if not ready and bars_have > 0 (partially warmed up).
        """
        try:
            level = "INFO" if ready else ("WARNING" if bars_have > 0 else "DEBUG")
            context = {
                "event_type": "warmup_status",
                "symbol": symbol,
                "bars_have": bars_have,
                "bars_need": bars_need,
                "warmup_remaining": max(0, bars_need - bars_have),
                "ready": ready,
                **extra
            }
            status = "READY" if ready else f"WARMUP({bars_have}/{bars_need})"
            log_event(self.logger, f"Warmup status {symbol}: {status}", level, **context)
        except Exception:
            pass


class DataEventLogger:
    """
    Structured logger for data ingestion events.
    
    CRASH-SAFE: All methods wrapped in try/except.
    
    Standard fields included:
    - symbol: Stock ticker
    - data_quality: Quality enum ("GOOD", "PARTIAL", "STALE", "MISSING")
    - decision_reason: Why data was accepted/rejected
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def log_fetch(self, source: str, count: int, data_quality: str = "GOOD", 
                  symbol: str = "", **extra) -> None:
        """
        Log a data fetch operation.
        
        Uses WARNING if data_quality is not GOOD.
        """
        try:
            # WARNING for non-GOOD quality fetches
            level = "WARNING" if data_quality in ("PARTIAL", "STALE", "MISSING") else "INFO"
            
            context = {
                "event_type": "data_fetch",
                "source": source,
                "symbol": symbol,
                "record_count": count,
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Fetched {count} records from {source} [{data_quality}]", level, **context)
        except Exception:
            pass
    
    def log_cache_hit(self, symbol: str, age_seconds: int, data_quality: str = "GOOD", **extra) -> None:
        """Log a cache hit."""
        try:
            context = {
                "event_type": "cache_hit",
                "symbol": symbol,
                "age_seconds": age_seconds,
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Cache hit for {symbol} (age: {age_seconds}s)", "DEBUG", **context)
        except Exception:
            pass
    
    def log_cache_miss(self, symbol: str, decision_reason: str = "not_in_cache", **extra) -> None:
        """
        Log a cache miss.
        
        Uses WARNING level since this is a recoverable issue.
        """
        try:
            context = {
                "event_type": "cache_miss",
                "symbol": symbol,
                "decision_reason": decision_reason,
                "data_quality": "MISSING",
                **extra
            }
            log_event(self.logger, f"Cache miss for {symbol}: {decision_reason}", "WARNING", **context)
        except Exception:
            pass
    
    def log_validation_fail(self, symbol: str, decision_reason: str, 
                            data_quality: str = "MISSING", **extra) -> None:
        """
        Log a data validation failure.
        
        Uses WARNING level - recoverable issue that needs attention.
        """
        try:
            context = {
                "event_type": "validation_fail",
                "symbol": symbol,
                "decision_reason": decision_reason,
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Validation failed for {symbol}: {decision_reason}", "WARNING", **context)
        except Exception:
            pass
    
    def log_fallback(self, symbol: str, fallback_source: str, 
                     decision_reason: str = "primary_source_failed", **extra) -> None:
        """
        Log when falling back to alternate data source.
        
        Uses WARNING level - recoverable but noteworthy.
        """
        try:
            context = {
                "event_type": "data_fallback",
                "symbol": symbol,
                "fallback_source": fallback_source,
                "decision_reason": decision_reason,
                "data_quality": "PARTIAL",
                **extra
            }
            log_event(self.logger, f"Fallback for {symbol} to {fallback_source}: {decision_reason}", "WARNING", **context)
        except Exception:
            pass


class ExtractionEventLogger:
    """
    Structured logger for ticker extraction events.
    
    CRASH-SAFE: All methods wrapped in try/except.
    
    Standard fields included:
    - symbol: Extracted ticker symbol
    - confidence: Extraction confidence (0.0-1.0)
    - decision_reason: Why extraction succeeded/failed
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def log_extraction(self, headline: str, tickers: list, **extra) -> None:
        """
        Log a successful extraction.
        
        Uses WARNING if no tickers extracted (might indicate extraction issue).
        """
        try:
            level = "INFO" if tickers else "WARNING"
            context = {
                "event_type": "ticker_extraction",
                "headline_preview": headline[:80] if headline else "",
                "ticker_count": len(tickers) if tickers else 0,
                "tickers": tickers or [],
                **extra
            }
            if tickers:
                log_event(self.logger, f"Extracted {len(tickers)} tickers", level, **context)
            else:
                log_event(self.logger, "No tickers extracted from headline", level, **context)
        except Exception:
            pass
    
    def log_rejection(self, symbol: str, decision_reason: str, 
                      confidence: float = 0.0, **extra) -> None:
        """
        Log a rejected ticker.
        
        Uses DEBUG for normal rejections (low confidence).
        Uses WARNING for rejections due to errors.
        """
        try:
            # WARNING for error-based rejections
            level = "WARNING" if "error" in decision_reason.lower() else "DEBUG"
            
            context = {
                "event_type": "ticker_rejection",
                "symbol": symbol,
                "confidence": round(float(confidence), 3) if confidence else 0.0,
                "decision_reason": decision_reason,
                **extra
            }
            log_event(self.logger, f"Rejected ticker {symbol}: {decision_reason}", level, **context)
        except Exception:
            pass
    
    def log_extraction_failure(self, headline: str, decision_reason: str, **extra) -> None:
        """
        Log when extraction completely fails.
        
        Uses WARNING level - recoverable but needs attention.
        """
        try:
            context = {
                "event_type": "extraction_failure",
                "headline_preview": headline[:80] if headline else "",
                "decision_reason": decision_reason,
                **extra
            }
            log_event(self.logger, f"Extraction failed: {decision_reason}", "WARNING", **context)
        except Exception:
            pass


class FeatureEventLogger:
    """
    Structured logger for feature computation events.
    
    CRASH-SAFE: All methods wrapped in try/except.
    
    Standard fields included:
    - symbol: Stock ticker
    - data_quality: Quality of source data
    - confidence: Feature completeness (0.0-1.0)
    - decision_reason: Why features were saved/skipped
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
    
    def log_features_saved(self, symbol: str, indicator_count: int, 
                           completeness_pct: float, data_quality: str = "GOOD",
                           is_warmed_up: bool = False, **extra) -> None:
        """Log successful feature save."""
        try:
            context = {
                "event_type": "features_saved",
                "symbol": symbol,
                "indicator_count": indicator_count,
                "confidence": round(completeness_pct / 100.0, 3),  # Normalize to 0-1
                "data_quality": data_quality,
                "is_warmed_up": is_warmed_up,
                "decision_reason": "warmed_up" if is_warmed_up else "partial_warmup",
                **extra
            }
            log_event(self.logger, f"Features saved for {symbol} ({indicator_count} indicators)", "INFO", **context)
        except Exception:
            pass
    
    def log_features_skipped(self, symbol: str, decision_reason: str,
                              bars_have: int = 0, bars_need: int = 0,
                              data_quality: str = "MISSING", **extra) -> None:
        """
        Log when features are not saved.
        
        Uses WARNING if we have some bars but not enough (partial warmup).
        """
        try:
            level = "WARNING" if bars_have > 0 else "DEBUG"
            
            context = {
                "event_type": "features_skipped",
                "symbol": symbol,
                "decision_reason": decision_reason,
                "bars_have": bars_have,
                "bars_need": bars_need,
                "confidence": round(bars_have / bars_need, 3) if bars_need > 0 else 0.0,
                "data_quality": data_quality,
                **extra
            }
            log_event(self.logger, f"Features skipped for {symbol}: {decision_reason}", level, **context)
        except Exception:
            pass
    
    def log_indicator_error(self, symbol: str, indicator: str, 
                            decision_reason: str, **extra) -> None:
        """Log indicator computation error (WARNING level)."""
        try:
            context = {
                "event_type": "indicator_error",
                "symbol": symbol,
                "indicator": indicator,
                "decision_reason": decision_reason,
                "data_quality": "PARTIAL",
                **extra
            }
            log_event(self.logger, f"Indicator error for {symbol}.{indicator}: {decision_reason}", "WARNING", **context)
        except Exception:
            pass


# Initialize on import (can be reconfigured by services)
if LOG_FORMAT == "json":
    configure_logging()
