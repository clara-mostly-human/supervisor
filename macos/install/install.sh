#!/bin/bash
# Install Home Assistant natively on this Mac. See install.py --help.
exec /usr/bin/python3 "$(cd "$(dirname "$0")" && pwd)/install.py" "$@"
