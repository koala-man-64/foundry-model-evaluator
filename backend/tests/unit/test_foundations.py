from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from foundry_eval.credentials import (
    REGISTRY,
    CredentialStore,
    CredentialStoreError,
    MemoryKeyring,
    RedactingFormatter,
)
from foundry_eval.credentials.redaction import sanitize_error_text
from foundry_eval.db.database import Database
from foundry_eval.instance_lock import InstanceLock, InstanceLockError
from foundry_eval.paths import (
    DataDirError,
    default_data_dir,
    default_static_dir,
    find_repo_root,
    is_within,
    runtime_dir,
    validate_data_dir,
)


# --- data directory -----------------------------------------------------------------------------
def test_windows_default_is_localappdata():
    path = default_data_dir({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, platform="win32")
    assert str(path).endswith("FoundryModelEvaluator")
    assert "AppData" in str(path)


def test_windows_without_localappdata_is_an_error():
    with pytest.raises(DataDirError):
        default_data_dir({}, platform="win32")


def test_override_and_posix_defaults(tmp_path):
    assert default_data_dir({"FME_DATA_DIR": str(tmp_path)}, platform="win32") == tmp_path
    assert default_data_dir({"XDG_DATA_HOME": str(tmp_path)}, platform="linux") == tmp_path / "FoundryModelEvaluator"
    assert default_data_dir({}, platform="linux").name == "FoundryModelEvaluator"


def test_data_dir_inside_repo_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "frontend").mkdir()
    with pytest.raises(DataDirError, match="source repository"):
        validate_data_dir(repo / "data", repo, env={})


def test_data_dir_under_onedrive_is_rejected(tmp_path):
    onedrive = tmp_path / "OneDrive - Contoso"
    onedrive.mkdir()
    with pytest.raises(DataDirError, match="OneDrive"):
        validate_data_dir(onedrive / "FoundryModelEvaluator", None, env={"OneDriveCommercial": str(onedrive)})


def test_valid_data_dir_passes(tmp_path):
    assert (
        validate_data_dir(tmp_path / "ok", None, env={"OneDrive": str(tmp_path / "elsewhere")})
        == (tmp_path / "ok").resolve()
    )


def test_repo_root_detection_finds_this_checkout():
    root = find_repo_root()
    assert root is not None
    assert (root / "backend").is_dir() and (root / "frontend").is_dir()
    assert find_repo_root(Path("/")) is None


def test_static_dir_prefers_a_local_build_then_the_installed_ui(tmp_path):
    repo, data = tmp_path / "repo", tmp_path / "data"
    installed = runtime_dir(data) / "ui"
    # Nothing built yet: point at the repo build so the "not built" hint names it.
    assert default_static_dir(data, repo) == repo / "frontend" / "dist"
    assert default_static_dir(data, None) == installed
    installed.mkdir(parents=True)
    (installed / "index.html").write_text("<!doctype html>")
    assert default_static_dir(data, repo) == installed
    local = repo / "frontend" / "dist"
    local.mkdir(parents=True)
    (local / "index.html").write_text("<!doctype html>")
    assert default_static_dir(data, repo) == local


def test_is_within():
    assert is_within(Path("/a/b/c"), Path("/a/b"))
    assert not is_within(Path("/a/bc"), Path("/a/b"))


# --- instance lock -------------------------------------------------------------------------------
def test_second_instance_is_refused(tmp_path):
    first = InstanceLock(tmp_path / "instance.lock")
    first.acquire()
    try:
        with pytest.raises(InstanceLockError, match="already using"):
            InstanceLock(tmp_path / "instance.lock").acquire()
    finally:
        first.release()
    with InstanceLock(tmp_path / "instance.lock"):
        pass  # free again after release


def test_lock_is_idempotent(tmp_path):
    lock = InstanceLock(tmp_path / "instance.lock")
    lock.acquire()
    lock.acquire()
    lock.release()
    lock.release()


# --- redaction -----------------------------------------------------------------------------------
def test_registered_secret_is_redacted():
    REGISTRY.register("sk-canary-0123456789")
    assert "sk-canary" not in REGISTRY.redact("key=sk-canary-0123456789 used")
    REGISTRY.forget("sk-canary-0123456789")


def test_header_patterns_are_redacted():
    text = 'headers: {"api-key": "abcdef123456", "Authorization": "Bearer eyJhbGciOi.payload"} x-api-key: zzz999zzz'
    cleaned = REGISTRY.redact(text)
    assert "abcdef123456" not in cleaned
    assert "eyJhbGciOi" not in cleaned
    assert "zzz999zzz" not in cleaned


