"""packaging/components.lock and the install-*.sh scripts pin the same
versions: the installers read the lock file, and their own values (the
fallback without it) must not drift apart."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging" / "components.lock"
INSTALLERS = {
    "install-m17.sh": ["GR_M17_COMMIT"],
    "install-lora.sh": ["GR_LORA_SDR_COMMIT"],
    "install-rade.sh": ["RADE_C_COMMIT"],
    "install-ft8.sh": ["FT8_LIB_COMMIT"],
    "install-js8.sh": ["JS8_REPO", "JS8_TAG", "JS8_COMMIT"],
}


def lock_values():
    out = {}
    for line in LOCK.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(\S+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


@unittest.skipUnless(LOCK.exists() and (ROOT / "install-m17.sh").exists(), "not a full checkout")
class ComponentsLockTests(unittest.TestCase):
    def test_installer_fallbacks_match_the_lock(self):
        lock = lock_values()
        for name, keys in INSTALLERS.items():
            text = (ROOT / name).read_text()
            for key in keys:
                m = re.search(rf'^{key}="([^"]+)"$', text, re.M)
                self.assertIsNotNone(m, f"{name}: no {key}=")
                self.assertEqual(m.group(1), lock[key], f"{name}: {key} differs from components.lock")

    def test_installers_read_the_lock(self):
        for name, keys in INSTALLERS.items():
            text = (ROOT / name).read_text()
            self.assertIn("packaging/components.lock", text, name)
            for key in keys:
                self.assertIn(f"s/^{key}=//p", text, f"{name} does not read {key} from the lock")

    def test_lock_is_valid_shell(self):
        out = subprocess.run(["bash", "-c", f'set -eu; . "{LOCK}"; echo "$GR_M17_COMMIT $JS8_TAG"'],
                             capture_output=True, text=True, check=True).stdout.split()
        lock = lock_values()
        self.assertEqual(out, [lock["GR_M17_COMMIT"], lock["JS8_TAG"]])

    def test_commits_are_full_hashes(self):
        for key, value in lock_values().items():
            if key.endswith("_COMMIT"):
                self.assertRegex(value, r"^[0-9a-f]{40}$", key)


if __name__ == "__main__":
    unittest.main()
