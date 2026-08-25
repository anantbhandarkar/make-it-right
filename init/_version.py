"""Read the repo's VERSION file. The only Python that parses it.

VERSION is metadata about the product, not a precondition of the security tooling. A
project can `mir init` its own harness with no working internet connection, no pip, and
(per install.sh's own documented path) no python3 at all for the shell half of the tool --
so a missing or unreadable VERSION file must never stop `mir init` from generating a
harness. read_version() therefore never raises: any failure (missing file, unreadable,
empty, garbled) falls back to a placeholder rather than propagating.

One line, no `v` prefix -- the `v` belongs to the git tag (see RELEASING.md). No parser
beyond `.strip()`: reading TOML needs `tomllib`, which is 3.11+, above this repo's 3.9
floor (see docs/v2-plan.md, "Version source of truth").
"""

from __future__ import annotations

import os

FALLBACK = "0.0.0+unknown"

# init/_version.py -> repo root -> VERSION. Resolved from this file's own path, not the
# working directory, so `mir --version` still works when invoked from elsewhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_VERSION_FILE = os.path.join(os.path.dirname(_HERE), "VERSION")


def read_version(path: str = _VERSION_FILE) -> str:
    """Return the version string, or FALLBACK on any failure. Never raises.

    A bare `except Exception` is deliberate and narrow in scope: this function does exactly
    one thing (read one line from one file), so there is nothing here an exception handler
    could mask that matters more than keeping `mir init` running.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        # A blank file is as unusable as a missing one -- fall back rather than report "".
        return text or FALLBACK
    except Exception:
        return FALLBACK


if __name__ == "__main__":
    print(read_version())
