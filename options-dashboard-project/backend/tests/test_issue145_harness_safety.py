"""Database-free unit tests for the Issue #145 rehearsal safety controls.

These tests never open a connection: they exercise the disposable-environment
gate, the credential redactor, the resource-ownership cleanup rules, and the
workflow structure that runs the real PostgreSQL 16 rehearsal elsewhere.
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import re

import pytest
import yaml
from sqlalchemy.engine import make_url

HERE = pathlib.Path(__file__).resolve().parent
HARNESS_PATH = HERE / "test_issue145_startup_concurrency.py"
WORKFLOW_PATH = (
    HERE.parents[2] / ".github" / "workflows" / "issue145-behavioral-tests.yaml"
)

BOOTSTRAP = "postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres"
NORMALIZED_BOOTSTRAP = "postgresql+psycopg://postgres:per-run-secret@127.0.0.1:5432/postgres"

spec = importlib.util.spec_from_file_location("issue145_rehearsal_harness", HARNESS_PATH)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


# ---------------------------------------------------------------------------
# Disposable-environment gate
# ---------------------------------------------------------------------------
VALID_ENV = {
    "ISSUE145_REHEARSAL": "1",
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "ISSUE145_DISPOSABLE_CONFIRM": "issue145-disposable",
    "ISSUE145_BOOTSTRAP_DATABASE_URL": BOOTSTRAP,
    "STRIKENOVA_ENV": "",
}


def _env(monkeypatch, **overrides):
    for key in set(VALID_ENV) | set(overrides):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**VALID_ENV, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_gate_accepts_a_complete_hosted_runner_environment(monkeypatch):
    _env(monkeypatch)
    assert harness._require_rehearsal_env() == NORMALIZED_BOOTSTRAP


def test_gate_normalizes_the_bootstrap_url_to_the_psycopg3_driver(monkeypatch):
    _env(monkeypatch)
    url = harness._require_rehearsal_env()
    parsed = make_url(url)
    # psycopg2 is deliberately absent: SQLAlchemy must select the psycopg 3
    # dialect that the application and requirements install.
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.get_backend_name() == "postgresql"
    assert parsed.username == "postgres"
    assert parsed.password == "per-run-secret"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 5432
    assert parsed.database == "postgres"


@pytest.mark.parametrize(
    "raw",
    [
        BOOTSTRAP,
        "postgres://postgres:per-run-secret@127.0.0.1:5432/postgres",
        NORMALIZED_BOOTSTRAP,
    ],
)
def test_gate_normalizes_every_supported_postgres_scheme(monkeypatch, raw):
    _env(monkeypatch, ISSUE145_BOOTSTRAP_DATABASE_URL=raw)
    assert harness._require_rehearsal_env() == NORMALIZED_BOOTSTRAP


def test_gate_still_rejects_non_postgres_bootstrap_urls(monkeypatch):
    _env(monkeypatch, ISSUE145_BOOTSTRAP_DATABASE_URL="sqlite:///./paper_journal.db")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness._require_rehearsal_env()
    assert "must be a PostgreSQL URL" in str(excinfo.value)


def test_harness_connection_urls_keep_the_real_passwords():
    rehearsal = harness._Rehearsal(NORMALIZED_BOOTSTRAP)
    admin = make_url(rehearsal.admin_db_url)
    app = make_url(rehearsal.app_url)
    migrator = make_url(rehearsal.migrator_url)
    for url in (admin, app, migrator):
        assert url.drivername == "postgresql+psycopg"
        assert url.database == rehearsal.database
        assert "***" not in url.password
    assert admin.password == "per-run-secret"
    assert app.username == rehearsal.app_role
    assert app.password == rehearsal.app_password
    assert migrator.username == rehearsal.migrator_role
    assert migrator.password == rehearsal.migrator_password


def test_gate_skips_only_when_the_rehearsal_is_not_requested(monkeypatch):
    _env(monkeypatch, ISSUE145_REHEARSAL=None)
    with pytest.raises(pytest.skip.Exception):
        harness._require_rehearsal_env()


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"GITHUB_ACTIONS": None}, "GITHUB_ACTIONS"),
        ({"GITHUB_ACTIONS": "false"}, "GITHUB_ACTIONS"),
        ({"RUNNER_ENVIRONMENT": None}, "RUNNER_ENVIRONMENT"),
        ({"RUNNER_ENVIRONMENT": "self-hosted"}, "RUNNER_ENVIRONMENT"),
        ({"ISSUE145_DISPOSABLE_CONFIRM": None}, "ISSUE145_DISPOSABLE_CONFIRM"),
        (
            {"ISSUE145_BOOTSTRAP_DATABASE_URL": "postgresql://postgres:pw@10.0.0.5:5432/postgres"},
            "loopback",
        ),
        (
            {"ISSUE145_BOOTSTRAP_DATABASE_URL": "postgresql://postgres:pw@127.0.0.1:5432/app_db"},
            "maintenance databases",
        ),
        (
            {
                "ISSUE145_BOOTSTRAP_DATABASE_URL": (
                    "postgresql://postgres:pw@127.0.0.1:5432/issue145_rehearsal_deadbeef"
                )
            },
            "maintenance databases",
        ),
        (
            {"ISSUE145_BOOTSTRAP_DATABASE_URL": "postgresql://postgres@127.0.0.1:5432/postgres"},
            "password",
        ),
        ({"STRIKENOVA_ENV": "production"}, "production"),
        (
            {"ISSUE145_BOOTSTRAP_DATABASE_URL": "sqlite:///tmp/rehearsal.db"},
            "must be a PostgreSQL URL",
        ),
    ],
)
def test_gate_fails_closed_on_every_inconsistency(monkeypatch, overrides, expected):
    _env(monkeypatch, **overrides)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness._require_rehearsal_env()
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------
def test_redact_removes_postgresql_credentials():
    text = "connection failed for postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres"
    assert harness._redact(text) == (
        "connection failed for postgresql://***@127.0.0.1:5432/postgres"
    )
    assert "per-run-secret" not in harness._redact(text)


def test_redact_handles_an_already_masked_sqlalchemy_url_promptly():
    text = "sqlalchemy://user:***@127.0.0.1:5432/db"
    result = harness._redact(text)
    assert "***@127.0.0.1:5432/db" in result
    assert harness._redact(result) == result


def test_redact_handles_multiple_urls():
    text = (
        "migrator postgresql://m:secret-one@127.0.0.1:5432/db then "
        "app postgresql+psycopg://a:secret-two@127.0.0.1:5432/db"
    )
    result = harness._redact(text)
    assert "secret-one" not in result
    assert "secret-two" not in result
    assert result.count("***@") == 2


def test_redact_leaves_ordinary_text_unchanged():
    text = "worker holder exited rc=0 (no URL here)"
    assert harness._redact(text) == text


def test_redact_is_idempotent():
    text = "postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres"
    once = harness._redact(text)
    assert harness._redact(once) == once


def test_redact_is_single_pass_not_a_fixpoint_loop():
    source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
    body = source.split("def _redact(", 1)[1].split("\ndef ", 1)[0]
    assert "while " not in body
    assert re.search(r"_URL_CREDENTIAL_RE\.sub\(", body)


# ---------------------------------------------------------------------------
# Resource ownership and cleanup
# ---------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, log, fail_on="", error_text=""):
        self.log = log
        self.fail_on = fail_on
        self.error_text = error_text

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise harness.SQLAlchemyError(f"{self.error_text}{sql}")
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def commit(self):  # pragma: no cover - AUTOCOMMIT engine never needs it
        pass


class _FakeEngine:
    def __init__(self, log, fail_on="", error_text=""):
        self.log = log
        self.fail_on = fail_on
        self.error_text = error_text

    def connect(self):
        return _FakeConn(self.log, self.fail_on, self.error_text)

    def dispose(self):
        pass


def _install(monkeypatch, engine):
    monkeypatch.setattr(harness, "create_engine", lambda *a, **k: engine)


def test_teardown_without_owned_resources_never_connects(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("teardown must not connect when nothing was created")

    monkeypatch.setattr(harness, "create_engine", _boom)
    instance = harness._Rehearsal(BOOTSTRAP)
    instance.teardown()
    assert instance.created_database is False
    assert instance.created_roles == []


def test_teardown_drops_only_the_recorded_role_when_no_database_was_created(monkeypatch):
    log = []
    _install(monkeypatch, _FakeEngine(log))
    instance = harness._Rehearsal(BOOTSTRAP)
    instance.created_roles.append("issue145_app_abcd1234")
    instance.teardown()

    joined = "\n".join(log)
    assert 'DROP ROLE IF EXISTS "issue145_app_abcd1234"' in joined
    assert "DROP DATABASE" not in joined
    assert instance.created_roles == []


def test_teardown_drops_only_the_recorded_database_when_no_roles_were_created(monkeypatch):
    log = []
    _install(monkeypatch, _FakeEngine(log))
    instance = harness._Rehearsal(BOOTSTRAP)
    instance.created_database = True
    instance.teardown()

    joined = "\n".join(log)
    assert "DROP DATABASE IF EXISTS" in joined
    assert "DROP ROLE" not in joined
    assert instance.created_database is False


def test_partial_provisioning_records_only_successful_creates(monkeypatch):
    log = []
    _install(monkeypatch, _FakeEngine(log, fail_on="CREATE DATABASE"))
    instance = harness._Rehearsal(BOOTSTRAP)
    with pytest.raises(harness.SQLAlchemyError):
        instance.provision()

    assert instance.created_database is False
    assert instance.created_roles == [instance.migrator_role, instance.app_role]


def test_role_name_collision_does_not_record_the_role(monkeypatch):
    log = []
    _install(monkeypatch, _FakeEngine(log, fail_on="CREATE ROLE"))
    instance = harness._Rehearsal(BOOTSTRAP)
    with pytest.raises(harness.SQLAlchemyError):
        instance.provision()
    assert instance.created_roles == []
    assert instance.created_database is False


def test_successful_provision_records_every_created_resource(monkeypatch):
    log = []
    _install(monkeypatch, _FakeEngine(log))
    instance = harness._Rehearsal(BOOTSTRAP)
    instance.provision()
    assert instance.created_database is True
    assert instance.created_roles == [instance.migrator_role, instance.app_role]


def test_cleanup_failure_is_raised_and_redacted_and_does_not_stop_later_drops(monkeypatch):
    log = []
    error_text = f"could not drop: postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres "
    _install(monkeypatch, _FakeEngine(log, fail_on="DROP DATABASE", error_text=error_text))
    instance = harness._Rehearsal(BOOTSTRAP)
    instance.created_database = True
    instance.created_roles.append("issue145_app_abcd1234")

    with pytest.raises(RuntimeError) as excinfo:
        instance.teardown()

    message = str(excinfo.value)
    assert "rehearsal teardown failed" in message
    assert "per-run-secret" not in message
    assert "***@" in message
    assert any('DROP ROLE IF EXISTS "issue145_app_abcd1234"' in sql for sql in log)


# ---------------------------------------------------------------------------
# Workflow structure
# ---------------------------------------------------------------------------
def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))


def _steps(workflow):
    jobs = workflow["jobs"]
    return jobs["startup-lease-rehearsal"]["steps"]


def test_workflow_triggers_are_preserved():
    triggers = _triggers(_workflow())
    assert triggers["push"]["branches"] == ["issue-145-fix"]
    assert triggers["pull_request"]["branches"] == ["main"]


def test_workflow_uses_postgres_16_with_password_auth():
    job = _workflow()["jobs"]["startup-lease-rehearsal"]
    service = job["services"]["postgres"]
    assert service["image"] == "postgres:16"
    assert service["env"]["POSTGRES_PASSWORD"]
    assert "POSTGRES_HOST_AUTH_METHOD" not in service["env"]


def test_workflow_captures_junit_and_never_uploads_raw_logs():
    steps = _steps(_workflow())
    rehearsal = next(s for s in steps if "startup_concurrency" in str(s.get("run", "")))
    assert "--junitxml" in rehearsal["run"]
    assert ".raw.log" in rehearsal["run"]

    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    paths = upload["with"]["path"]
    assert ".raw.log" not in paths
    assert "issue145-rehearsal.log" in paths


def test_workflow_verifies_three_scenarios_and_fails_on_skips():
    steps = _steps(_workflow())
    verifier = next(s for s in steps if "Redact, print, and verify" in s.get("name", ""))
    script = verifier["run"]
    assert verifier["if"] == "always()"
    for name in (
        "test_two_concurrent_startups_serialize_migrations_and_backfill",
        "test_lease_is_renewed_while_backfill_outlasts_ttl",
        "test_workers_leave_a_clean_leased_database",
    ):
        assert name in script
    assert "len(cases) != 3" in script
    assert "skipped" in script
    assert "sys.exit(1)" in script


def test_workflow_preserves_the_six_module_regression_suite():
    steps = _steps(_workflow())
    regression = next(s for s in steps if "Existing migration/exposure regression" in s.get("name", ""))
    for module in (
        "tests/test_db_migration.py",
        "tests/test_migration_serialization.py",
        "tests/test_migration_identity_separation.py",
        "tests/test_alembic_migrations.py",
        "tests/test_day5_alembic_authority.py",
        "tests/test_leg_exposure.py",
    ):
        assert module in regression["run"]

# ---------------------------------------------------------------------------
# Server identity gate (the server itself, not runner strings)
# ---------------------------------------------------------------------------
def _observation(**overrides):
    base = {
        "version_num": 160004,
        "banner": "16.4",
        "server_addr": "127.0.0.1",
        "server_port": 5432,
        "data_directory": "/var/lib/postgresql/data",
        "uptime_seconds": 42.0,
        "databases": ["postgres", "template0", "template1"],
        "login_roles": ["postgres"],
        "other_client_backends": 0,
        "container_marker": "",
    }
    base.update(overrides)
    return base


def test_server_identity_accepts_a_fresh_service_container():
    harness._validate_job_local_server(_observation(), BOOTSTRAP)


def test_server_identity_tolerates_this_harness_only_leftovers():
    harness._validate_job_local_server(
        _observation(
            databases=["postgres", "template0", "template1", "issue145_rehearsal_ab12"],
            login_roles=["postgres", "issue145_migrator_ab12", "issue145_app_ab12"],
        ),
        BOOTSTRAP,
    )


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"version_num": 150000, "banner": "15.8"}, "PostgreSQL 16"),
        ({"server_addr": "8.8.8.8"}, "routable"),
        ({"server_addr": "8.8.8.8/32"}, "routable"),
        ({"server_addr": ""}, "routable"),
        ({"server_addr": "not-an-address"}, "routable"),
        ({"server_port": 6432}, "listens on port"),
        ({"container_marker": None}, "/.dockerenv"),
        ({"data_directory": "/var/lib/postgresql/16/main"}, "data directory"),
        ({"uptime_seconds": 4000.0}, "running for"),
        (
            {"databases": ["postgres", "template0", "template1", "customer_prod"]},
            "non-system databases",
        ),
        ({"login_roles": ["postgres", "app_user"]}, "unexpected login roles"),
        ({"other_client_backends": 2}, "other client session"),
    ],
)
def test_server_identity_rejects_anything_not_job_local(overrides, expected):
    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness._validate_job_local_server(_observation(**overrides), BOOTSTRAP)
    assert expected in str(excinfo.value)


@pytest.mark.parametrize(
    "server_addr",
    ["127.0.0.1", "127.0.0.1/32", "::1", "172.18.0.2/32", "10.1.0.5"],
)
def test_server_identity_accepts_loopback_and_container_bridge_addresses(server_addr):
    """Service containers publish on loopback but report their bridge address."""
    harness._validate_job_local_server(_observation(server_addr=server_addr), BOOTSTRAP)


def test_server_identity_honours_the_bootstrap_port():
    with pytest.raises(pytest.fail.Exception):
        harness._validate_job_local_server(
            _observation(), "postgresql://postgres:pw@127.0.0.1:6543/postgres"
        )


def test_fixture_probes_the_server_before_creating_anything():
    source = HARNESS_PATH.read_text(encoding="utf-8")
    fixture = source.split("def rehearsal():", 1)[1].split("@pytest.fixture", 1)[0]
    assert fixture.index("assert_job_local_server") < fixture.index("provision()")


# ---------------------------------------------------------------------------
# Worker-side redaction (no raw credential ever reaches disk)
# ---------------------------------------------------------------------------
def _worker_redaction_namespace(monkeypatch):
    monkeypatch.setenv("ISSUE145_REDACT_PATTERN", harness.REDACT_PATTERN)
    namespace = {"re": re, "os": os, "__name__": "issue145_worker_support"}
    exec(compile(harness._WORKER_REDACT_SUPPORT, "<worker-support>", "exec"), namespace)
    return namespace


def test_worker_stream_redacts_a_logged_url(monkeypatch):
    namespace = _worker_redaction_namespace(monkeypatch)
    sink = io.StringIO()
    stream = namespace["_RedactingStream"](sink)
    stream.write(
        "INFO using postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres\n"
    )
    stream.flush()
    written = sink.getvalue()
    assert "per-run-secret" not in written
    assert "postgresql://***@127.0.0.1:5432/postgres" in written


def test_worker_stream_buffers_partial_lines_before_writing(monkeypatch):
    namespace = _worker_redaction_namespace(monkeypatch)
    sink = io.StringIO()
    stream = namespace["_RedactingStream"](sink)
    stream.write("Traceback: postgresql://postgres:per-run-")
    assert sink.getvalue() == ""
    stream.write("secret@127.0.0.1:5432/postgres\n")
    written = sink.getvalue()
    assert "per-run-secret" not in written
    assert written.endswith("\n")


def test_worker_stream_redacts_a_trailing_partial_line_on_flush(monkeypatch):
    namespace = _worker_redaction_namespace(monkeypatch)
    sink = io.StringIO()
    stream = namespace["_RedactingStream"](sink)
    stream.write("fatal postgresql://u:hunter2@h/db")
    stream.flush()
    assert "hunter2" not in sink.getvalue()


def test_worker_stream_leaves_ordinary_output_unchanged(monkeypatch):
    namespace = _worker_redaction_namespace(monkeypatch)
    sink = io.StringIO()
    stream = namespace["_RedactingStream"](sink)
    stream.write("migration lock acquired owner=box:123:abcd\n")
    stream.flush()
    assert sink.getvalue() == "migration lock acquired owner=box:123:abcd\n"


def test_worker_error_marker_redactor_removes_credentials(monkeypatch):
    namespace = _worker_redaction_namespace(monkeypatch)
    text = namespace["redact"](
        "OperationalError: postgresql://postgres:per-run-secret@127.0.0.1:5432/postgres"
    )
    assert "per-run-secret" not in text


def test_worker_script_wires_redaction_before_logging():
    script = harness._WORKER_SCRIPT
    assert "__REDACT_SUPPORT__" not in script
    assert "ISSUE145_REDACT_PATTERN" in script
    assert script.index("sys.stderr = _RedactingStream") < script.index(
        "logging.basicConfig"
    )
    compile(script, "<worker>", "exec")
