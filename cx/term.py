from __future__ import annotations

import os
import sys

from cx.model import SCOPE_LABEL
from cx.util import disp_width


class C:
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.on else s

    @classmethod
    def dim(cls, s):
        return cls._w("2", s)

    @classmethod
    def bold(cls, s):
        return cls._w("1", s)

    @classmethod
    def red(cls, s):
        return cls._w("31", s)

    @classmethod
    def green(cls, s):
        return cls._w("32", s)

    @classmethod
    def yellow(cls, s):
        return cls._w("33", s)

    @classmethod
    def blue(cls, s):
        return cls._w("34", s)

    @classmethod
    def magenta(cls, s):
        return cls._w("35", s)

    @classmethod
    def cyan(cls, s):
        return cls._w("36", s)


SCOPE_COLOR = {
    "user": C.blue,
    "project": C.green,
    "local": C.yellow,
    "managed": C.magenta,
}


def tag(scope: str) -> str:
    return SCOPE_COLOR.get(scope, C.dim)(f"[{SCOPE_LABEL.get(scope, scope)}]")


def hr(title: str) -> str:
    line = "─" * max(4, 62 - disp_width(title))
    return C.bold(f"\n── {title} ") + C.dim(line)
