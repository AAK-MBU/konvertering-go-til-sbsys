"""Automation Server forventer main.py i repoets rod.

Denne fil er kun en indgang — logikken ligger i automation/main.py og
src/konvertering/, så den kan testes og køres fra terminalen uden en
Automation Server.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from automation.main import main  # noqa: E402

if __name__ == "__main__":
    main()
