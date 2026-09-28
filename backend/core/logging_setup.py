"""Process-wide logging configuration.

Uvicorn configures only its own ``uvicorn*`` loggers, so without this every
``logging.getLogger("services....").info(...)`` call fell through to Python's
last-resort handler (WARNING and above) and never reached ``.logs/backend.log``.

``configure_logging`` is idempotent: it installs a single stderr handler on the
root logger (start.sh redirects stderr into the log file), sets the level from
``LOG_LEVEL`` (default INFO) and attaches a filter that strips credentials
from query strings in uvicorn's access / websocket lines.
"""

from __future__ import annotations

import logging
import os
import re
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Query parameters whose values must never be written to a log file. Matches
# token=, access_token=, refresh_token=, ws_token=, password=, api_key=, ...
_SECRET_PARAM = re.compile(
    r"(?i)([?&;](?:[a-z0-9_\-]*token|password|passwd|secret|api_key|apikey|key)=)[^&\s\"'#]*"
)
REDACTED = "[REDACTED]"

_HANDLER_FLAG = "_marketplace_handler"


def redact_secrets(value: str) -> str:
    """Replace the value of any credential-like query parameter."""
    return _SECRET_PARAM.sub(lambda m: m.group(1) + REDACTED, value)


class RedactQueryTokensFilter(logging.Filter):
    """Redact ``token=...`` (and similar) values in a record's message/args.

    Uvicorn's access line is ``'%s - "%s %s HTTP/%s" %d'`` with the full path
    (query string included) as an arg, and its websocket line
    (``"WebSocket /api/v1/ws?token=..." [accepted]``) goes through
    ``uvicorn.error``; both carry the JWT the frontend passes on the socket URL.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_secrets(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: redact_secrets(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        return True


def _level_from_env() -> int:
    name = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def install_redaction_filters() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactQueryTokensFilter) for f in target.filters):
            target.addFilter(RedactQueryTokensFilter())


def configure_logging() -> None:
    level = _level_from_env()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, _HANDLER_FLAG, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_FLAG, True)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    # Our own packages at the configured level, regardless of library defaults.
    for name in ("services", "api", "core", "db", "app"):
        logging.getLogger(name).setLevel(level)

    # One line per outbound Qdrant/Ollama request is noise at INFO.
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    # Give uvicorn's own lines the same timestamped format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)

    install_redaction_filters()
