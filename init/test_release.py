"""Release-engineering tests: VERSION, _version.py, `mir --version`. Owned by workstream R.

Discovered and run automatically by test_init.py's `for _name in ... test_*.py` loop, which
requires each such module to define `run(check)` -- see test_init.py's own comment on that
contract. `check` is injected rather than imported so this module pulls in no test framework
of its own and still counts into test_init.py's single tally.
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import _version  # noqa: E402


def run(check) -> None:
    print("_version.read_version()")
    real_version = _version.read_version()
    # Not hardcoded to "1.1.0": the whole point of a root VERSION file is that bumping it is
    # a one-line edit, and a test pinned to today's number would go stale (and lie about
    # passing) the moment RELEASING.md's own procedure is followed.
    check("reads the real VERSION file, not the fallback",
          real_version != _version.FALLBACK, real_version)
    check("no 'v' prefix -- the v belongs to the git tag, not the file",
          not real_version.startswith("v"), real_version)
    check("matches the on-disk VERSION file byte-for-byte (modulo trailing whitespace)",
          real_version == open(os.path.join(REPO_ROOT, "VERSION"), encoding="utf-8")
          .read().strip())
    check("never raises on a missing file, and returns the documented fallback",
          _version.read_version("/does/not/exist/VERSION") == _version.FALLBACK)
    check("never raises on a path that is a directory, not a file",
          _version.read_version(HERE) == _version.FALLBACK)

    print("mir --version (the make-or-break: required=True on the subparser)")
    # cli.py's subparsers are `dest="cmd", required=True`. A bare `mir` with no subcommand
    # must therefore fail -- that is the control that proves the subparser really is
    # required, so a green --version test below cannot be explained by a lenient parser.
    r = subprocess.run([sys.executable, os.path.join(HERE, "cli.py")],
                        capture_output=True, text=True, cwd=REPO_ROOT)
    check("bare `mir` with no subcommand and no --version fails (proves required=True)",
          r.returncode != 0, f"rc={r.returncode}")

    # The actual regression: argparse's `version` action calls parser.exit() before the
    # required-subparser check runs, so `--version` alone must still exit 0 despite the line
    # above. This is ordering inside argparse, not a documented contract -- see the comment
    # at the --version add_argument call in cli.py -- so it needs its own pinned test rather
    # than living only as a claim in a docstring.
    r = subprocess.run([sys.executable, os.path.join(HERE, "cli.py"), "--version"],
                        capture_output=True, text=True, cwd=REPO_ROOT)
    check("`mir --version` exits 0 despite the required subparser",
          r.returncode == 0, f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
    check("`mir --version` prints the VERSION file's contents",
          real_version in r.stdout, r.stdout)
    check("`mir --version` names the tool",
          r.stdout.startswith("mir "), r.stdout)

    # --version must win even when it is not the first token relative to other global
    # flags, and even when a (bogus) subcommand-shaped token follows it -- argparse's
    # version action fires the moment it is consumed, regardless of position.
    r = subprocess.run([sys.executable, os.path.join(HERE, "cli.py"), "--version", "init"],
                        capture_output=True, text=True, cwd=REPO_ROOT)
    check("`mir --version init` still exits 0 -- version wins over a trailing subcommand",
          r.returncode == 0, f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")

    # Confirm the exact framing from docs/v2-plan.md: bare `mir --version` "works despite
    # required=True on the subparser ... because argparse's version action calls
    # parser.exit() before the required check runs -- but that is ordering, not design."
    # If argparse ever changes that ordering, THIS is the assertion that goes red first,
    # not a user filing a bug that `mir --version` broke.
    check("verified on this interpreter's argparse: required check does not preempt --version",
          r.returncode == 0)
