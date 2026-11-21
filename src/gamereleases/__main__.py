"""Process entry point: ``python -m gamereleases``."""

from __future__ import annotations

import logging

from gamereleases import __version__
from gamereleases.config import get_settings

logger = logging.getLogger("gamereleases")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info(
        "gamereleases %s configured (db=%s, timezone=%s)",
        __version__,
        settings.db_path,
        settings.timezone,
    )
    logger.warning("Discord client is not wired up yet; exiting")


if __name__ == "__main__":
    main()
