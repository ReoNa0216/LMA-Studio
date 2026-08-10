import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPOSITORY / "packaging/windows/validate_bundle_runtime.py"


def load_validator_module():
    spec = importlib.util.spec_from_file_location(
        "lma_windows_bundle_validator", VALIDATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load bundle validator: {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WindowsBundleValidatorTest(unittest.TestCase):
    def setUp(self):
        self.validator = load_validator_module()
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "python"
        self.bundle = self.root / "bundle"
        self.toc = self.root / "Analysis-00.toc"
        self.pyexpat_source = self.prefix / "DLLs/pyexpat.pyd"
        self.scientific_source = (
            self.prefix / "Lib/site-packages/numpy/_core/_multiarray_umath.pyd"
        )
        self._write(self.pyexpat_source, b"official-pyexpat")
        self._write(self.bundle / "pyexpat.pyd", b"official-pyexpat")
        self._write(self.scientific_source, b"numpy-extension")
        self._write(
            self.bundle / "numpy/_core/_multiarray_umath.pyd", b"numpy-extension"
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _write_toc(self, *extra_rows: tuple[str, str, str]) -> None:
        rows = [
            ("pyexpat.pyd", str(self.pyexpat_source), "EXTENSION"),
            (
                "numpy/_core/_multiarray_umath.pyd",
                str(self.scientific_source),
                "EXTENSION",
            ),
            *extra_rows,
        ]
        self.toc.write_text(repr(rows), encoding="utf-8")

    def test_official_python_embedded_expat_is_valid_without_external_dll(self):
        self._write_toc()

        with (
            mock.patch.object(
                self.validator,
                "imported_dll_names",
                return_value={"python311.dll", "vcruntime140.dll"},
            ),
            mock.patch.object(self.validator, "pe_machine", return_value=0x8664),
        ):
            result = self.validator.audit_bundle(
                self.toc,
                self.bundle,
                python_prefix=self.prefix,
            )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["expat_abi"]["mode"], "embedded")
        self.assertEqual(result["selected_python_prefix"], str(self.prefix.resolve()))

    def test_conda_external_expat_keeps_strict_abi_and_hash_checks(self):
        expat_source = self.prefix / "Library/bin/libexpat.dll"
        self._write(expat_source, b"conda-expat")
        self._write(self.bundle / "libexpat.dll", b"conda-expat")
        self._write_toc(("libexpat.dll", str(expat_source), "BINARY"))

        def fake_abi(path: Path, *, extension: bool):
            if extension:
                return {"ordinals": [72, 73], "names": ["XML_Parse"]}
            return {"ordinals": [1, 72, 73], "names": ["XML_Parse"]}

        with (
            mock.patch.object(
                self.validator,
                "imported_dll_names",
                return_value={"libexpat.dll", "python311.dll"},
            ),
            mock.patch.object(self.validator, "expat_abi", side_effect=fake_abi),
            mock.patch.object(self.validator, "pe_machine", return_value=0x8664),
        ):
            result = self.validator.audit_bundle(
                self.toc,
                self.bundle,
                python_prefix=self.prefix,
            )

        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["expat_abi"]["mode"], "external")
        self.assertEqual(result["expat_abi"]["missing_ordinals"], [])

    def test_external_expat_dependency_cannot_pass_without_its_dll(self):
        self._write_toc()

        with (
            mock.patch.object(
                self.validator,
                "imported_dll_names",
                return_value={"libexpat.dll", "python311.dll"},
            ),
            mock.patch.object(self.validator, "pe_machine", return_value=0x8664),
        ):
            result = self.validator.audit_bundle(
                self.toc,
                self.bundle,
                python_prefix=self.prefix,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any("libexpat.dll" in error for error in result["errors"]),
            result["errors"],
        )

    def test_explicit_prefix_still_rejects_foreign_scientific_binary(self):
        foreign = self.root / "foreign/numpy/_core/_multiarray_umath.pyd"
        self._write(foreign, b"numpy-extension")
        self.scientific_source = foreign
        self._write_toc()

        with (
            mock.patch.object(
                self.validator,
                "imported_dll_names",
                return_value={"python311.dll"},
            ),
            mock.patch.object(self.validator, "pe_machine", return_value=0x8664),
        ):
            result = self.validator.audit_bundle(
                self.toc,
                self.bundle,
                python_prefix=self.prefix,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            len(result["scientific_binaries"]["foreign_sources"]), 1
        )


if __name__ == "__main__":
    unittest.main()
