"""Fail-closed gate over the pytest JUnit reports produced by CI.

The PostgreSQL workflows keep pytest's exit status in a side file and write a
JUnit XML report, so the job can upload sanitized evidence before deciding the
outcome. This script makes that decision, for **every** suite, from the
artifacts CI actually produced:

    python scripts/pytest_gate.py \
        --suite 'integration|80|issue145-application.xml|issue145-application.rc' \
        --suite 'regression|181|issue145-regression.xml|issue145-regression.rc'

A suite passes only when all of the following hold:

* its recorded exit code exists, is readable and is exactly ``0``;
* its JUnit XML report exists and parses;
* the report has a ``testsuite``/``testsuites`` root with at least one
  ``testsuite`` and every suite carries consistent, non-negative ``tests``,
  ``failures``, ``errors`` and ``skipped`` counts;
* the recorded counts agree with the ``testcase`` elements actually present
  (a report that claims more or fewer tests than it lists is rejected);
* there are no failures, errors or skips;
* the derived passed count (``tests - failures - errors - skipped``) is at
  least the declared minimum.

The JUnit XML is the only acceptance-count source. The text logs are retained
for diagnostics and are never parsed by this gate, so arbitrary test or
teardown output — including a fabricated ``999999 failed`` or ``999 passed``
line before or after the real summary — cannot change the decision.

The minimum is a floor, not a target: dropping or silently skipping tests
lowers the passed count and fails the gate until the change is made explicit
in the workflow alongside the test that added or removed coverage.

Exit status: ``0`` all suites pass, ``1`` at least one suite fails, ``2`` the
gate itself was invoked incorrectly.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET

#: The largest report the gate will read. A CI report is far smaller; the cap
#: keeps a corrupt or hostile artifact from consuming the runner's memory.
_MAX_REPORT_BYTES = 64 * 1024 * 1024


class GateError(Exception):
    """The report cannot be trusted as acceptance evidence."""


def _count_attr(element: ET.Element, name: str) -> int:
    raw = element.get(name)
    if raw is None:
        raise GateError(f"<{element.tag}> is missing the {name!r} attribute")
    try:
        value = int(raw)
    except ValueError:
        raise GateError(
            f"<{element.tag}> has a non-integer {name!r} attribute ({raw!r})"
        ) from None
    if value < 0:
        raise GateError(
            f"<{element.tag}> has a negative {name!r} attribute ({value})"
        )
    return value


def _suite_counts(suite: ET.Element) -> tuple[int, int, int, int]:
    """Validate one ``<testsuite>`` and return (tests, failures, errors, skipped).

    The declared counters must agree with the ``testcase`` elements the report
    actually lists. That cross-check is what stops a report whose summary says
    everything passed while individual cases are marked failed/skipped.
    """
    tests = _count_attr(suite, "tests")
    failures = _count_attr(suite, "failures")
    errors = _count_attr(suite, "errors")
    skipped = _count_attr(suite, "skipped")
    if failures + errors + skipped > tests:
        raise GateError(
            f"<testsuite {suite.get('name', '?')!r}> reports "
            f"{failures} failure(s), {errors} error(s) and {skipped} skip(s) "
            f"for only {tests} test(s)"
        )
    if suite.find("testsuite") is not None:
        raise GateError(
            f"<testsuite {suite.get('name', '?')!r}> contains a nested "
            "<testsuite>; nested reports are ambiguous and are rejected"
        )

    cases = suite.findall("testcase")
    if len(cases) != tests:
        raise GateError(
            f"<testsuite {suite.get('name', '?')!r}> declares {tests} test(s) "
            f"but lists {len(cases)} <testcase> element(s)"
        )

    observed_failures = sum(1 for case in cases if case.find("failure") is not None)
    observed_errors = sum(1 for case in cases if case.find("error") is not None)
    observed_skipped = sum(1 for case in cases if case.find("skipped") is not None)
    if (observed_failures, observed_errors, observed_skipped) != (
        failures,
        errors,
        skipped,
    ):
        raise GateError(
            f"<testsuite {suite.get('name', '?')!r}> counters do not match its "
            f"<testcase> elements (declared failures/errors/skips "
            f"{failures}/{errors}/{skipped}, observed "
            f"{observed_failures}/{observed_errors}/{observed_skipped})"
        )
    return tests, failures, errors, skipped


def _read_report(path: pathlib.Path) -> tuple[int, int, int, int]:
    """Parse and validate a JUnit XML report; return summed counts."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise GateError(f"cannot read {path} ({type(exc).__name__})") from None
    if size <= 0:
        raise GateError(f"{path} is empty")
    if size > _MAX_REPORT_BYTES:
        raise GateError(f"{path} is larger than {_MAX_REPORT_BYTES} bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateError(f"cannot read {path} ({type(exc).__name__})") from None
    # JUnit is a plain data document. External entities/DOCTYPEs are never
    # needed and are refused rather than handed to the XML parser.
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise GateError(f"{path} contains a DOCTYPE/ENTITY declaration")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise GateError(f"{path} is not well-formed XML ({exc})") from None

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = [child for child in root if child.tag == "testsuite"]
        if len(suites) != len(list(root)):
            raise GateError(
                f"{path} has a <testsuites> root with non-<testsuite> children"
            )
    else:
        raise GateError(
            f"{path} has root <{root.tag}>, expected <testsuite> or <testsuites>"
        )
    if not suites:
        raise GateError(f"{path} contains no <testsuite>")

    totals = [0, 0, 0, 0]
    for suite in suites:
        counts = _suite_counts(suite)
        for index, value in enumerate(counts):
            totals[index] += value
    return totals[0], totals[1], totals[2], totals[3]


class Suite:
    def __init__(self, spec: str) -> None:
        parts = spec.split("|")
        if len(parts) != 4:
            raise ValueError(
                f"invalid --suite {spec!r}: expected 'label|min_passed|xml|rc'"
            )
        label, minimum, report, rc = parts
        self.label = label.strip()
        self.minimum = int(minimum)
        self.report = pathlib.Path(report.strip())
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
    if not suite.report.is_file():
        fail(f"no JUnit XML report at {suite.report}")
        return None
    try:
        tests, failures, errors_count, skipped = _read_report(suite.report)
    except GateError as exc:
        fail(str(exc))
        return None

    if failures or errors_count:
        fail(f"{failures + errors_count} failed/errored test(s) in the JUnit report")
    if skipped:
        fail(f"{skipped} skipped test(s) in the JUnit report")
    passed = tests - failures - errors_count - skipped
    if passed <= 0:
        fail("the JUnit report contains no passed tests")
        return None
    if passed < suite.minimum:
        fail(f"only {passed} test(s) passed, expected at least {suite.minimum}")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pytest_gate", description="Fail-closed gate over pytest JUnit reports."
    )
    parser.add_argument(
        "--suite",
        action="append",
        required=True,
        metavar="LABEL|MIN_PASSED|XML|RC",
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
