"""PyInstaller's entry script.

PyInstaller freezes a script, not a console-script name from `pyproject.toml`, so the
`.exe` needs a real file that calls `clockwork.app.main` -- this one, named directly by
`clockwork.spec`.
"""

import sys

from clockwork.app import main

if __name__ == "__main__":
    sys.exit(main())