def test_short_values_are_not_registered():
    REGISTRY.register("abc")
    assert REGISTRY.redact("abc") == "abc"


def test_sanitize_error_text_redacts_and_limits():
    REGISTRY.register("secret-value-xyz")
    out = sanitize_error_text("boom secret-value-xyz " + "x" * 5000, limit=100)
    assert out is not None and "secret-value-xyz" not in out
    assert out.endswith("[error text shortened]")
    assert sanitize_error_text(None) is None
    REGISTRY.forget("secret-value-xyz")


def test_redacting_formatter_scrubs_tracebacks():
    REGISTRY.register("tb-secret-123456")
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("failed with tb-secret-123456")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "oops", None, exc_info=True)
        import sys

        record.exc_info = sys.exc_info()
    out = formatter.format(record)
    assert "tb-secret-123456" not in out
    REGISTRY.forget("tb-secret-123456")


# --- credential store ----------------------------------------------------------------------------
def test_memory_store_roundtrip_and_registration():
    store = CredentialStore.from_environment("memory")
    assert store.is_test_backend
    store.set("ref-1", "  key-value-123  ")
    assert store.get("ref-1") == "key-value-123"
    assert store.has("ref-1")
    assert "key-value-123" not in REGISTRY.redact("key-value-123")
    store.delete("ref-1")
    store.delete("ref-1")  # deleting twice is fine
    assert store.get("ref-1") is None


@pytest.mark.parametrize("bad", ["", "   ", "has space", "x" * 3000])
def test_invalid_keys_rejected(bad):
    store = CredentialStore(MemoryKeyring())
    with pytest.raises(ValueError):
        store.set("ref", bad)


def test_unsafe_backends_are_refused(monkeypatch):
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    with pytest.raises(CredentialStoreError, match="No secure credential store"):
        CredentialStore.from_environment(None, platform="linux")


def test_unknown_mode_is_refused():
    with pytest.raises(CredentialStoreError):
        CredentialStore.from_environment("plaintext")


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows Credential Manager")
def test_windows_backend_is_selected_explicitly():
    store = CredentialStore.from_environment(None, platform="win32")
    assert "Windows" in store.backend_name


# --- database ------------------------------------------------------------------------------------
def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    assert db.migrate() >= 1
    version = db.migrate()
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_strict_tables_enforce_column_types(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO price_snapshots (id, profile_id, created_at, currency, processing_tier, pricing_identity, context_json)"
            " VALUES ('s', NULL, 'now', 'USD', 'standard', 'x', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO price_snapshot_rates (snapshot_id, category, tier_above_input, status, currency)"
                " VALUES ('s', 'output', 'not-a-number', 'priced', 'USD')"
            )


def test_money_type_rejects_floats():
    from pydantic import BaseModel, ValidationError

    from foundry_eval.money import Money

    class Model(BaseModel):
        amount: Money

    assert Model(amount="0.000001").model_dump(mode="json") == {"amount": "0.000001"}
    with pytest.raises(ValidationError):
        Model(amount=0.1)  # type: ignore[arg-type]


def test_price_snapshots_are_immutable(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    with db.transaction() as conn:
        # Rates first, then the header that seals the snapshot (as create_snapshot does).
        conn.execute(
            "INSERT INTO price_snapshot_rates (snapshot_id, category, tier_above_input, status, currency, price)"
            " VALUES ('s1', 'output', 0, 'priced', 'USD', '10')"
        )
        conn.execute(
            "INSERT INTO price_snapshots (id, profile_id, created_at, currency, processing_tier, pricing_identity, context_json)"
            " VALUES ('s1', NULL, 'now', 'USD', 'standard', 'x', '{}')"
        )
    with db.connect() as conn:
        for statement in (
            "UPDATE price_snapshots SET currency = 'EUR'",
            "DELETE FROM price_snapshots",
            "UPDATE price_snapshot_rates SET price = '0'",
            "DELETE FROM price_snapshot_rates",
            "INSERT INTO price_snapshot_rates (snapshot_id, category, tier_above_input, status, currency)"
            " VALUES ('s1', 'input_uncached', 0, 'unknown', 'USD')",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(statement)


def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()

    def failing_transaction() -> None:
        with db.transaction() as conn:
            conn.execute("INSERT INTO app_settings (key, value_json) VALUES ('a', '1')")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing_transaction()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM app_settings").fetchone()[0] == 0
