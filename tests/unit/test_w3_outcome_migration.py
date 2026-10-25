"""MIG-01..05: synthetic 014 upgrade, work-copy faults and frozen resource identity."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage import outcome_migration
from src.storage.migration_manager import DatabaseState, MigrationManager


MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "migrations"
_STATES = ("draft", "awaiting_review", "approved", "returned", "needs_review")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _connect(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@pytest.fixture
def legacy(tmp_path: Path) -> tuple[Path, list[Path]]:
    """An actual 013 schema, with synthetic history and no live evidence."""

    db = tmp_path / "db" / "knowledge_vault.db"
    manager = MigrationManager(db, MIGRATIONS)
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration.name < "014_":
            manager.apply_migration(migration, auto_backup=False)
    projections = []
    with _connect(db) as conn:
        for ordinal, state in enumerate(_STATES, 1):
            topic_id = f"topic-{state}"
            conn.execute(
                "INSERT INTO outcome_topics(topic_id,title,question,state,revision) VALUES(?,?,?,'active',3)",
                (topic_id, f"Inquiry {state}", "Which evidence supports this claim?"),
            )
            path = tmp_path / "outcomes" / f"{state}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(f"# Historical brief {state}\n".encode())
            projections.append(path)
            conn.execute(
                "INSERT INTO outcome_briefs(outcome_id,topic_id,outcome_type,state,revision,markdown_relative_path,content_sha256) "
                "VALUES(?,?,'research_brief',?,2,?,?)",
                (f"outcome-{state}", topic_id, state, f"outcomes/{state}.md", _sha(path)),
            )
            conn.execute(
                "INSERT INTO outcome_evidence(evidence_id,topic_id,ordinal,locator,locator_kind,knowledge_id,"
                "source_revision_sha256,acquisition_method,retrieval_status,recorded_state) "
                "VALUES(?,?,1,?,'entry',?,'" + "a" * 64 + "','user_selected','not_requested','current')",
                (f"evidence-{state}", topic_id, f"pkv://entries/{ordinal}", ordinal),
            )
            conn.execute(
                "INSERT INTO outcome_claims(claim_id,topic_id,ordinal,statement,state) VALUES(?,?,1,?,'supported')",
                (f"claim-{state}", topic_id, "Synthetic authored claim"),
            )
            conn.execute(
                "INSERT INTO outcome_claim_evidence(claim_id,evidence_id,polarity) VALUES(?,?,'support')",
                (f"claim-{state}", f"evidence-{state}"),
            )
        conn.execute(
            "INSERT INTO outcome_topics(topic_id,title,question,state) VALUES('topic-empty','Empty inquiry','No brief yet','active')"
        )
    return db, projections


def test_mig01_preserves_legacy_history_and_binds_only_current_input(legacy) -> None:
    db, projections = legacy
    original_files = {str(path): path.read_bytes() for path in projections}
    with _connect(db) as conn:
        old_briefs = [dict(row) for row in conn.execute("SELECT * FROM outcome_briefs ORDER BY topic_id")]
        preserved = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("outcome_evidence", "outcome_claims", "outcome_claim_evidence")
        }
    manager = MigrationManager(db, MIGRATIONS)
    inspection = manager.inspect_database()
    assert inspection.state is DatabaseState.UPGRADE_REQUIRED
    assert inspection.pending_versions == ("1.2.8",)
    assert inspection.outcome_legacy_brief_count == 5
    assert inspection.migration_effects == (
        "outcome_input_binding", "legacy_briefs_need_review", "markdown_unchanged", "vectors_unchanged",
    )
    assert manager.apply_all_pending(auto_backup=False) == 1
    assert manager.inspect_database().state is DatabaseState.READY
    with _connect(db) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for before, after in zip(old_briefs, conn.execute("SELECT * FROM outcome_briefs ORDER BY topic_id")):
            assert {key: after[key] for key in before} == before
            assert after["binding_state"] == "legacy_unverified"
            assert all(after[key] is None for key in (
                "publication_id", "rendered_input_revision", "rendered_input_digest", "render_contract_version",
            ))
        for table, before in preserved.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")] == before
        for topic in conn.execute("SELECT * FROM outcome_topics"):
            assert topic["input_revision"] == 1
            assert topic["render_contract_version"] == "research-brief.v2"
            assert topic["input_digest"] == outcome_migration.input_digest_from_connection(conn, dict(topic))
        assert conn.execute("SELECT count(*) FROM outcome_review_receipts").fetchone()[0] == 0
    assert {str(path): path.read_bytes() for path in projections} == original_files


@pytest.mark.parametrize("cut", ["ddl", "backfill", "foreign_keys", "version", "pre_publish"])
def test_mig02_work_copy_failure_preserves_original_bytes(legacy, monkeypatch, cut) -> None:
    db, projections = legacy
    original = _sha(db)
    original_files = [_sha(path) for path in projections]
    if cut == "ddl":
        import src.storage.migration_manager as module
        read = module._read_migration_text

        def fail_ddl(path):
            sql = read(path)
            return sql + "\nINVALID DDL;" if path.name.startswith("014_") else sql
        monkeypatch.setattr(module, "_read_migration_text", fail_ddl)
    elif cut == "backfill":
        backfill = outcome_migration.backfill_outcome_bindings

        def fail_backfill(conn):
            backfill(conn)
            raise OSError("synthetic after backfill")
        monkeypatch.setattr(outcome_migration, "backfill_outcome_bindings", fail_backfill)
    elif cut == "foreign_keys":
        validate = outcome_migration.validate_outcome_schema

        def fail_validation(conn, *, legacy_sql, target_sql=None):
            validate(conn, legacy_sql=legacy_sql, target_sql=target_sql)
            if target_sql is not None and conn.in_transaction:
                raise sqlite3.IntegrityError("synthetic target FK failure")
        monkeypatch.setattr(outcome_migration, "validate_outcome_schema", fail_validation)
    elif cut == "version":
        record = MigrationManager._record_schema_version

        def fail_version(self, conn, version, description):
            record(self, conn, version, description)
            if version == "1.2.8":
                raise OSError("synthetic after version insertion")
        monkeypatch.setattr(MigrationManager, "_record_schema_version", fail_version)
    else:
        import src.storage.migration_manager as module
        replace = module.os.replace

        def fail_publish(source, destination):
            if Path(destination) == db:
                raise OSError("synthetic before publication")
            return replace(source, destination)
        monkeypatch.setattr(module.os, "replace", fail_publish)
    with pytest.raises(PKVRuntimeError):
        MigrationManager(db, MIGRATIONS).apply_all_pending(auto_backup=False)
    assert _sha(db) == original
    assert MigrationManager(db, MIGRATIONS).get_current_version() == "1.2.7"
    assert [_sha(path) for path in projections] == original_files


def test_mig03_post_replace_response_loss_is_idempotent(legacy, monkeypatch) -> None:
    db, _ = legacy
    import src.storage.migration_manager as module
    replace = module.os.replace

    def lose_response(source, destination):
        result = replace(source, destination)
        if Path(destination) == db:
            raise OSError("synthetic response loss after replace")
        return result
    with monkeypatch.context() as scoped:
        scoped.setattr(module.os, "replace", lose_response)
        with pytest.raises(PKVRuntimeError) as caught:
            MigrationManager(db, MIGRATIONS).apply_all_pending(auto_backup=False)
        assert "原数据库保持不变" not in str(caught.value)
    with _connect(db) as conn:
        identities = list(conn.execute("SELECT topic_id,outcome_id,input_digest FROM outcome_topics ORDER BY topic_id"))
        identities = [tuple(row) for row in identities]
    fingerprint = _sha(db)
    fresh_manager = MigrationManager(db, MIGRATIONS, read_only=True)
    assert fresh_manager.inspect_database().current_version == "1.2.8"
    assert MigrationManager(db, MIGRATIONS).apply_all_pending(auto_backup=False) == 0
    assert _sha(db) == fingerprint
    with _connect(db) as conn:
        assert [tuple(row) for row in conn.execute("SELECT topic_id,outcome_id,input_digest FROM outcome_topics ORDER BY topic_id")] == identities


@pytest.mark.parametrize("corruption", ["version_only", "receipt_table", "input_digest", "renderer", "constraint", "quoted_literal"])
def test_mig04_forged_latest_schema_rejected_even_on_no_pending(legacy, corruption) -> None:
    db, _ = legacy
    manager = MigrationManager(db, MIGRATIONS)
    if corruption == "version_only":
        with _connect(db) as conn:
            conn.execute("INSERT INTO schema_version(version,description) VALUES('1.2.8','forged')")
    else:
        manager.apply_all_pending(auto_backup=False)
        with _connect(db) as conn:
            if corruption == "receipt_table":
                conn.execute("DROP TABLE outcome_review_receipts")
            elif corruption == "input_digest":
                conn.execute("UPDATE outcome_topics SET input_digest=? WHERE topic_id='topic-empty'", ("f" * 64,))
            elif corruption == "renderer":
                conn.execute("UPDATE outcome_topics SET render_contract_version='unknown-renderer' WHERE topic_id='topic-empty'")
            else:
                conn.execute("PRAGMA writable_schema=ON")
                if corruption == "quoted_literal":
                    conn.execute("UPDATE sqlite_master SET sql=replace(sql, '''approve''', '''ap prove''') WHERE name='outcome_review_receipts'")
                else:
                    conn.execute("UPDATE sqlite_master SET sql=replace(sql, 'CHECK(typeof(input_revision) = ''integer'' AND input_revision >= 1)', '') WHERE name='outcome_topics'")
                conn.execute("PRAGMA writable_schema=OFF")
    fingerprint = _sha(db)
    with pytest.raises(PKVRuntimeError):
        manager.inspect_database()
    with pytest.raises(PKVRuntimeError):
        manager.apply_all_pending(auto_backup=False)
    assert _sha(db) == fingerprint
    assert manager.run_health_check()["healthy"] is False


def test_mig04_direct_014_refuses_inplace_upgrade(legacy) -> None:
    db, _ = legacy
    fingerprint = _sha(db)
    with pytest.raises(PKVRuntimeError, match="工作副本"):
        MigrationManager(db, MIGRATIONS).apply_migration(MIGRATIONS / "014_add_lifecycle_consistency.sql", auto_backup=False)
    assert _sha(db) == fingerprint


def test_mig01_empty_fresh_schema_and_identity_manifest(tmp_path) -> None:
    db = tmp_path / "db" / "fresh.db"
    manager = MigrationManager(db, MIGRATIONS)
    before = manager.inspect_database()
    assert before.state is DatabaseState.FRESH
    assert len(before.migration_manifest_sha256) == 64
    assert not db.exists()
    assert manager.initialize_fresh().current_version == "1.2.8"
    assert manager.inspect_database().migration_manifest_sha256 == before.migration_manifest_sha256


@pytest.mark.parametrize("resource", ["014_add_lifecycle_consistency.sql", "outcome_migration.py", "identity.py"])
def test_migration_resource_drift_invalidates_runtime_confirmation(tmp_path, monkeypatch, resource) -> None:
    from src.runtime import confirm_runtime_plan, execute_runtime_plan, inspect_runtime, plan_runtime
    from tests.unit.test_runtime_lifecycle import _configured, _FakeProbe
    import src.storage.migration_manager as module

    layout, config = _configured(tmp_path)
    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)
    confirmation = confirm_runtime_plan(plan, allow_network=True)
    original_open = module.open_user_file_nofollow

    def changed_resource(path, mode, **kwargs):
        if Path(path).name == resource and mode == "rb":
            with original_open(path, mode, **kwargs) as handle:
                return io.BytesIO(handle.read() + b"\n# synthetic resource drift\n")
        return original_open(path, mode, **kwargs)
    monkeypatch.setattr(module, "open_user_file_nofollow", changed_resource)
    changed = inspect_runtime(config)
    assert changed.revision != inspection.revision
    assert plan_runtime(changed).plan_id != plan.plan_id
    probe = _FakeProbe()
    with pytest.raises(PKVRuntimeError) as caught:
        execute_runtime_plan(plan, confirmation, provider_probe=probe)
    assert caught.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.user_data_root.exists()


def test_migration_upgrade_impact_is_visible_but_not_executable(legacy, tmp_path) -> None:
    from src.runtime import RuntimeLayout, RuntimeActionKind, inspect_runtime, plan_runtime
    from src.utils.config import Config

    db, _ = legacy
    layout = RuntimeLayout.resolve(
        resources_root=MIGRATIONS.parent.parent,
        user_data_root=db.parent.parent,
        profile_root=tmp_path / "profile",
        environment={},
    )
    fingerprint = _sha(db)
    plan = plan_runtime(inspect_runtime(Config(layout=layout)))
    upgrade = next(action for action in plan.actions if action.kind is RuntimeActionKind.UPGRADE_DATABASE)
    assert upgrade.executable is False
    assert "014" in upgrade.impact
    assert "5" in upgrade.impact
    assert "Markdown" in upgrade.impact
    assert "向量" in upgrade.impact
    assert _sha(db) == fingerprint


def test_migration_original_connections_remain_readonly_until_publication(legacy, monkeypatch) -> None:
    db, _ = legacy
    import src.storage.migration_manager as module
    connect = module.connect_existing_sqlite
    original_opens = []

    def observe(path, *, read_only=False, **kwargs):
        if Path(path) == db:
            original_opens.append(read_only)
            assert read_only is True, "Original database opened writable before atomic publication"
        return connect(path, read_only=read_only, **kwargs)
    monkeypatch.setattr(module, "connect_existing_sqlite", observe)
    manager = MigrationManager(db, MIGRATIONS)
    manager.inspect_database()
    manager.run_health_check()
    assert manager.apply_all_pending(auto_backup=False) == 1
    assert original_opens and all(original_opens)


def test_mig04_broken_bundled_ddl_has_stable_readonly_failure(legacy, monkeypatch) -> None:
    db, _ = legacy
    manager = MigrationManager(db, MIGRATIONS)
    manager.apply_all_pending(auto_backup=False)
    before = _sha(db)
    import src.storage.migration_manager as module
    read = module._read_migration_text

    def broken(path):
        sql = read(path)
        return sql + "\nBROKEN TARGET DDL;" if path.name.startswith("014_") else sql
    monkeypatch.setattr(module, "_read_migration_text", broken)
    with pytest.raises(PKVRuntimeError) as caught:
        manager.inspect_database()
    assert caught.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    assert manager.run_health_check()["healthy"] is False
    assert _sha(db) == before


def test_readiness_binding_validation_uses_one_snapshot(legacy, monkeypatch) -> None:
    db, _ = legacy
    manager = MigrationManager(db, MIGRATIONS)
    manager.apply_all_pending(auto_backup=False)
    with _connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    digest = outcome_migration.input_digest_from_connection
    committed = False

    def interleave(conn, topic):
        nonlocal committed
        if not committed and topic["topic_id"] == "topic-approved":
            committed = True
            with _connect(db) as writer:
                writer.execute("UPDATE outcome_claims SET statement='New committed claim',revision=revision+1 WHERE topic_id='topic-approved'")
                writer.execute("UPDATE outcome_topics SET input_revision=input_revision+1 WHERE topic_id='topic-approved'")
                current = dict(writer.execute("SELECT * FROM outcome_topics WHERE topic_id='topic-approved'").fetchone())
                writer.execute("UPDATE outcome_topics SET input_digest=? WHERE topic_id='topic-approved'", (digest(writer, current),))
                writer.execute("UPDATE outcome_briefs SET state='needs_review' WHERE topic_id='topic-approved'")
        return digest(conn, topic)
    monkeypatch.setattr(outcome_migration, "input_digest_from_connection", interleave)
    assert manager.inspect_database().state is DatabaseState.READY
    assert committed
    assert manager.inspect_database().state is DatabaseState.READY


def test_mig04_legacy_locator_auxiliary_mismatch_is_not_rebound(legacy) -> None:
    db, projections = legacy
    with _connect(db) as conn:
        conn.execute(
            "UPDATE outcome_evidence SET knowledge_id=999 WHERE evidence_id='evidence-approved'"
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    fingerprint = _sha(db)
    projection_bytes = [path.read_bytes() for path in projections]
    with pytest.raises(PKVRuntimeError) as caught:
        MigrationManager(db, MIGRATIONS).apply_all_pending(auto_backup=False)
    assert caught.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    assert _sha(db) == fingerprint
    assert MigrationManager(db, MIGRATIONS).get_current_version() == "1.2.7"
    assert [path.read_bytes() for path in projections] == projection_bytes
