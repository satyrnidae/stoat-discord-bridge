from __future__ import annotations

import asyncio

from stoat_discord_bridge.bridge import run
from stoat_discord_bridge.config import load_config
from stoat_discord_bridge.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    asyncio.run(run(load_config()))


if __name__ == "__main__":
    main()
