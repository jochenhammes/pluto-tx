"""Runs `python3 -m unittest discover tests` (from the current directory)
and also writes every test's outcome to a JSON file, so a baseline and a
later run can be compared per test ID (compare_results.py).

Usage: python3 unittest_json.py OUT.json [discover start dir, default: tests]
Exit code like unittest: 0 all passed, 1 otherwise.
"""
import json
import os
import sys
import unittest


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "pass"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = "fail"

    def addError(self, test, err):
        super().addError(test, err)
        # setUpClass/module import errors come as _ErrorHolder with their own id
        self.outcomes[getattr(test, "id", lambda: str(test))()] = "error"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "skip"

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.outcomes[test.id()] = "xfail"

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcomes[test.id()] = "xpass"

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.outcomes[test.id()] = "fail"


def main():
    # like `python3 -m unittest`: the current directory (repo root) is importable
    sys.path.insert(0, os.getcwd())
    out_path = sys.argv[1]
    start = sys.argv[2] if len(sys.argv) > 2 else "tests"
    suite = unittest.defaultTestLoader.discover(start)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult)
    result = runner.run(suite)
    with open(out_path, "w") as f:
        json.dump(dict(sorted(result.outcomes.items())), f, indent=1)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
