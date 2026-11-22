"""Fail-closed gate over the pytest logs produced by CI.

The PostgreSQL workflows deliberately keep pytest's exit status in a side file
so the job can upload sanitized evidence before deciding the outcome. This
script makes that decision, for **every** suite, from the two artifacts CI
actually produced:

    python scripts/pytest_gate.py \
        --suite 'integration|55|issue145-application.log|issue145-application.rc' \
        --suite 'regression|156|issue145-regression.log|issue145-regression.rc'

A suite passes only when all of the following hold:

* its recorded exit code exists and is ``0``;
* its pytest log exists;
* the log contains no ``N failed`` and no ``N error`` / ``N errors`` counts;
* the log contains no ``N skipped`` count (a skipped PostgreSQL suite is a
  false green, not a pass);
* the log contains a ``N passed`` summary;
* ``N`` is at least the declared minimum.

The minimum is the exact number of tests the suite is expected to run. It is
deliberately exact rather than a round number: dropping or silently skipping
tests lowers the count, so the gate fails until the change is made explicit in
the workflow alongside the test that added or removed coverage.

Exit status: ``0`` all suites pass, ``1`` at least one suite fails, ``2`` the
gate itself was invoked incorrectly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys


def _counts(text: str, words: tuple[str, ...]) -> list[str]:
    """Find every ``N <word>`` count in one linear pass over the log.

    A linear token scan rather than a regular expression: the previous
    patterns were reported by static analysis as super-linear (S8786), and
    the log is untrusted CI input. Semantics are unchanged: each summary
    line such as ``1 failed, 24 passed in 3.2s`` yields the numbers that
    immediately precede the requested words.
    """
    found: list[str] = []
    for line in text.splitlines():
        tokens = line.replace(",", " ").split()
        for index, token in enumerate(tokens):
            if index and token in words and tokens[index - 1].isdigit():
                found.append(tokens[index - 1])
    return found


class Suite:
    def __init__(self, spec: str) -> None:
        parts = spec.split("|")
        if len(parts) != 4:
            raise ValueError(
                f"invalid --suite {spec!r}: expected 'label|min_passed|log|rc'"
            )
        label, minimum, log, rc = parts
        self.label = label.strip()
        self.minimum = int(minimum)
        self.log = pathlib.Path(log.strip())
        self.rc = pathlib.Path(rc.strip())


def check(suite: Suite, errors: list[str]) -> int | None:
    """Validate one suite, appending ``::error::`` lines to ``errors``."""

    def fail(message: str) -> None:
        errors.append(f"::error::{suite.label}: {message}")

    if not suite.label:
        fail("empty suite label")
        return None
    if not suite.rc.is_file():
        fail(f"no recorded pytest exit code at {suite.rc}")
        exit_code = None
    else:
        raw = suite.rc.read_text(encoding="utf-8", errors="replace").strip()
        try:
            exit_code = int(raw)
        except ValueError:
            fail(f"unreadable pytest exit code {raw!r} in {suite.rc}")
            exit_code = None
    if exit_code not in (None, 0):
        fail(f"pytest exited {exit_code}")
    if not suite.log.is_file():
        fail(f"no pytest log at {suite.log}")
        return None
    text = suite.log.read_text(encoding="utf-8", errors="replace")

    failed = _counts(text, ("failed", "error", "errors"))
    if failed:
        fail(f"{', '.join(f + ' failed/errored test(s)' for f in failed)}")
    skipped = _counts(text, ("skipped",))
    if skipped:
        fail(f"{', '.join(s + ' skipped test(s)' for s in skipped)}")
    passed = _counts(text, ("passed",))
    if not passed:
        fail("the log has no 'N passed' summary")
        return None
    count = int(passed[-1])
    if count < suite.minimum:
        fail(f"only {count} test(s) passed, expected at least {suite.minimum}")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytest_gate", description="Fail-closed gate over pytest CI logs."
    )
    parser.add_argument(
        "--suite",
        action="append",
        required=True,
        metavar="LABEL|MIN_PASSED|LOG|RC",
        help="one suite to validate; repeat the flag for each suite",
    )
    args = parser.parse_args(argv)

    try:
        suites = [Suite(spec) for spec in args.suite]
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    for suite in suites:
        count = check(suite, errors)
        if count is not None:
            print(f"{suite.label}: {count} passed (minimum {suite.minimum})")
    if errors:
        for message in errors:
            print(message)
        return 1
    print("all suites passed the gate")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
