"""Checks the Python environment the Windows scripts run the app from, without changing anything.

``scripts\\check.ps1`` runs this file with that environment's own interpreter (``python -I <this file>``), before
anything else of the app can be imported. So it uses only the standard library, and keeps to syntax old Pythons can
parse, so that it can tell them they are too old. It prints one JSON object,
``{"findings": [{"status": "ok" | "needed" | "warning", "title": ..., "detail": ...}, ...]}``, and exits with 1 when
something is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from importlib import metadata, util
from pathlib import Path

MIN_PYTHON = (3, 12)
DISTRIBUTION = "foundry-model-evaluator"
PACKAGE = "foundry_eval"

# name, [extras], specifiers, ; marker
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*([^;]*?)\s*(?:;\s*(.*?))?\s*$")


@dataclass(frozen=True)
class Finding:
    status: str  # "ok", "needed" or "warning"
    title: str
    detail: str = ""


def check_python(version: Sequence[int], base_prefix: str) -> list[Finding]:
    found = ".".join(str(part) for part in version[:3])
    minimum = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    findings: list[Finding] = []
    if tuple(version[:2]) < MIN_PYTHON:
        findings.append(
            Finding("needed", f"Python {minimum} or newer", f"This environment has Python {found}; create it again.")
        )
    else:
        findings.append(Finding("ok", f"Python {found}", base_prefix))
    if "\\windowsapps\\" in base_prefix.replace("/", "\\").lower():
        findings.append(
            Finding(
                "needed",
                "A Python that is not the Microsoft Store Python",
                "This environment is based on the Microsoft Store Python, which redirects the app's writes under "
                "%LOCALAPPDATA% to a private folder. Create it again from another Python.",
            )
        )
    return findings


def requirement_key(requirement: str) -> tuple[str, frozenset[str], frozenset[str], str]:
    """A requirement in a form that compares equal however it was spelled (as written, or as installed)."""
    match = _REQUIREMENT.match(requirement)
    if match is None:
        return (requirement.strip(), frozenset(), frozenset(), "")
    name, extras, specifiers, marker = match.groups()
    return (
        re.sub(r"[-_.]+", "-", name).lower(),
        frozenset(extra.strip().lower() for extra in (extras or "").split(",") if extra.strip()),
        frozenset(spec.replace(" ", "") for spec in specifiers.split(",") if spec.strip()),
        (marker or "").replace(" ", "").replace("'", '"'),
    )


def check_backend(
    backend: Path,
    python: str,
    package_dir: Path | None,
    recorded: list[str] | None,
    is_installed: Callable[[str], bool],
) -> list[Finding]:
    """``package_dir`` is where ``foundry_eval`` imports from, ``recorded`` the requirements its install recorded."""
    install = f'Install it in editable mode, for example: "{python}" -m pip install -e "{backend}"'
    if package_dir is None:
        return [Finding("needed", "The backend, installed in this environment", install)]
    if not _same_path(package_dir, backend / "src" / PACKAGE):
        return [
            Finding(
                "needed",
                "The backend installed from this checkout",
                f"This environment runs the copy in {package_dir}, not this checkout. {install}",
            )
        ]
    findings = [Finding("ok", "Backend installed from this checkout", str(package_dir))]
    if recorded is None:
        return [*findings, Finding("needed", "The backend's package metadata", f"It was not found. {install}")]
    declared = {requirement_key(req) for req in _declared_dependencies(backend / "pyproject.toml")}
    if declared != {key for key in map(requirement_key, recorded) if "extra" not in key[3]}:
        findings.append(
            Finding(
                "needed",
                "Dependencies matching backend\\pyproject.toml",
                f"They changed since the backend was installed. {install}",
            )
        )
        return findings
    # A requirement with a marker may not apply here, and markers can't be evaluated without `packaging`.
    missing = sorted(key[0] for key in declared if not key[3] and not is_installed(key[0]))
    if missing:
        findings.append(Finding("needed", "All dependencies installed", f"Missing: {', '.join(missing)}. {install}"))
    else:
        findings.append(Finding("ok", "Dependencies match backend\\pyproject.toml"))
    return findings


def _declared_dependencies(pyproject: Path) -> list[str]:
    import tomllib  # Python 3.11+, so only after the version check

    with pyproject.open("rb") as handle:
        return list(tomllib.load(handle)["project"]["dependencies"])


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _package_dir() -> Path | None:
    spec = util.find_spec(PACKAGE)
    return Path(spec.origin).parent if spec is not None and spec.origin else None


def _recorded_requirements() -> list[str] | None:
    try:
        return list(metadata.distribution(DISTRIBUTION).requires or [])
    except metadata.PackageNotFoundError:
        return None


def _is_installed(name: str) -> bool:
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Python environment of Foundry Model Evaluator.")
    parser.add_argument("--backend", type=Path, required=True, help="the checkout's backend folder")
    args = parser.parse_args(argv)
    findings = check_python(sys.version_info[:3], sys.base_prefix)
    if tuple(sys.version_info[:2]) >= MIN_PYTHON:
        backend = args.backend.resolve()
        findings += check_backend(backend, sys.executable, _package_dir(), _recorded_requirements(), _is_installed)
    print(json.dumps({"findings": [asdict(finding) for finding in findings]}))
    return 1 if any(finding.status == "needed" for finding in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
