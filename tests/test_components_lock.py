"""packaging/components.lock is the one place for the pinned versions: scripts/install/ and the .deb build read it, and no
script keeps a copy that could drift apart."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging" / "components.lock"
INSTALL = ROOT / "scripts" / "install"
READS = {
    "m17.sh": ["GR_M17_REPO", "GR_M17_COMMIT"],
    "lora.sh": ["GR_LORA_SDR_REPO", "GR_LORA_SDR_COMMIT"],
    "rade.sh": ["RADE_C_REPO", "RADE_C_COMMIT"],
    "ft8.sh": ["FT8_LIB_REPO", "FT8_LIB_COMMIT"],
    "js8.sh": ["JS8_REPO", "JS8_TAG", "JS8_COMMIT"],
    "jtty.sh": ["WSJT_CODEC_TAG", "WSJT_CODEC_SHA256_X86_64", "WSJT_CODEC_SHA256_AARCH64"],
}


def lock_values():
    out = {}
    for line in LOCK.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(\S+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


@unittest.skipUnless(LOCK.exists() and INSTALL.exists(), "not a full checkout")
class ComponentsLockTests(unittest.TestCase):
    def test_install_scripts_read_the_lock(self):
        lock = lock_values()
        for name, keys in READS.items():
            text = (INSTALL / name).read_text()
            for key in keys:
                self.assertIn(key, lock)
                self.assertIn(f"lock_get {key}", text, f"{name} does not read {key} from the lock")

    def test_no_script_keeps_a_copy_of_a_pinned_value(self):
        pinned = [v for k, v in lock_values().items() if k.endswith(("_COMMIT", "_SHA256_X86_64", "_SHA256_AARCH64"))]
        for path in [ROOT / "install.sh", *INSTALL.iterdir()]:
            text = path.read_text()
            for value in pinned:
                self.assertNotIn(value, text, path.name)

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
