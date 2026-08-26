#!/usr/bin/env python3
"""Pin a guard process's own ``stdout``/``stderr`` to UTF-8 on every host.

Python encodes ``print`` output with the host's locale codec. On the Windows
developer host where ``bp/*`` branches are authored that codec is cp1252, and
that breaks a guard in two distinct ways:

* **The stream is not UTF-8.** A guard message containing an em dash emits
  byte ``0x97`` instead of ``e2 80 94``, so the captured lane output fails a
  strict UTF-8 decode -- the exact failure that motivated this module:
  ``'utf-8' codec can't decode byte 0x97 in position 1525: invalid start byte``
  on ``scripts/generate_test_estate_ranked_queue.py --check``.
* **The guard dies mid-report.** cp1252 cannot encode the box drawing, arrows
  and check marks that pytest/tsc/vitest output carries (``backend/testing``
  alone holds 31 distinct such characters and ~95k occurrences of ``U+2500``),
  so every guard that echoes captured tool output raises
  ``UnicodeEncodeError`` while printing the failure it just found. A guard
  that cannot print its own verdict reports nothing at all.

``errors="replace"`` is deliberate on the ENCODE side. UTF-8 can represent
every code point, so it engages only on a lone surrogate -- but when it does,
a mangled character is always the right outcome and a dead guard never is.

This is a no-op on Linux and in CI, where the locale codec is already UTF-8.
"""

from __future__ import annotations

import sys


def force_utf8_streams() -> None:
    """Reconfigure this process's text streams to UTF-8, best effort.

    Never raises: a stream that cannot be reconfigured (a pytest capture
    object, a closed handle, a non-``TextIOWrapper`` replacement) is left
    exactly as it was rather than costing the caller its run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            continue
