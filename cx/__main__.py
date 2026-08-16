"""支持 `python3 -m cx`。"""

import sys

from cx.cli import main

if __name__ == "__main__":
    sys.exit(main())
