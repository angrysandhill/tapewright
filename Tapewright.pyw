# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Double-click launcher. Windows runs .pyw files with pythonw, so no console window appears."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tapewright.app import main  # noqa: E402

main()
