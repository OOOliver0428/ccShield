"""Publish failed pytest cases as GitHub Actions annotations.

GitHub does not expose raw job logs through the public API.  JUnit output is
stable and machine-readable, so a failed CI run can still identify the exact
test and traceback in its public check annotations.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape_command(value: str) -> str:
    """Escape text for the GitHub workflow command protocol."""

    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("::error title=pytest report::usage: report_pytest_failures.py JUNIT_XML")
        return 2

    report_path = Path(args[0])
    if not report_path.is_file():
        print(
            "::error title=pytest report::"
            f"JUnit report was not created: {_escape_command(str(report_path))}"
        )
        return 1

    root = ET.parse(report_path).getroot()
    failures = 0
    for case in root.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue

        failures += 1
        classname = case.get("classname", "pytest")
        test_name = case.get("name", "unknown test")
        title = _escape_command(f"pytest: {classname}::{test_name}")
        detail = "\n".join(
            part for part in (failure.get("message", ""), failure.text or "") if part
        )
        # GitHub annotations are bounded; the tail contains pytest-timeout's
        # active stack and is the most useful part for diagnosing a hang.
        print(f"::error title={title}::{_escape_command(detail[-6000:])}")

    if failures == 0:
        print("::error title=pytest report::pytest failed without a JUnit failure entry")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
