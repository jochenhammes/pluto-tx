"""pluto_tx/paths.py: environment -> checkout -> package, and XDG user dirs.

Everything against temp directories: REPO_ROOT and the package directories
are patched, so neither a real checkout build nor an installed package on
this machine changes the result.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pluto_tx import paths


def touch(path, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Layout(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "checkout"
        self.pkg_lib = self.tmp / "usr-lib-pluto-tx"
        self.pkg_share = self.tmp / "usr-share-pluto-tx"
        for d in (self.repo, self.pkg_lib, self.pkg_share):
            d.mkdir()
        for name, value in (("REPO_ROOT", self.repo), ("PACKAGE_LIBDIR", self.pkg_lib),
                            ("PACKAGE_SHAREDIR", self.pkg_share)):
            p = mock.patch.object(paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = {k: v for k, v in os.environ.items()
               if k not in (paths.LIBDIR_ENV, "JS8REF") and not k.startswith("XDG_")}
        env["PATH"] = str(self.tmp / "empty-path")   # no lpcnet_demo/js8ref from this machine
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)


class LibraryTests(Layout):
    def test_nothing_built_only_the_bare_name(self):
        self.assertEqual(paths.library_candidates("libft8wrap.so"), ["libft8wrap.so"])

    def test_checkout_before_package(self):
        repo_lib = touch(self.repo / "ft8_lib" / "libft8wrap.so")
        pkg = touch(self.pkg_lib / "lib" / "libft8wrap.so")
        self.assertEqual(paths.library_candidates("libft8wrap.so"), ["libft8wrap.so", str(repo_lib), str(pkg)])

    def test_package_only(self):
        pkg = touch(self.pkg_lib / "lib" / "librade.so")
        self.assertEqual(paths.library_candidates("librade.so"), ["librade.so", str(pkg)])

    def test_environment_first(self):
        env_lib = touch(self.tmp / "custom" / "lib" / "librade.so")
        repo_lib = touch(self.repo / "rade_c" / "build" / "src" / "librade.so")
        with mock.patch.dict(os.environ, {paths.LIBDIR_ENV: str(self.tmp / "custom")}):
            self.assertEqual(paths.library_candidates("librade.so"), ["librade.so", str(env_lib), str(repo_lib)])


class ProgramTests(Layout):
    def test_not_found(self):
        self.assertIsNone(paths.program("lpcnet_demo"))
        self.assertIsNone(paths.program("js8ref"))

    def test_checkout_before_package(self):
        repo = touch(self.repo / "rade_c" / "build" / "src" / "lpcnet_demo", executable=True)
        touch(self.pkg_lib / "bin" / "lpcnet_demo", executable=True)
        self.assertEqual(paths.program("lpcnet_demo"), str(repo))

    def test_package(self):
        pkg = touch(self.pkg_lib / "bin" / "js8ref", executable=True)
        self.assertEqual(paths.program("js8ref"), str(pkg))

    def test_path_before_checkout(self):
        # the launchers of a git installation put rade_c/build/src on PATH
        on_path = touch(self.tmp / "empty-path" / "lpcnet_demo", executable=True)
        touch(self.repo / "rade_c" / "build" / "src" / "lpcnet_demo", executable=True)
        self.assertEqual(paths.program("lpcnet_demo"), str(on_path))

    def test_not_executable_is_skipped(self):
        touch(self.repo / "js8call" / "build-js8ref" / "js8ref")
        pkg = touch(self.pkg_lib / "bin" / "js8ref", executable=True)
        self.assertEqual(paths.program("js8ref"), str(pkg))

    def test_js8ref_variable_wins(self):
        own = touch(self.tmp / "elsewhere" / "js8ref", executable=True)
        touch(self.pkg_lib / "bin" / "js8ref", executable=True)
        with mock.patch.dict(os.environ, {"JS8REF": str(own)}):
            self.assertEqual(paths.program("js8ref"), str(own))

    def test_libdir_variable(self):
        own = touch(self.tmp / "custom" / "bin" / "lpcnet_demo", executable=True)
        touch(self.repo / "rade_c" / "build" / "src" / "lpcnet_demo", executable=True)
        with mock.patch.dict(os.environ, {paths.LIBDIR_ENV: str(self.tmp / "custom")}):
            self.assertEqual(paths.program("lpcnet_demo"), str(own))


class DataTests(Layout):
    def test_jsc(self):
        self.assertIsNone(paths.data_file("jsc.json"))
        pkg = touch(self.pkg_share / "js8" / "jsc.json")
        self.assertEqual(paths.data_file("jsc.json"), str(pkg))
        repo = touch(self.repo / "js8call" / "jsc.json")
        self.assertEqual(paths.data_file("jsc.json"), str(repo))

    def test_is_package(self):
        self.assertFalse(paths.is_package())
        touch(self.repo / paths.PACKAGE_MARKER)
        self.assertTrue(paths.is_package())


class UserDirTests(Layout):
    def test_defaults_under_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}):
            home = self.tmp / "home"
            self.assertEqual(paths.user_dir("config"), home / ".config" / "pluto-tx")
            self.assertEqual(paths.user_dir("state", "web-trx"), home / ".local" / "state" / "web-trx")
            self.assertEqual(paths.user_dir("cache"), home / ".cache" / "pluto-tx")
            self.assertEqual(paths.user_dir("data", "web-trx"), home / ".local" / "share" / "web-trx")

    def test_xdg_variables(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "st"),
                                          "XDG_CONFIG_HOME": "relative/ignored",
                                          "HOME": str(self.tmp / "home")}):
            self.assertEqual(paths.user_dir("state", "web-trx"), self.tmp / "st" / "web-trx")
            # the XDG spec: relative values are invalid and ignored
            self.assertEqual(paths.user_dir("config"), self.tmp / "home" / ".config" / "pluto-tx")

    def test_create(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "c")}):
            d = paths.user_dir("cache", create=True)
            self.assertTrue(d.is_dir())


class VersionTests(unittest.TestCase):
    def test_version_is_a_string(self):
        import pluto_tx
        from pluto_tx.version import version
        self.assertIsInstance(version(), str)
        self.assertTrue(version())
        self.assertEqual(pluto_tx.__version__, version())


if __name__ == "__main__":
    unittest.main()
