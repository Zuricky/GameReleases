"""Process entry point: ``python -m gamereleases``."""

from __future__ import annotations

import logging
import sys

from pydantic import ValidationError

from gamereleases import __version__
from gamereleases.bot.app import GameReleasesBot
from gamereleases.config import Settings, get_settings

logger = logging.getLogger("gamereleases")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Libraries that log every request at INFO level.
_NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def load_settings() -> Settings:
    """Read the settings, or exit with the names of the invalid or missing keys."""
    try:
        return get_settings()
    except ValidationError as exc:
        # Only field names are reported: values may be secrets.
        fields = sorted(
            {".".join(str(part) for part in e["loc"]) for e in exc.errors()}
        )
        print(
            f"Invalid configuration, check these settings: {', '.join(fields)}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info(
        "Starting gamereleases %s (db=%s, timezone=%s)",
        __version__,
        settings.db_path,
        settings.timezone,
    )
    bot = GameReleasesBot(settings)
    # Logging is configured above, so the library must not install its own.
    bot.run(settings.discord_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    main()
