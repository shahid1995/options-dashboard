"""Fail-closed gate over the pytest logs produced by CI.

The PostgreSQL workflows deliberately keep pytest's exit status in a side file
so the job can upload sanitized evidence before deciding the outcome. This
script makes that decision, for **every** suite, from the two artifacts CI
actually produced:

    python scripts/pytest_gate.py \
        --suite 'integration|80|issue145-application.log|issue145-application.rc' \
        --suite 'regression|181|issue145-regression.log|issue145-regression.rc'

A suite passes only when all of the following hold:

* its recorded exit code exists and is ``0``;
* its pytest log exists;
* the log's authoritative terminal summary reports no ``N failed`` and no
  ``N error`` / ``N errors`` counts;
* that summary reports no ``N skipped`` count (a skipped PostgreSQL suite is a
  false green, not a pass);
* the summary reports a ``N passed`` count;
* ``N`` is at least the declared minimum.

Only pytest's own terminal summary line is trusted. Earlier revisions scanned
every line of the (untrusted) log for ``N <word>`` tokens, so arbitrary test
output containing "999999 failed" could fabricate a failure. The summary line
is the last line that consists solely of ``<count> <status>`` pairs followed by
`` in <seconds>s``, which is the shape pytest prints and test output does not
control.

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
import re
import sys

#: One ``<count> <status>`` pair. ``errors`` is what pytest prints for
#: collection errors; the singular forms appear for single occurrences.
_SUMMARY_PAIR = re.compile(
    r"(\d+) "
    r"(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|warning)"
)
#: pytest appends ``(0:01:10)`` to the duration once a run is over a minute,
#: so the suffix is optional but must be the whole tail of the line.
_SUMMARY_DURATION = re.compile(
    r" in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?$"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _summary_counts(text: str) -> dict[str, int]:
    """Return the counts from pytest's authoritative terminal summary.

    The scan walks the log backwards and accepts the first line that:

    * ends with pytest's `` in <duration>s`` suffix;
    * is composed **only** of ``<count> <status>`` pairs (optionally prefixed
      by a run of ``=`` characters).

    Everything else in the log is untrusted test output and is ignored, so a
    test that prints "999999 failed" cannot change the gate's decision. An
    empty mapping means no unambiguous summary was found, which the caller
    treats as a failure.
    """
    for raw_line in reversed(text.splitlines()):
        line = _ANSI.sub("", raw_line).strip()
        if not line:
            continue
        line = line.strip("=").strip()
        duration = _SUMMARY_DURATION.search(line)
        if duration is None:
            continue
        body = line[: duration.start()].strip()
        if not body:
            continue
        counts: dict[str, int] = {}
        consumed = 0
        valid = True
        for part in body.split(","):
            token = part.strip()
            match = _SUMMARY_PAIR.fullmatch(token)
            if match is None:
                valid = False
                break
            status = match.group(2)
            counts[status] = counts.get(status, 0) + int(match.group(1))
            consumed += 1
        if valid and consumed:
            return counts
    return {}


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
    if not suite.minimum > 0:
        fail(f"declared minimum {suite.minimum} is not positive")
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
    if exit_code is None:
        fail("the pytest exit status is missing or unreadable")
    elif exit_code != 0:
        fail(f"pytest exited {exit_code}")
    if not suite.log.is_file():
        fail(f"no pytest log at {suite.log}")
        return None
    text = suite.log.read_text(encoding="utf-8", errors="replace")

    counts = _summary_counts(text)
    if not counts:
        fail("the log has no authoritative pytest summary line")
        return None

    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if failed:
        fail(f"{failed} failed/errored test(s)")
    skipped = counts.get("skipped", 0)
    if skipped:
        fail(f"{skipped} skipped test(s)")
    passed = counts.get("passed", 0)
    if not passed:
        fail("the summary reports no passed tests")
        return None
    if passed < suite.minimum:
        fail(f"only {passed} test(s) passed, expected at least {suite.minimum}")
    return passed


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
