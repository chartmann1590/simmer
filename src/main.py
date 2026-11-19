#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simmer.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
