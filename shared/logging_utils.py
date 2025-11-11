import logging
import os
from typing import Iterable, Optional, Sequence, Set

_REDACTED_PLACEHOLDER = "[REDACTED]"
_SENSITIVE_KEY_PARTS = ("TOKEN", "SECRET", "KEY", "PASS", "PWD")


def _is_sensitive_env_var(name: str) -> bool:
    upper_name = name.upper()
    return any(part in upper_name for part in _SENSITIVE_KEY_PARTS)


def _collect_sensitive_values(extra_values: Optional[Iterable[Optional[str]]] = None) -> Sequence[str]:
    secrets: Set[str] = set()
    for key, value in os.environ.items():
        if _is_sensitive_env_var(key) and value:
            secrets.add(value)
    if extra_values:
        for value in extra_values:
            if isinstance(value, str) and value:
                secrets.add(value)
    return tuple(secrets)


class RedactingFormatter(logging.Formatter):
    """Wrap another formatter and redact sensitive values from its output."""

    def __init__(
        self,
        base_formatter: Optional[logging.Formatter] = None,
        secrets: Optional[Sequence[str]] = None,
        placeholder: str = _REDACTED_PLACEHOLDER,
    ) -> None:
        super().__init__()
        self._base_formatter = base_formatter or logging.Formatter()
        self._secrets: Sequence[str] = tuple(secrets or ())
        self._placeholder = placeholder
        self.converter = self._base_formatter.converter

    def update_secrets(self, secrets: Sequence[str]) -> None:
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        formatted = self._base_formatter.format(record)
        for secret in self._secrets:
            formatted = formatted.replace(secret, self._placeholder)
        return formatted

    def formatException(self, ei):
        return self._base_formatter.formatException(ei)

    def formatTime(self, record, datefmt=None):
        return self._base_formatter.formatTime(record, datefmt)


def configure_logging(
    *,
    level: Optional[str] = None,
    extra_values: Optional[Iterable[Optional[str]]] = None,
) -> None:
    """Configure root logging and redact sensitive values from all handlers."""

    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level)
    root_logger.setLevel(level)

    secrets = _collect_sensitive_values(extra_values)

    for handler in root_logger.handlers:
        formatter = handler.formatter
        if isinstance(formatter, RedactingFormatter):
            formatter.update_secrets(secrets)
        else:
            handler.setFormatter(RedactingFormatter(formatter, secrets))

    for logger_name, logger_obj in logging.Logger.manager.loggerDict.items():
        if not isinstance(logger_obj, logging.Logger):
            continue
        for handler in logger_obj.handlers:
            formatter = handler.formatter
            if isinstance(formatter, RedactingFormatter):
                formatter.update_secrets(secrets)
            else:
                handler.setFormatter(RedactingFormatter(formatter, secrets))
