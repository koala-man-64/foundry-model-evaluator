from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from foundry_eval import environment_check
from foundry_eval.environment_check import MIN_PYTHON, check_backend, check_python, requirement_key

BACKEND = Path(__file__).resolve().parents[2]
CHECKER = Path(environment_check.__file__)


def _backend(tmp_path: Path, *dependencies: str) -> Path:
    (tmp_path / "src" / "foundry_eval").mkdir(parents=True)
    listed = ", ".join(f'"{dep}"' for dep in dependencies)
    (tmp_path / "pyproject.toml").write_text(f"[project]\nname = 'x'\ndependencies = [{listed}]\n", encoding="utf-8")
    return tmp_path


def _statuses(findings: list[environment_check.Finding]) -> list[tuple[str, str]]:
    return [(finding.status, finding.title) for finding in findings]


# --- the file itself ----------------------------------------------------------------------------
def test_minimum_python_matches_pyproject():
    with (BACKEND / "pyproject.toml").open("rb") as handle:
        requires = tomllib.load(handle)["project"]["requires-python"]
    assert requires == f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]}"


def test_old_pythons_can_parse_it_to_say_they_are_too_old():
    ast.parse(CHECKER.read_text(encoding="utf-8"), feature_version=(3, 8))


def test_runs_in_this_environment_with_only_the_standard_library():
    result = subprocess.run(
        [sys.executable, "-I", str(CHECKER), "--backend", str(BACKEND)],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = json.loads(result.stdout)["findings"]
    assert result.returncode == 0, findings
    assert [finding["status"] for finding in findings] == ["ok", "ok", "ok"]


# --- Python -------------------------------------------------------------------------------------
def test_a_supported_python_is_ok():
    assert _statuses(check_python((3, 13, 1), r"C:\Python313")) == [("ok", "Python 3.13.1")]


def test_an_old_python_is_needed():
    findings = check_python((3, 10, 4), r"C:\Python310")
    assert _statuses(findings) == [("needed", "Python 3.12 or newer")]
    assert "3.10.4" in findings[0].detail


def test_the_microsoft_store_python_is_refused():
    prefix = r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_3.13.1_x64__qbz5n2kfra8p0"
    assert ("needed", "A Python that is not the Microsoft Store Python") in _statuses(check_python((3, 13, 1), prefix))


# --- requirements -------------------------------------------------------------------------------
def test_requirements_compare_equal_however_they_were_written():
    assert requirement_key("anthropic>=1.14,<2") == requirement_key("anthropic<2,>=1.14")
    assert requirement_key("Foo_Bar[B, a] >= 1 ; python_version<'3.13'") == requirement_key(
        'foo-bar[a,b]>=1; python_version < "3.13"'
    )
    assert requirement_key("anthropic>=1.14,<2") != requirement_key("anthropic>=1.20,<2")


# --- backend ------------------------------------------------------------------------------------
def test_an_editable_install_of_this_checkout_with_its_dependencies_is_ok(tmp_path: Path):
    backend = _backend(tmp_path, "anthropic>=1.14,<2", "truststore>=0.10")
    findings = check_backend(
        backend,
        "python.exe",
        backend / "src" / "foundry_eval",
        ["anthropic<2,>=1.14", "truststore>=0.10", 'pytest; extra == "test"'],
        lambda name: True,
    )
    assert _statuses(findings) == [
        ("ok", "Backend installed from this checkout"),
        ("ok", "Dependencies match backend\\pyproject.toml"),
    ]


def test_a_missing_backend_is_needed_with_the_install_command(tmp_path: Path):
    backend = _backend(tmp_path)
    findings = check_backend(backend, r"C:\venv\Scripts\python.exe", None, None, lambda name: True)
    assert _statuses(findings) == [("needed", "The backend, installed in this environment")]
    assert f'"C:\\venv\\Scripts\\python.exe" -m pip install -e "{backend}"' in findings[0].detail


def test_a_backend_installed_from_elsewhere_is_needed(tmp_path: Path):
    backend = _backend(tmp_path / "here")
    other = tmp_path / "site-packages" / "foundry_eval"
    findings = check_backend(backend, "python.exe", other, [], lambda name: True)
    assert _statuses(findings) == [("needed", "The backend installed from this checkout")]
    assert str(other) in findings[0].detail


def test_missing_package_metadata_is_needed(tmp_path: Path):
    backend = _backend(tmp_path)
    findings = check_backend(backend, "python.exe", backend / "src" / "foundry_eval", None, lambda name: True)
    assert _statuses(findings)[-1] == ("needed", "The backend's package metadata")


@pytest.mark.parametrize(
    "recorded",
    [
        ["anthropic<2,>=1.14"],  # a dependency was added
        ["anthropic<2,>=1.14", "truststore>=0.9"],  # a range changed
        ["anthropic<2,>=1.14", "truststore>=0.10", "regex"],  # a dependency was removed
    ],
)
def test_dependencies_changed_since_the_install_are_needed(tmp_path: Path, recorded: list[str]):
    backend = _backend(tmp_path, "anthropic>=1.14,<2", "truststore>=0.10")
    findings = check_backend(backend, "python.exe", backend / "src" / "foundry_eval", recorded, lambda name: True)
    assert _statuses(findings)[-1] == ("needed", "Dependencies matching backend\\pyproject.toml")


def test_uninstalled_dependencies_are_named(tmp_path: Path):
    backend = _backend(tmp_path, "anthropic>=1.14,<2", "truststore>=0.10", "pywin32; sys_platform == 'win32'")
    recorded = ["anthropic<2,>=1.14", "truststore>=0.10", 'pywin32; sys_platform == "win32"']
    findings = check_backend(
        backend, "python.exe", backend / "src" / "foundry_eval", recorded, lambda name: name == "anthropic"
    )
    assert _statuses(findings)[-1] == ("needed", "All dependencies installed")
    assert "Missing: truststore." in findings[-1].detail  # pywin32 has a marker, so it may not apply here
