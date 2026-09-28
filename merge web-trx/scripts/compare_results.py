"""Compares two test runs per test ID -- the baseline (before the merge)
and the run on the merged tree.

  python3 compare_results.py unittest BASE.json NEW.json [options]
  python3 compare_results.py junit    BASE.xml  NEW.xml  [options]

Options:
  --renamed OLD=NEW   a test that was renamed on purpose (repeatable; the
                      ID part after the last '.' or '::' is enough)
  --allow-new-skip    new tests may be skipped (only for dry runs on a
                      machine without GNU Radio -- never on the radio server)

Rules: a test that passed before must still pass. A test skipped before may
now pass or still be skipped. A test that was already red stays a warning
(pre-existing, not caused by the merge). Every new test must pass.
Exit code 0 = no regression.
"""
import json
import sys
import xml.etree.ElementTree as ET


def load_unittest(path):
    with open(path) as f:
        return json.load(f)


def load_junit(path):
    outcomes = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None:
            outcome = "fail"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skip"
        else:
            outcome = "pass"
        outcomes[test_id] = outcome
    return outcomes


def short(test_id):
    return test_id.replace("::", ".").rsplit(".", 1)[-1]


def main(argv):
    kind, base_path, new_path, *rest = argv
    renamed, allow_new_skip = {}, False
    it = iter(rest)
    for arg in it:
        if arg == "--renamed":
            old, new = next(it).split("=", 1)
            renamed[old] = new
        elif arg == "--allow-new-skip":
            allow_new_skip = True
        else:
            raise SystemExit(f"unknown option {arg}")
    load = load_unittest if kind == "unittest" else load_junit
    base, new = load(base_path), load(new_path)

    def mapped(test_id):
        name = short(test_id)
        for old, new_name in renamed.items():
            if name == old or name.startswith(old + "["):
                return test_id.replace(old, new_name)
        return test_id

    problems, warnings, seen = [], [], set()
    for test_id, before in sorted(base.items()):
        target = mapped(test_id)
        after = new.get(target)
        seen.add(target)
        if after is None:
            problems.append(f"MISSING  {test_id} (was {before})")
        elif before == "pass" and after != "pass":
            problems.append(f"REGRESS  {test_id}: pass -> {after}")
        elif before == "skip" and after not in ("pass", "skip"):
            problems.append(f"REGRESS  {test_id}: skip -> {after}")
        elif before in ("fail", "error"):
            warnings.append(f"PRE-RED  {test_id}: {before} -> {after} (already red in the baseline)")
    added = {k: v for k, v in new.items() if k not in seen}
    for test_id, after in sorted(added.items()):
        if after == "pass" or (after == "skip" and allow_new_skip):
            continue
        problems.append(f"NEW-RED  {test_id}: {after}")

    counts = {}
    for v in new.values():
        counts[v] = counts.get(v, 0) + 1
    print(f"baseline: {len(base)} tests, now: {len(new)} tests ({len(added)} new), outcomes now: {counts}")
    for line in warnings:
        print("  " + line)
    for line in problems:
        print("  " + line)
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    print("no regressions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
