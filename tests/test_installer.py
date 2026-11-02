"""./install.sh: its choices, summary and launchers, in a temporary $HOME with fake feature scripts. No sudo, no network."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ["base", "m17", "lora", "rade", "ft8", "wsjtx", "jtty", "js8", "js8call", "espdr", "web-trx"]


FULL_CHECKOUT = (ROOT / "install.sh").exists() and (ROOT / "scripts" / "install").exists()  # not in the installed package


@unittest.skipUnless(FULL_CHECKOUT, "not a full checkout")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.home.mkdir()
        # a copy of the checkout's installer, so that the feature scripts can be replaced by fakes
        self.repo = self.tmp / "repo"
        (self.repo / "packaging").mkdir(parents=True)
        (self.repo / "web-trx" / "backend").mkdir(parents=True)
        (self.repo / "web-trx" / "frontend" / "src").mkdir(parents=True)
        (self.repo / "web-trx" / "backend" / "pyproject.toml").write_text("x")
        (self.repo / "web-trx" / "frontend" / "package-lock.json").write_text("x")
        shutil.copy(ROOT / "install.sh", self.repo)
        shutil.copy(ROOT / "packaging" / "components.lock", self.repo / "packaging")
        self.install = self.repo / "scripts" / "install"
        self.install.mkdir(parents=True)
        for name in ("lib.sh", "features.sh", "write-launchers.sh", "packages-base.txt"):
            shutil.copy(ROOT / "scripts" / "install" / name, self.install)
        self.ran = self.tmp / "ran"
        for feature in FEATURES:
            self.fake(feature, "exit 0")

    def fake(self, feature, body):
        path = self.install / f"{feature}.sh"
        path.write_text(f'#!/usr/bin/env bash\necho {feature} >>"{self.ran}"\n{body}\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_install(self, *args, stdin=None, menu=False, **env_more):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "PLUTO_INSTALL_NO_SUDO": "1", "PLUTO_INSTALL_NO_DOCTOR": "1",
               "XDG_STATE_HOME": str(self.home / "state"), "XDG_CACHE_HOME": str(self.home / "cache"), **env_more}
        if menu:
            env["PLUTO_INSTALL_FORCE_MENU"] = "1"
        return subprocess.run(["bash", str(self.repo / "install.sh"), *args], env=env, input=stdin or "", capture_output=True,
                              text=True, timeout=60)

    def ran_features(self):
        return self.ran.read_text().split() if self.ran.exists() else []


class ChoiceTests(Base):
    def test_named_features_come_after_the_base_in_order(self):
        result = self.run_install("rade", "m17", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran_features(), ["base", "m17", "rade"])

    def test_all_leaves_out_what_is_not_part_of_it(self):
        self.run_install("--all", "--yes", "--force")  # --force: this machine may have jt9 and esptool already
        self.assertEqual(self.ran_features(), [f for f in FEATURES if f != "js8call"])

    def test_without_a_terminal_only_the_base(self):
        self.run_install()
        self.assertEqual(self.ran_features(), ["base"])

    def test_dry_run_and_list_change_nothing(self):
        for args in (["--dry-run", "--all"], ["--list"]):
            result = self.run_install(*args)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self.ran_features(), [])
            self.assertFalse((self.home / ".local").exists())

    def test_unknown_feature_and_option(self):
        for arg in ("nonsense", "--nonsense"):
            result = self.run_install(arg)
            self.assertEqual(result.returncode, 64)
        self.assertEqual(self.ran_features(), [])

    def test_options_go_to_exactly_one_feature(self):
        self.fake("web-trx", 'echo "args:$*" >>"%s"' % (self.tmp / "args"))
        self.run_install("web-trx", "--", "--sim-only")
        self.assertEqual((self.tmp / "args").read_text().strip(), "args:--sim-only")
        self.assertEqual(self.run_install("m17", "rade", "--", "--x").returncode, 64)


class MenuTests(Base):
    def test_numbers_toggle_and_enter_starts(self):
        self.run_install(stdin="2 4\n\n", menu=True)
        self.assertEqual(self.ran_features(), ["base", "m17", "rade"])

    def test_a_selects_all_but_what_is_left_out(self):
        self.run_install(stdin="a\n\n", menu=True)
        self.assertNotIn("js8call", self.ran_features())
        self.assertIn("web-trx", self.ran_features())

    def test_q_stops_without_installing(self):
        self.assertEqual(self.run_install(stdin="q\n", menu=True).returncode, 0)
        self.assertEqual(self.ran_features(), [])

    def test_a_wrong_number_does_not_end_the_menu(self):
        result = self.run_install(stdin="99\nfoo\n\n", menu=True)
        self.assertIn("Keine Nummer", result.stdout)
        self.assertEqual(self.ran_features(), ["base"])


class OutcomeTests(Base):
    def test_a_failed_feature_stops_only_itself(self):
        self.fake("m17", "echo kaputt; exit 1")
        result = self.run_install("m17", "rade", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("rade", self.ran_features())
        self.assertRegex(result.stdout, r"m17\s+FEHLER, siehe .*m17\.log")
        self.assertIn("kaputt", (self.home / "cache/pluto-tx/install/m17.log").read_text())

    def test_a_failed_base_stops_the_rest(self):
        self.fake("base", "exit 1")
        result = self.run_install("m17", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.ran_features(), ["base"])
        self.assertRegex(result.stdout, r"m17\s+nicht versucht")

    def test_skipped_is_not_a_failure(self):
        self.fake("web-trx", "exit 3")
        result = self.run_install("web-trx", "--yes")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"web-trx\s+uebersprungen")

    def test_notices_come_at_the_end(self):
        self.fake("m17", '. "$(dirname "$0")/lib.sh"; notice "Bitte neu anmelden"')
        result = self.run_install("m17", "--yes")
        self.assertLess(result.stdout.index("=== Ergebnis"), result.stdout.index("Bitte neu anmelden"))


class StampTests(Base):
    def installed(self):
        (self.repo / "rade_c" / "build" / "src").mkdir(parents=True)
        (self.repo / "rade_c" / "build" / "src" / "librade.so").write_text("")

    def test_current_is_skipped_and_force_builds_again(self):
        self.installed()
        self.run_install("rade", "--yes")
        self.ran.unlink()
        self.run_install("rade", "--yes")
        self.assertNotIn("rade", self.ran_features() if self.ran.exists() else [])
        self.run_install("rade", "--yes", "--force")
        self.assertIn("rade", self.ran_features())

    def test_a_new_lock_builds_again(self):
        self.installed()
        self.run_install("rade", "--yes")
        lock = self.repo / "packaging" / "components.lock"
        lock.write_text(re.sub(r"^RADE_C_COMMIT=.*$", "RADE_C_COMMIT=" + "0" * 40, lock.read_text(), flags=re.M))
        self.ran.unlink()
        self.run_install("rade", "--yes")
        self.assertIn("rade", self.ran_features())

    def test_a_failed_feature_is_not_stamped(self):
        self.fake("m17", "exit 1")
        self.run_install("m17", "--yes")
        self.assertFalse((self.home / "state/pluto-tx/install/m17").exists())


class LauncherTests(Base):
    def launcher(self, name="pluto-tx"):
        return (self.home / ".local/bin" / name).read_text()

    def test_plain_without_builds(self):
        self.run_install("--yes")
        text = self.launcher()
        self.assertNotIn("LD_LIBRARY_PATH", text)
        self.assertIn(f'cd "{self.repo}" && exec python3 -m pluto_tx.app --gui "$@"', text)
        self.assertIn("pluto_advanced_rx.app", self.launcher("pluto-advanced-rx"))
        self.assertIn("pluto_cli.app", self.launcher("pluto-cli"))

    def test_finds_what_is_built_without_repeating_it(self):
        lib = self.home / ".local/lib/x86_64-linux-gnu"
        lib.mkdir(parents=True)
        (lib / "libgnuradio-m17.so.1").write_text("")
        (lib / "libgnuradio-lora_sdr.so.1").write_text("")
        rade = self.repo / "rade_c/build/src"
        rade.mkdir(parents=True)
        (rade / "librade.so").write_text("")
        self.run_install("--yes")
        text = self.launcher()
        self.assertIn(f'export LD_LIBRARY_PATH="{rade}:{lib}:${{LD_LIBRARY_PATH:-}}"', text)
        self.assertIn(f'export PATH="{rade}:${{PATH:-}}"', text)

    def test_an_old_launcher_is_kept_once(self):
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True)
        (bindir / "pluto-tx").write_text("#!/bin/sh\nold\n")
        self.run_install("--yes")
        self.assertEqual((bindir / "pluto-tx.bak").read_text(), "#!/bin/sh\nold\n")
        self.run_install("--yes", "--force")
        self.assertEqual((bindir / "pluto-tx.bak").read_text(), "#!/bin/sh\nold\n")


@unittest.skipUnless(FULL_CHECKOUT, "not a full checkout")
class ConsistencyTests(unittest.TestCase):
    def test_every_feature_has_a_script_and_a_label(self):
        out = subprocess.run(["bash", "-c", f'HOME=/nonexistent; . "{ROOT}/scripts/install/lib.sh"; . "{ROOT}/scripts/install/features.sh";'
                              ' for f in "${FEATURES[@]}"; do echo "$f|$(feature_label "$f")|$(feature_cost "$f")"; done'],
                             capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual([line.split("|")[0] for line in out], FEATURES)
        for line in out:
            feature, label, cost = line.split("|")
            self.assertTrue(label and cost, feature)
            self.assertTrue((ROOT / "scripts/install" / f"{feature}.sh").exists(), feature)

    def test_base_packages_match_the_debian_package(self):
        base = set()
        for line in (ROOT / "scripts/install/packages-base.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                base.add(line.lstrip("?"))
        control = (ROOT / "packaging/debian/control").read_text()
        package = control[control.index("Package: pluto-tx"):]
        depends = re.search(r"^Depends:(.*?)^Recommends:(.*?)^Suggests:", package, re.M | re.S)
        declared = {p.split()[0] for p in (depends.group(1) + "," + depends.group(2)).replace("\n", " ").split(",") if p.strip()}
        declared = {p for p in declared if not p.startswith("${")}
        # the package's Python runtime and Web-TRX, which a checkout gets from gnuradio and its venv; a Git checkout needs git and curl
        only_package = {"python3", "python3-pyqt5", "python3-numpy", "python3-fastapi", "python3-uvicorn", "python3-websockets",
                        "python3-pydantic", "openssl", "wsjtx"}
        only_checkout = {"gnuradio", "git", "curl"}
        self.assertEqual(declared - base, only_package)
        self.assertEqual(base - declared, only_checkout)

    def test_no_old_script_names_outside_the_history(self):
        files = subprocess.run(["git", "grep", "-lE", r"install-(m17|lora|rade|ft8|js8|jtty|espdr|web-trx)\.sh"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split()
        history = ("backlog/", "docs/JS8_PLAN.md", "docs/RELEASE_PLAN.md", "docs/release/", "web-trx/docs/FT8_PLAN.md",
                   "web-trx/docs/PROJECT_PLAN.md", "PLAN ", "tests/test_installer.py")
        allowed = {"README.md"}  # names the old commands once, so that they can be translated
        self.assertEqual([f for f in files if not f.startswith(history) and f not in allowed], [])

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        scripts = [str(ROOT / "install.sh"), *map(str, (ROOT / "scripts/install").glob("*.sh"))]
        result = subprocess.run(["shellcheck", "-S", "warning", "-x", *scripts], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout[:2000])


class RootTests(Base):
    def test_refuses_root(self):
        fake_id = self.tmp / "bin"
        fake_id.mkdir()
        (fake_id / "id").write_text("#!/bin/sh\necho 0\n")
        (fake_id / "id").chmod(0o755)
        result = self.run_install("--yes", PATH=f"{fake_id}:{os.environ['PATH']}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("root", result.stderr)
        self.assertEqual(self.ran_features(), [])


if __name__ == "__main__":
    unittest.main()
