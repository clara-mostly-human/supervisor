"""Install a Home Assistant Core version the way the native Supervisor does.

Usage: python -m supervisor.native.install_core <data>/core <version>

Lets a Core version be installed ahead of time, e.g. before switching an
existing installation over to the native Supervisor.
"""

import asyncio
import logging
from pathlib import Path
import sys

from .homeassistant import InstallError, install_core

_LOGGER = logging.getLogger(__name__)


def main() -> int:
    """Run the installer."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        _LOGGER.error(__doc__.strip().splitlines()[2])
        return 2
    try:
        asyncio.run(install_core(Path(sys.argv[1]), sys.argv[2]))
    except InstallError as err:
        _LOGGER.error("%s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
