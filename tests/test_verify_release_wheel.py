"""Release checks must fail when a customer cannot use the built artifact.

Run without installing the SDK (or accessing its private Rust dependencies):
python -m unittest discover -s tests -p test_verify_release_wheel.py
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.verify_release_wheel import select_wheel, smoke_install, verify_contents


class ReleaseWheelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dist = Path(self.directory.name)

    def wheel(
        self, tag: str, *, version: str = "2.13.8", files: tuple[str, ...] = ()
    ) -> Path:
        path = self.dist / f"chalkcompute-{version}-{tag}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name in files:
                archive.writestr(name, "fixture")
        return path

    def test_arm_does_not_select_existing_intel_wheel(self) -> None:
        self.wheel("cp312-cp312-manylinux_2_28_x86_64")
        with self.assertRaisesRegex(ValueError, "found 0"):
            select_wheel(self.dist, "3.12", "manylinux_2_28_aarch64")

    def test_source_archive_does_not_satisfy_missing_wheel(self) -> None:
        (self.dist / "chalkcompute-2.13.8.tar.gz").touch()
        with self.assertRaisesRegex(ValueError, "found 0"):
            select_wheel(self.dist, "3.12", "win_amd64")

    def test_selects_matching_python_abi_and_libc(self) -> None:
        self.wheel("cp311-cp311-musllinux_1_2_aarch64")
        self.wheel("cp312-cp311-musllinux_1_2_aarch64")
        self.wheel("cp312-cp312-manylinux_2_28_aarch64")
        expected = self.wheel("cp312-cp312-musllinux_1_2_aarch64")
        self.assertEqual(
            select_wheel(self.dist, "3.12", "musllinux_1_2_aarch64"), expected
        )

    def test_accepts_compressed_platform_tags(self) -> None:
        expected = self.wheel(
            "cp313-cp313-manylinux_2_17_aarch64.manylinux_2_28_aarch64"
        )
        self.assertEqual(
            select_wheel(self.dist, "3.13", "manylinux_2_28_aarch64"), expected
        )

    def test_duplicate_versions_cannot_hide_an_unverified_artifact(self) -> None:
        self.wheel("cp314-cp314-win_amd64", version="2.13.8")
        self.wheel("cp314-cp314-win_amd64", version="2.13.9")
        with self.assertRaisesRegex(ValueError, "found 2"):
            select_wheel(self.dist, "3.14", "win_amd64")

    def test_native_module_required(self) -> None:
        wheel = self.wheel("cp312-cp312-win_amd64", files=("chalkcompute/__init__.py",))
        with self.assertRaisesRegex(ValueError, "No chalkcompute native extension"):
            verify_contents(wheel)

    def test_windows_and_unix_native_extensions_accepted(self) -> None:
        for extension in (
            "_native.cp312-win_amd64.pyd",
            "_native.cpython-312-aarch64-linux-gnu.so",
        ):
            with self.subTest(extension=extension):
                wheel = self.wheel(
                    "cp312-cp312-win_amd64", files=(f"chalkcompute/{extension}",)
                )
                verify_contents(wheel)

    def test_credentials_and_tests_rejected_at_any_depth(self) -> None:
        for filename in (
            ".env",
            "chalkcompute/.env.production",
            "nested/secret.enc",
            "nested/.sops.yaml",
            "tests/sample.py",
            "chalkcompute/test_example.py",
        ):
            with self.subTest(filename=filename):
                wheel = self.wheel(
                    "cp312-cp312-win_amd64",
                    files=("chalkcompute/_native.pyd", filename),
                )
                with self.assertRaisesRegex(ValueError, "Sensitive or test files"):
                    verify_contents(wheel)

    def test_default_smoke_requires_both_packages_without_source_builds(self) -> None:
        wheel = self.dist / "candidate.whl"
        with patch.dict("os.environ", {}, clear=True):
            with patch("scripts.verify_release_wheel.subprocess.run") as run:
                smoke_install(wheel)
        install = next(call for call in run.call_args_list if "install" in call.args[0])
        self.assertIn(str(wheel.resolve()), install.args[0])
        self.assertIn("chalkpy[runtime]", install.args[0])
        self.assertIn("--only-binary=:all:", install.args[0])
        imports = next(call for call in run.call_args_list if "-c" in call.args[0])
        self.assertIn("import chalk;", imports.args[0][-1])
        self.assertIn("import chalkcompute", imports.args[0][-1])

    def test_sdk_only_still_checks_native_clients(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch("scripts.verify_release_wheel.subprocess.run") as run:
                smoke_install(self.dist / "candidate.whl", sdk_only=True)
        install = next(call for call in run.call_args_list if "install" in call.args[0])
        self.assertNotIn("chalkpy[runtime]", install.args[0])
        imports = next(call for call in run.call_args_list if "-c" in call.args[0])
        self.assertNotIn("import chalk;", imports.args[0][-1])
        for native_api in (
            "RemoteCallClient",
            "VolumeNativeClient",
            "notebook_source_dependencies",
        ):
            self.assertIn(native_api, imports.args[0][-1])

    def test_smoke_does_not_inherit_package_indexes_or_pythonpath(self) -> None:
        inherited = {
            "PIP_EXTRA_INDEX_URL": "https://private.example/simple",
            "UV_INDEX_URL": "https://private.example/simple",
            "PYTHONPATH": "/local/source",
            "GIT_CONFIG_COUNT": "1",
        }
        with patch.dict("os.environ", inherited, clear=True):
            with patch("scripts.verify_release_wheel.subprocess.run") as run:
                smoke_install(self.dist / "candidate.whl")
        for call in run.call_args_list:
            environment = call.kwargs["env"]
            for key in inherited:
                self.assertNotIn(key, environment)
            self.assertTrue(call.kwargs["check"])
            self.assertNotEqual(call.kwargs["cwd"], Path.cwd())

    def test_private_build_credentials_block_customer_smoke_test(self) -> None:
        with patch.dict("os.environ", {"CHALK_PRIVATE_READ_TOKEN": "fixture-token"}):
            with patch("scripts.verify_release_wheel.subprocess.run") as run:
                with self.assertRaisesRegex(
                    RuntimeError, "Private repository credentials"
                ):
                    smoke_install(self.dist / "unused.whl")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
