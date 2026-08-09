#!/usr/bin/env python3
"""Read-only provenance and ABI audit for a built Windows PyInstaller bundle."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable

import pefile


CORE_CONDA_DLLS = (
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "liblzma.dll",
    "libbz2.dll",
    "libexpat.dll",
    "ffi-8.dll",
    "sqlite3.dll",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_toc_rows(value: Any) -> Iterable[tuple[str, str, str]]:
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 3
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], str)
        ):
            yield str(value[0]), str(value[1]), str(value[2])
        for item in value:
            yield from iter_toc_rows(item)


def normalized(path: Path) -> str:
    return str(path.resolve()).replace("/", "\\").casefold()


def is_within(path: Path, root: Path) -> bool:
    normalized_path = normalized(path)
    normalized_root = normalized(root).rstrip("\\")
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root + "\\"
    )


def pe_machine(path: Path) -> int:
    return int(pefile.PE(str(path), fast_load=True).FILE_HEADER.Machine)


def expat_abi(path: Path, *, extension: bool) -> dict[str, Any]:
    pe = pefile.PE(str(path), fast_load=False)
    if extension:
        entry = next(
            row
            for row in pe.DIRECTORY_ENTRY_IMPORT
            if row.dll.decode(errors="replace").casefold() == "libexpat.dll"
        )
        return {
            "ordinals": sorted(
                int(item.ordinal) for item in entry.imports if item.name is None
            ),
            "names": sorted(
                item.name.decode(errors="replace")
                for item in entry.imports
                if item.name is not None
            ),
        }
    exports = pe.DIRECTORY_ENTRY_EXPORT.symbols
    return {
        "ordinals": sorted(int(item.ordinal) for item in exports),
        "names": sorted(
            item.name.decode(errors="replace")
            for item in exports
            if item.name is not None
        ),
    }


def audit_bundle(analysis_toc: Path, bundle_internal: Path) -> dict[str, Any]:
    toc = ast.literal_eval(analysis_toc.read_text(encoding="utf-8"))
    rows = list(dict.fromkeys(iter_toc_rows(toc)))
    by_destination: dict[str, Path] = {}
    for destination, source, type_code in rows:
        if type_code in {"BINARY", "EXTENSION"}:
            by_destination.setdefault(Path(destination).name.casefold(), Path(source))

    errors: list[str] = []
    pyexpat_source = by_destination.get("pyexpat.pyd")
    if pyexpat_source is None:
        errors.append("Analysis TOC does not contain pyexpat.pyd")
        selected_prefix = None
    else:
        selected_prefix = pyexpat_source.parent.parent.resolve()

    dependencies: dict[str, Any] = {}
    if selected_prefix is not None:
        selected_library_bin = selected_prefix / "Library" / "bin"
        for name in CORE_CONDA_DLLS:
            source = by_destination.get(name.casefold())
            bundled = bundle_internal / name
            expected = selected_library_bin / name
            item: dict[str, Any] = {
                "source": str(source) if source else None,
                "bundled": str(bundled),
                "expected": str(expected),
                "expected_exists": expected.is_file(),
            }
            if source is not None and source.is_file():
                item["source_sha256"] = sha256_file(source)
            if bundled.is_file():
                item["bundled_sha256"] = sha256_file(bundled)
            if expected.is_file():
                item["expected_sha256"] = sha256_file(expected)
            dependencies[name] = item

            if expected.is_file():
                if source is None:
                    errors.append(f"{name}: expected selected-environment DLL was not collected")
                elif normalized(source) != normalized(expected):
                    errors.append(
                        f"{name}: foreign source {source}; expected {expected}"
                    )
                if not bundled.is_file():
                    errors.append(f"{name}: missing from bundle")
                elif sha256_file(bundled) != sha256_file(expected):
                    errors.append(
                        f"{name}: bundled hash differs from selected environment"
                    )

    scientific_binary_count = 0
    scientific_foreign_sources: list[str] = []
    scientific_missing_bundle_rows: list[str] = []
    scientific_hash_mismatches: list[str] = []
    scientific_roots = {"numpy", "numpy.libs", "scipy", "scipy.libs"}
    for destination, source_text, type_code in rows:
        destination_root = (
            destination.replace("\\", "/").split("/", 1)[0].casefold()
        )
        if type_code not in {"BINARY", "EXTENSION"} or destination_root not in scientific_roots:
            continue
        scientific_binary_count += 1
        source = Path(source_text)
        bundled = bundle_internal / Path(destination)
        if selected_prefix is not None and not is_within(source, selected_prefix):
            scientific_foreign_sources.append(f"{destination} <- {source}")
        if not bundled.is_file():
            scientific_missing_bundle_rows.append(destination)
        elif not source.is_file() or sha256_file(bundled) != sha256_file(source):
            scientific_hash_mismatches.append(destination)
    if scientific_binary_count == 0:
        errors.append("Analysis TOC contains no NumPy/SciPy binary entries")
    if scientific_foreign_sources:
        errors.append(
            f"NumPy/SciPy contains {len(scientific_foreign_sources)} foreign binary sources"
        )
    if scientific_missing_bundle_rows:
        errors.append(
            f"Bundle is missing {len(scientific_missing_bundle_rows)} NumPy/SciPy binaries"
        )
    if scientific_hash_mismatches:
        errors.append(
            f"Bundle has {len(scientific_hash_mismatches)} NumPy/SciPy binary hash mismatches"
        )

    bundled_pyexpat = bundle_internal / "pyexpat.pyd"
    bundled_expat = bundle_internal / "libexpat.dll"
    abi: dict[str, Any] = {}
    if bundled_pyexpat.is_file() and bundled_expat.is_file():
        extension_abi = expat_abi(bundled_pyexpat, extension=True)
        library_abi = expat_abi(bundled_expat, extension=False)
        missing_ordinals = sorted(
            set(extension_abi["ordinals"]) - set(library_abi["ordinals"])
        )
        missing_names = sorted(
            set(extension_abi["names"]) - set(library_abi["names"])
        )
        abi = {
            "pyexpat_machine": hex(pe_machine(bundled_pyexpat)),
            "libexpat_machine": hex(pe_machine(bundled_expat)),
            "missing_ordinals": missing_ordinals,
            "missing_names": missing_names,
        }
        if pe_machine(bundled_pyexpat) != pe_machine(bundled_expat):
            errors.append("pyexpat.pyd and libexpat.dll have different PE machines")
        if missing_ordinals:
            errors.append(
                "libexpat.dll is missing pyexpat ordinals: "
                + ", ".join(map(str, missing_ordinals))
            )
        if missing_names:
            errors.append(
                "libexpat.dll is missing pyexpat exports: "
                + ", ".join(missing_names)
            )
    else:
        errors.append("Bundle is missing pyexpat.pyd or libexpat.dll")

    return {
        "analysis_toc": str(analysis_toc.resolve()),
        "bundle_internal": str(bundle_internal.resolve()),
        "selected_python_prefix": str(selected_prefix) if selected_prefix else None,
        "dependencies": dependencies,
        "scientific_binaries": {
            "count": scientific_binary_count,
            "foreign_sources": scientific_foreign_sources,
            "missing_bundle_rows": scientific_missing_bundle_rows,
            "hash_mismatches": scientific_hash_mismatches,
        },
        "expat_abi": abi,
        "errors": errors,
        "ok": not errors,
    }


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--analysis-toc",
        type=Path,
        default=repository / "build/lifms_annotation/Analysis-00.toc",
    )
    parser.add_argument(
        "--bundle-internal",
        type=Path,
        default=repository / "dist/LMAStudio/_internal",
    )
    args = parser.parse_args()
    if not args.analysis_toc.is_file():
        parser.error(f"Analysis TOC not found: {args.analysis_toc}")
    if not args.bundle_internal.is_dir():
        parser.error(f"Bundle directory not found: {args.bundle_internal}")
    result = audit_bundle(args.analysis_toc, args.bundle_internal)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
