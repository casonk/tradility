"""Allow `python -m tradility` execution."""

import sys

from tradility.cli import main

sys.exit(main())
