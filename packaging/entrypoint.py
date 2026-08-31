"""PyInstaller entry point.

PyInstaller freezes a *script*, not a module, so it cannot be pointed at ``python -m sqlhealthwatch``
directly. This is that script: a two-line shim onto the same ``main()`` the console script uses, so
the frozen exe and ``python -m sqlhealthwatch`` behave identically.
"""

import sys

from sqlhealthwatch.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
