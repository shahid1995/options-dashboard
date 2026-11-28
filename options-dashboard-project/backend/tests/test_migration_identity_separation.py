"""ADR-016: runtime/migration database identity separation.

When STRIKENOVA_MIGRATION_DATABASE_URL is set, Alembic must run under the
dedicated migration identity; otherwise behavior is identical to the
historical single-identity model. These tests are hermetic: no module
reloads, no shared-state mutation, no destructive operations.
"""
import contextlib
import os
from unittest.mock import patch

from app.config import Settings
from app.db import _migration_engine_url, assert_guarded_migration_configuration


class TestMigrationIdentityResolution:
    def test_default_uses_runtime_url_when_no_migration_url(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app", STRIKENOVA_MIGRATION_DATABASE_URL=None)
        with patch("app.db.settings", s):
            # Fallback is the runtime identity URL (historical behavior) —
            # resolved from settings and normalized, exactly what the runtime
            # engine is built from in every real deployment.
            assert _migration_engine_url() == "postgresql+psycopg://runtime:pw@h:26257/app"

    def test_migration_url_takes_precedence_when_set(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            # Returned through normalize_database_url (ADR-014 dialect rules).
            assert _migration_engine_url() == "postgresql+psycopg://migrator:pw@h:26257/app"

    def test_migration_url_is_normalized_to_supported_dialect(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="cockroachdb+psycopg://migrator:pw@h:26257/app?sslmode=require",
        )
        with patch("app.db.settings", s):
            assert _migration_engine_url() == (
                "cockroachdb+psycopg://migrator:pw@h:26257/app?sslmode=require"
            )

    def test_postgres_scheme_migration_url_maps_to_psycopg(self):
        s = Settings(
            DATABASE_URL="postgresql://runtime:pw@h:26257/app",
            STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://migrator:pw@h:26257/app",
        )
        with patch("app.db.settings", s):
            assert _migration_engine_url() == "postgresql+psycopg://migrator:pw@h:26257/app"

    def test_blank_migration_url_falls_back_to_runtime(self):
        s = Settings(DATABASE_URL="postgresql://runtime:pw@h:26257/app", STRIKENOVA_MIGRATION_DATABASE_URL="")
        with patch("app.db.settings", s):
            assert _migration_engine_url() == "postgresql+psycopg://runtime:pw@h:26257/app"


class TestGuardedTargetIdentity:
    """ADR-018 §8.1: one logical database, two distinct PostgreSQL logins.

    These tests are hermetic: they exercise the URL contract only and never
    open a connection, so no database of any kind is required or touched.
    """

    RUNTIME = "postgresql://strikenova_app:runtime-secret@db.example:5432/options"
    MIGRATOR = "postgresql://strikenova_migrator:migrator-secret@db.example:5432/options"

    def _settings(self, **overrides):
        values = {
            "DATABASE_URL": self.RUNTIME,
            "STRIKENOVA_MIGRATION_DATABASE_URL": self.MIGRATOR,
            # ADR-018 §8.1 names both identities: the URLs must connect as
            # exactly these roles, not merely as two different logins.
            "STRIKENOVA_MIGRATOR_ROLE": "strikenova_migrator",
            "STRIKENOVA_RUNTIME_ROLE": "strikenova_app",
        }
        values.update(overrides)
        return Settings(**values)

    def _assert_refused(self, settings_obj, *fragments):
        import pytest

        from app._migration_lease import LeaseError

        with patch("app.db.settings", settings_obj), pytest.raises(LeaseError) as exc:
            assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"
        message = str(exc.value)
        for secret in ("runtime-secret", "migrator-secret", "postgresql://"):
            assert secret not in message
        for fragment in fragments:
            assert fragment in message

    def test_same_database_distinct_roles_is_accepted(self):
        settings_obj = self._settings()
        with patch("app.db.settings", settings_obj):
            resolved = assert_guarded_migration_configuration()
        assert resolved.startswith("postgresql+psycopg://strikenova_migrator:")
        assert resolved.endswith("@db.example:5432/options")

    def test_migration_login_must_be_the_configured_migrator(self):
        """A login that merely differs from the runtime login is not enough."""
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://postgres:migrator-secret@db.example:5432/options"
                )
            ),
            "not the configured migration identity 'strikenova_migrator'",
        )

    def test_runtime_login_must_be_the_configured_runtime_identity(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=(
                    "postgresql://someone_else:runtime-secret@db.example:5432/options"
                )
            ),
            "not the configured runtime identity 'strikenova_app'",
        )

    def test_unnamed_migration_role_is_refused(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATOR_ROLE="   "),
            "STRIKENOVA_MIGRATOR_ROLE is not configured",
        )

    def test_unconfigured_runtime_role_does_not_constrain_the_runtime_login(self):
        settings_obj = self._settings(
            DATABASE_URL=(
                "postgresql://another_runtime:runtime-secret@db.example:5432/options"
            ),
            STRIKENOVA_RUNTIME_ROLE="",
        )
        with patch("app.db.settings", settings_obj):
            assert assert_guarded_migration_configuration()

    def test_same_login_with_a_different_password_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_app:another-secret@db.example:5432/options"
                )
            ),
            "same PostgreSQL login",
        )

    def test_different_database_name_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_migrator:migrator-secret@db.example:5432/other"
                )
            ),
            "different PostgreSQL databases",
        )

    def test_different_endpoint_is_refused_without_a_declared_alias(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "postgresql://strikenova_migrator:migrator-secret@other.example:5432/options"
                )
            ),
            "different PostgreSQL endpoints",
        )

    def test_declared_alias_is_accepted_for_one_logical_endpoint(self):
        settings_obj = self._settings(
            STRIKENOVA_MIGRATION_DATABASE_URL=(
                "postgresql://strikenova_migrator:migrator-secret@direct.example:5432/options"
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=direct.example",
        )
        with patch("app.db.settings", settings_obj):
            assert assert_guarded_migration_configuration()

    def test_alias_does_not_override_a_different_port(self):
        settings_obj = self._settings(
            STRIKENOVA_MIGRATION_DATABASE_URL=(
                "postgresql://strikenova_migrator:migrator-secret@direct.example:6432/options"
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=direct.example",
        )
        self._assert_refused(settings_obj, "different PostgreSQL endpoints")

    def test_neon_pooler_route_needs_an_explicit_declaration(self):
        runtime = "postgresql://strikenova_app:pw@ep-cool-1-pooler.eu.aws.neon.tech/neondb"
        migrator = "postgresql://strikenova_migrator:pw@ep-cool-1.eu.aws.neon.tech/neondb"
        self._assert_refused(
            self._settings(
                DATABASE_URL=runtime, STRIKENOVA_MIGRATION_DATABASE_URL=migrator
            ),
            "different PostgreSQL endpoints",
        )
        declared = self._settings(
            DATABASE_URL=runtime,
            STRIKENOVA_MIGRATION_DATABASE_URL=migrator,
            STRIKENOVA_MIGRATION_TARGET_ALIASES=(
                "ep-cool-1-pooler.eu.aws.neon.tech=ep-cool-1.eu.aws.neon.tech"
            ),
        )
        with patch("app.db.settings", declared):
            assert assert_guarded_migration_configuration()

    def test_postgres_runtime_with_sqlite_migration_target_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL="sqlite:///./paper_journal.db"
            ),
            "sqlite",
        )

    def test_postgres_runtime_with_cockroachdb_migration_target_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=(
                    "cockroachdb+psycopg://strikenova_migrator:pw@db.example:26257/options"
                )
            ),
            "cockroachdb",
        )

    def test_missing_blank_and_malformed_urls_fail_closed(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL=None),
            "required",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL="   "),
            "required",
        )
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL="postgresql://strikenova_migrator:pw@/options"
            ),
            "host",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_DATABASE_URL="this is not a url"),
        )

    def test_postgres_migration_url_without_a_postgres_runtime_is_refused(self):
        self._assert_refused(
            self._settings(DATABASE_URL="sqlite:///./paper_journal.db"),
            "no proven target",
        )

    def test_malformed_alias_configuration_fails_closed(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example"),
            "hostA=hostB",
        )
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=db.example"),
            "distinct",
        )


class TestNeonComputeEndpointIdentity:
    """Neon compute-endpoint identity read from the native hostname.

    Neon documents the native connection-string shape
    ``<endpoint-id>[-pooler].<region>.<cloud>.neon.tech``: the first label is
    the compute endpoint (never a branch or project id), a different compute or
    branch has a different host, and the pooled route differs from the direct
    route for one compute only by the ``-pooler`` suffix. These tests are
    hermetic: every refusal must happen statically, before an engine, a
    connection or the server-side identity proof exists.
    """

    HOST_DIRECT = "ep-cool-darkness-123456.eu-central-1.aws.neon.tech"
    HOST_POOLED = "ep-cool-darkness-123456-pooler.eu-central-1.aws.neon.tech"
    HOST_OTHER = "ep-quiet-mountain-654321.eu-central-1.aws.neon.tech"
    ALIAS = HOST_POOLED + "=" + HOST_DIRECT

    @staticmethod
    def _url(role, host, *, database="neondb", query="", password=None):
        secret = password or (
            "runtime-secret" if role == "strikenova_app" else "migrator-secret"
        )
        return f"postgresql://{role}:{secret}@{host}/{database}{query}"

    def _settings(self, **overrides):
        values = {
            "DATABASE_URL": self._url("strikenova_app", self.HOST_POOLED),
            "STRIKENOVA_MIGRATION_DATABASE_URL": self._url(
                "strikenova_migrator", self.HOST_DIRECT
            ),
            "STRIKENOVA_MIGRATOR_ROLE": "strikenova_migrator",
            "STRIKENOVA_RUNTIME_ROLE": "strikenova_app",
            "STRIKENOVA_MIGRATION_TARGET_ALIASES": self.ALIAS,
        }
        values.update(overrides)
        return Settings(**values)

    @contextlib.contextmanager
    def _spies(self, settings_obj, calls):
        """Arm every engine/connection entry point as a recording spy."""

        def _record(*args, **kwargs):
            calls.append(args)
            raise AssertionError(
                "an engine, connection or server-side proof was attempted before "
                "the static endpoint check finished"
            )

        with patch("app.db.settings", settings_obj), patch(
            "app.db.create_engine", _record
        ), patch("app.db._create_runtime_engine", _record), patch(
            "app.db._verify_logical_target", _record
        ):
            yield

    def _assert_accepted(self, settings_obj):
        calls = []
        with self._spies(settings_obj, calls):
            resolved = assert_guarded_migration_configuration()
        assert resolved.startswith("postgresql+psycopg://strikenova_migrator:")
        assert calls == []
        return resolved

    def _assert_refused(self, settings_obj, *fragments):
        import pytest

        from app._migration_lease import LeaseError

        calls = []
        with self._spies(settings_obj, calls):
            with pytest.raises(LeaseError) as exc:
                assert_guarded_migration_configuration()
        assert exc.value.sqlstate == "45004"
        message = str(exc.value)
        for secret in ("runtime-secret", "migrator-secret", "postgresql://"):
            assert secret not in message
        for fragment in fragments:
            assert fragment in message
        # Refused by the static check: nothing was opened, so no server-side
        # signature could have agreed and authorized the pair.
        assert calls == []

    def test_same_neon_host_and_port_is_accepted(self):
        self._assert_accepted(
            self._settings(
                DATABASE_URL=self._url("strikenova_app", self.HOST_DIRECT),
                STRIKENOVA_MIGRATION_TARGET_ALIASES="",
            )
        )

    def test_neon_direct_and_pooled_routes_with_matching_endpoint_are_accepted(self):
        self._assert_accepted(self._settings())

    def test_different_neon_endpoints_with_an_alias_are_refused(self):
        alias = self.HOST_OTHER + "=" + self.HOST_DIRECT
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url("strikenova_app", self.HOST_OTHER),
                STRIKENOVA_MIGRATION_TARGET_ALIASES=alias,
            ),
            "different Neon compute endpoints",
            "ep-quiet-mountain-654321",
            "ep-cool-darkness-123456",
            "cannot declare two different compute endpoints",
        )

    def test_different_neon_endpoints_are_refused_despite_one_server_signature(self):
        """A branch/copy shares the signature, so equality must not rescue it."""
        alias = self.HOST_OTHER + "=" + self.HOST_DIRECT
        with patch(
            "app.db._logical_target_identity",
            lambda conn: ("neondb", 16384, "7300000000000000000"),
        ):
            self._assert_refused(
                self._settings(
                    DATABASE_URL=self._url("strikenova_app", self.HOST_OTHER),
                    STRIKENOVA_MIGRATION_TARGET_ALIASES=alias,
                ),
                "different Neon compute endpoints",
            )

    def test_neon_direct_and_pooled_routes_without_a_declaration_are_refused(self):
        self._assert_refused(
            self._settings(STRIKENOVA_MIGRATION_TARGET_ALIASES=""),
            "different PostgreSQL endpoints",
        )

    def test_neon_host_mixed_with_a_non_neon_host_is_refused(self):
        self._assert_refused(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator", "db.example"
                ),
                STRIKENOVA_MIGRATION_TARGET_ALIASES=(self.HOST_POOLED + "=db.example"),
            ),
            "mix a native Neon host",
        )

    def test_malformed_neon_hosts_are_refused_not_alias_handled(self):
        """An unreadable Neon compute identity is never handed to the alias policy."""
        for host in (
            "ep-cool-darkness-123456.neon.tech",  # no region/cloud labels
            "epcool-darkness.eu-central-1.aws.neon.tech",  # not an endpoint id
            "ep-cool-darkness-123456.eu-central-1.gcp.neon.tech",  # undocumented cloud
        ):
            self._assert_refused(
                self._settings(
                    DATABASE_URL=self._url("strikenova_app", host),
                    STRIKENOVA_MIGRATION_TARGET_ALIASES=(host + "=" + self.HOST_DIRECT),
                ),
                "does not match Neon's documented",
            )

    def test_neon_host_case_and_trailing_dot_are_normalized(self):
        self._assert_accepted(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app", self.HOST_POOLED.upper() + "."
                ),
            )
        )

    def test_endpoint_option_matching_the_host_is_accepted(self):
        self._assert_accepted(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_POOLED,
                    query="?options=endpoint%3Dep-cool-darkness-123456&sslmode=require",
                ),
            )
        )

    def test_endpoint_option_conflicting_with_the_host_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query="?options=endpoint%3Dep-quiet-mountain-654321",
                ),
            ),
            "must name one compute",
        )

    def test_endpoint_option_on_a_non_neon_host_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    "db.example",
                    database="options",
                    query="?options=endpoint%3Dep-cool-darkness-123456",
                ),
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator", "db.example", database="options"
                ),
                STRIKENOVA_MIGRATION_TARGET_ALIASES="",
            ),
            "no documented meaning",
        )

    def test_malformed_and_ambiguous_endpoint_options_are_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query="?options=endpoint%3Dnot-an-endpoint",
                )
            ),
            "malformed Neon compute endpoint",
        )
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=endpoint%3Dep-cool-darkness-123456%20"
                        "project%3Dep-quiet-mountain-654321"
                    ),
                )
            ),
            "more than one Neon compute endpoint",
        )

    def test_sqlalchemy_represents_a_repeated_options_parameter_as_a_tuple(self):
        """The guard's input shape, observed on the pinned SQLAlchemy."""
        from sqlalchemy.engine import make_url

        parsed = make_url(
            self._url(
                "strikenova_app",
                self.HOST_DIRECT,
                query=(
                    "?options=endpoint%3Dep-cool-darkness-123456"
                    "&options=endpoint%3Dep-quiet-mountain-654321"
                ),
            )
        )
        assert parsed.query["options"] == (
            "endpoint=ep-cool-darkness-123456",
            "endpoint=ep-quiet-mountain-654321",
        )

    def test_psycopg_renders_a_repeated_options_tuple_as_a_python_tuple_string(self):
        """Why a repeated value is refused: the pinned driver does not reject it.

        ``make_conninfo`` accepts the tuple the URL parser produced and embeds
        ``str(tuple)`` into one options string, whose first token is a
        parenthesis rather than a libpq option. The value is therefore not a
        readable options command at all.
        """
        from psycopg.conninfo import make_conninfo

        rendered = make_conninfo(
            "",
            options=(
                "endpoint=ep-cool-darkness-123456",
                "-c statement_timeout=5000",
            ),
        )
        assert rendered.startswith("options='(")

    def test_single_runtime_options_are_still_accepted_on_a_neon_host(self):
        self._assert_accepted(
            self._settings(
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator",
                    self.HOST_DIRECT,
                    query="?options=-c%20statement_timeout%3D5000",
                ),
            )
        )

    def test_repeated_options_with_matching_and_conflicting_endpoints_are_refused(
        self,
    ):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=endpoint%3Dep-cool-darkness-123456"
                        "&options=endpoint%3Dep-quiet-mountain-654321"
                    ),
                )
            ),
            "more than one Neon compute endpoint",
        )

    def test_repeated_options_with_two_endpoint_ids_are_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=endpoint%3Dep-quiet-mountain-654321"
                        "&options=endpoint%3Dep-mute-forest-111111"
                    ),
                )
            ),
            "more than one Neon compute endpoint",
        )

    def test_repeated_identical_options_values_are_refused(self):
        """The chosen policy for a repetition that names one endpoint: refuse.

        No repeated value can reach a server (the driver renders the tuple), so
        accepting it would only defer an unreadable options string to connect
        time; the parameter is refused statically instead.
        """
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=endpoint%3Dep-cool-darkness-123456"
                        "&options=endpoint%3Dep-cool-darkness-123456"
                    ),
                )
            ),
            "repeats the 'options' query parameter",
            "supply a single 'options' value",
        )

    def test_repeated_options_cannot_hide_a_malformed_endpoint_declaration(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=-c%20statement_timeout%3D5000"
                        "&options=endpoint%3Dnot-an-endpoint"
                    ),
                )
            ),
            "malformed Neon compute endpoint",
        )

    def test_repeated_options_cannot_hide_an_endpoint_behind_a_runtime_option(self):
        """A hidden selector is compared with the hostname, not stringified away.

        Under the previous ``str(value)`` reading this URL's endpoint option sat
        behind tuple punctuation and was not read; the conflicting selector must
        now be detected and refused as a mismatch.
        """
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=-c%20statement_timeout%3D5000"
                        "&options=endpoint%3Dep-quiet-mountain-654321"
                    ),
                )
            ),
            "must name one compute",
        )

    def test_repeated_deprecated_project_spelling_is_refused_too(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    query=(
                        "?options=project%3Dep-cool-darkness-123456"
                        "&options=project%3Dep-quiet-mountain-654321"
                    ),
                )
            ),
            "more than one Neon compute endpoint",
        )

    def test_non_string_option_values_are_refused(self):
        """Values the URL parser cannot produce are still refused explicitly."""
        import pytest

        import app.db as db_module
        from app._migration_lease import LeaseError

        for value in (
            ("endpoint=ep-cool-darkness-123456", 7),
            (),
            {"endpoint": "ep-cool-darkness-123456"},
        ):
            with pytest.raises(LeaseError) as exc:
                db_module._declared_compute_endpoint(
                    {"options": value}, label="DATABASE_URL"
                )
            assert exc.value.sqlstate == "45004"
            assert "DATABASE_URL" in str(exc.value)

    def test_generic_postgres_urls_keep_their_options_behavior(self):
        """No repeated-parameter policy is applied outside native Neon hosts."""
        self._assert_accepted(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    "db.example",
                    database="options",
                    query="?options=-c%20statement_timeout%3D5000",
                ),
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator", "db.example", database="options"
                ),
                STRIKENOVA_MIGRATION_TARGET_ALIASES="",
            )
        )
        # A repeated ``options`` parameter carries no Neon endpoint identity on a
        # generic host, so the Neon-only repetition refusal does not apply.
        import app.db as db_module

        target = db_module._parse_migration_target(
            self._url(
                "strikenova_migrator",
                "db.example",
                database="options",
                query="?options=-c%20statement_timeout%3D5000&options=-c%20lock_timeout%3D100",
            ),
            label="STRIKENOVA_MIGRATION_DATABASE_URL",
        )
        assert target.neon is None

    def test_generic_postgres_hosts_keep_the_existing_alias_policy(self):
        generic = self._settings(
            DATABASE_URL=self._url("strikenova_app", "db.example", database="options"),
            STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                "strikenova_migrator", "direct.example", database="options"
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="db.example=direct.example",
        )
        self._assert_accepted(generic)
        # A Neon-shaped label on a non-Neon domain is a generic host: no Neon
        # grammar is assumed for other providers.
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app", "ep-cool-1.db.example", database="options"
                ),
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator", "db.example", database="options"
                ),
                STRIKENOVA_MIGRATION_TARGET_ALIASES="",
            ),
            "different PostgreSQL endpoints",
        )


    # -- Neon's other documented endpoint-routing inputs ---------------------
    #
    # Neon documents three ways to name a compute: the query option, the
    # database field, and the password prefix. With the pinned psycopg 3 the
    # password is passed to Neon's proxy verbatim and the database field is not
    # expanded (libpq's expand_dbname path is not used), but none of them may
    # be allowed to name a compute the hostname does not name.

    def test_password_endpoint_prefix_with_a_semicolon_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    password="endpoint=ep-quiet-mountain-654321;runtime-secret",
                )
            ),
            "endpoint-prefixed password",
            "SCRAM-SHA-256",
        )

    def test_password_endpoint_prefix_with_a_dollar_separator_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_POOLED,
                    password="endpoint=ep-quiet-mountain-654321$runtime-secret",
                )
            ),
            "endpoint-prefixed password",
        )

    def test_password_endpoint_prefix_is_refused_even_when_it_matches_the_host(self):
        """Chosen policy: refuse the form, do not support it with weaker auth.

        Neon documents that the prefix downgrades authentication from
        SCRAM-SHA-256 to password authentication, and psycopg passes the value
        through untouched, so the application cannot see which compute the
        proxy would select. A matching prefix is therefore still refused: the
        supported way to select a compute is the hostname plus, at most, the
        documented query option.
        """
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    password="endpoint=ep-cool-darkness-123456;runtime-secret",
                )
            ),
            "endpoint-prefixed password",
            "use a plain password",
        )

    def test_deprecated_project_password_prefix_is_refused_too(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    password="project=ep-quiet-mountain-654321;runtime-secret",
                )
            ),
            "endpoint-prefixed password",
        )

    def test_query_project_option_follows_the_same_endpoint_policy(self):
        accepted = self._settings(
            DATABASE_URL=self._url(
                "strikenova_app",
                self.HOST_POOLED,
                query="?options=project%3Dep-cool-darkness-123456",
            )
        )
        self._assert_accepted(accepted)
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_POOLED,
                    query="?options=project%3Dep-quiet-mountain-654321",
                )
            ),
            "must name one compute",
        )

    def test_dbname_endpoint_option_differing_from_the_host_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    database="dbname=neondb options=endpoint=ep-quiet-mountain-654321",
                )
            ),
            "must name one compute",
        )

    def test_dbname_endpoint_option_matching_the_host_is_accepted(self):
        self._assert_accepted(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    database="dbname=neondb options=endpoint=ep-cool-darkness-123456",
                ),
                STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                    "strikenova_migrator",
                    self.HOST_DIRECT,
                    database="dbname=neondb options=endpoint=ep-cool-darkness-123456",
                ),
                STRIKENOVA_MIGRATION_TARGET_ALIASES="",
            )
        )

    def test_dbname_connection_parameters_that_can_retarget_are_refused(self):
        for database, fragment in (
            ("dbname=neondb host=ep-quiet-mountain-654321.eu.aws.neon.tech", "host"),
            ("dbname=neondb port=6543", "port"),
            ("dbname=neondb user=someone_else", "user"),
            ("dbname=neondb password=another-secret", "password"),
            ("dbname=neondb service=other", "service"),
        ):
            self._assert_refused(
                self._settings(
                    DATABASE_URL=self._url(
                        "strikenova_app", self.HOST_DIRECT, database=database
                    )
                ),
                "carries connection parameters",
                fragment,
            )

    def test_dbname_connection_string_that_cannot_be_parsed_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    database="weird=name options=endpoint=ep-quiet-mountain-654321",
                )
            ),
            "cannot be parsed",
        )

    def test_ambiguous_dbname_endpoint_declaration_is_refused(self):
        self._assert_refused(
            self._settings(
                DATABASE_URL=self._url(
                    "strikenova_app",
                    self.HOST_DIRECT,
                    database=(
                        "dbname=neondb options='endpoint=ep-cool-darkness-123456 "
                        "project=ep-quiet-mountain-654321'"
                    ),
                )
            ),
            "more than one Neon compute endpoint",
        )

    def test_punctuated_database_names_are_not_parsed_as_connection_strings(self):
        for database in ("app.db-v2_prod", "neondb", "options"):
            self._assert_accepted(
                self._settings(
                    DATABASE_URL=self._url(
                        "strikenova_app", self.HOST_DIRECT, database=database
                    ),
                    STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                        "strikenova_migrator", self.HOST_DIRECT, database=database
                    ),
                    STRIKENOVA_MIGRATION_TARGET_ALIASES="",
                )
            )

    def test_generic_postgres_hosts_keep_the_previous_routing_policy(self):
        """No Neon-only parsing is applied outside native Neon hostnames."""
        generic = dict(
            DATABASE_URL=self._url(
                "strikenova_app",
                "db.example",
                database="dbname=options options=endpoint=ep-quiet-mountain-654321",
                password="endpoint=ep-quiet-mountain-654321;runtime-secret",
            ),
            STRIKENOVA_MIGRATION_DATABASE_URL=self._url(
                "strikenova_migrator",
                "db.example",
                database="dbname=options options=endpoint=ep-quiet-mountain-654321",
            ),
            STRIKENOVA_MIGRATION_TARGET_ALIASES="",
        )
        self._assert_accepted(self._settings(**generic))


class TestSingleIdentityBackwardCompatibility:
    def test_alembic_source_still_present_and_ordered(self):
        import inspect

        import app.db as db_module

        src = inspect.getsource(db_module)
        assert "STRIKENOVA_MIGRATION_DATABASE_URL" in src
        # The migration runner must still call command.upgrade(cfg, "head").
        assert 'command.upgrade(alembic_cfg, "head")' in src
        # Guard still ordered before engine construction.
        guard = src.rindex("validate_production_config()")
        engine_line = src.index("engine = _engine()")
        assert guard < engine_line

    def test_alembic_authority_unchanged(self):
        """init_db still delegates schema management solely to Alembic."""
        import inspect

        import app.db as db_module

        src = inspect.getsource(db_module.init_db)
        assert "_run_alembic_migrations(" in src


class TestSqliteEffectiveTargetIdentity:
    """ADR-017: SQLite identity is what the driver opens, not the URL text.

    Every expectation here is derived from a real connection to the URL under
    test: ``PRAGMA database_list`` reports the file SQLite actually opened, and
    independent engines establish whether an in-memory database is really
    shared. The helper's own normalization is never used as the oracle.

    The pinned driver (SQLAlchemy 2.0.43 / SQLite 3.45.1) reads the query keys
    exactly as written: only ``uri`` reaches ``sqlite3.connect(uri=True)`` and
    SQLite matches ``mode``/``cache`` case-sensitively, so case-variant keys are
    inert. Identity mirrors that, and the migration-target policy refuses a
    private in-memory target rather than treating one URL string as one shared
    database.
    """

    @staticmethod
    def _identity(url):
        from app.db import _sqlite_target_identity

        return _sqlite_target_identity(url)

    @staticmethod
    def _assert_target_refused(url, migration_url=None):
        """Call the policy layer and return the refusal message it raised."""
        from app._migration_lease import LeaseError

        import pytest

        from app.db import _assert_sqlite_migration_target

        with pytest.raises(LeaseError) as exc:
            _assert_sqlite_migration_target(url, migration_url or url)
        assert exc.value.sqlstate == "45004"
        return str(exc.value)

    @staticmethod
    def _assert_target_accepted(url, migration_url=None):
        from app.db import _assert_sqlite_migration_target

        return _assert_sqlite_migration_target(url, migration_url or url)

    @staticmethod
    def _driver_target(url):
        """(kind, realpath) of the database the pinned driver really opens."""
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                path = conn.execute(text("PRAGMA database_list")).one()[2]
        finally:
            engine.dispose()
        if path == "":
            return ("memory", None)
        return ("file", os.path.realpath(path))

    def _assert_identity_matches_driver(self, url):
        identity = self._identity(url)
        kind, path = self._driver_target(url)
        assert identity[1] == kind, f"{url}: identity {identity} is not {kind}"
        if kind == "file":
            assert identity[0] == path, (
                f"{url}: identity names {identity[0]!r} but the driver opened {path!r}"
            )

    def test_identity_matches_the_database_the_driver_opens(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for url in (
            "sqlite:///plain.db",
            "sqlite:///journal%20notes.db",
            "sqlite:///file:journal%20notes.db?uri=true",
            "sqlite:///space notes.db",
            "sqlite:///file:space%20notes.db?uri=true",
            "sqlite:///file:memory-mode.db?mode=memory&uri=true",
            "sqlite:///file:mode-rwc.db?mode=rwc&uri=true",
            "sqlite:///:memory:",
            "sqlite:///",
        ):
            self._assert_identity_matches_driver(url)

    def test_percent_encoded_ordinary_name_differs_from_uri_decoded_name(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        literal = self._identity("sqlite:///journal%20notes.db")
        decoded = self._identity("sqlite:///file:journal%20notes.db?uri=true")
        # Both identities agree with what the driver really opens ...
        self._assert_identity_matches_driver("sqlite:///journal%20notes.db")
        self._assert_identity_matches_driver(
            "sqlite:///file:journal%20notes.db?uri=true"
        )
        # ... and those are two different files.
        assert "%20" in literal[0] and " " not in literal[0]
        assert " " in decoded[0] and "%20" not in decoded[0]
        assert literal != decoded

    def test_ordinary_path_with_spaces_equals_the_equivalent_uri(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        plain = self._identity("sqlite:///space notes.db")
        uri = self._identity("sqlite:///file:space%20notes.db?uri=true")
        assert plain == uri
        assert self._driver_target("sqlite:///space notes.db") == self._driver_target(
            "sqlite:///file:space%20notes.db?uri=true"
        )

    def test_mode_and_cache_are_inert_outside_uri_mode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # The driver ignores mode/cache without a URI filename: it opens a file.
        assert self._driver_target("sqlite:///ignored.db?mode=memory")[0] == "file"
        assert self._identity("sqlite:///ignored.db?mode=memory") == self._identity(
            "sqlite:///ignored.db"
        )
        assert self._identity(
            "sqlite:///ignored.db?cache=shared"
        ) == self._identity("sqlite:///ignored.db")
        # With file: + uri=true the same options are effective.
        assert (
            self._driver_target("sqlite:///file:x.db?mode=memory&uri=true")[0]
            == "memory"
        )
        assert self._identity("sqlite:///file:x.db?mode=memory&uri=true")[1] == "memory"

    def test_the_uri_key_is_case_sensitive(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # SQLAlchemy coerces only the literal key ``uri``; ``URI`` is not it, so
        # the driver opens a *file* literally named ``file:upper-uri.db`` (the
        # query string is dropped because URI processing stayed off).
        upper = "sqlite:///file:upper-uri.db?URI=true"
        assert self._driver_target(upper) == (
            "file",
            os.path.realpath("file:upper-uri.db"),
        )
        # Identity must not claim the URI-decoded ``upper-uri.db`` instead.
        message = self._assert_target_refused(upper)
        assert "requires the exact query key" in message
        assert self._identity("sqlite:///file:lower-uri.db?uri=true") == (
            os.path.realpath("lower-uri.db"),
            "file",
            None,
        )
        self._assert_identity_matches_driver("sqlite:///file:lower-uri.db?uri=true")
        # Outside a file: name the same key is simply an ordinary file URL:
        # the driver ignores the whole query, exactly as identity reports.
        self._assert_identity_matches_driver("sqlite:///plain-uri.db?URI=true")

    def test_case_variant_mode_is_inert_and_refused(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        # SQLite matches query parameter names case-sensitively, so ``MODE`` is
        # passed through and ignored: the driver opens the named file.
        url = "sqlite:///file:MODE-case.db?MODE=memory&uri=true"
        assert self._driver_target(url) == (
            "file",
            os.path.realpath("MODE-case.db"),
        )
        identity = self._identity(url)
        assert identity == (os.path.realpath("MODE-case.db"), "file", None)
        assert identity == self._identity("sqlite:///file:MODE-case.db?uri=true")
        # A URL that reads as a memory request the driver will not honour is
        # refused rather than silently resolved to the file the driver opens.
        message = self._assert_target_refused(url)
        assert "in-memory database the driver would not open" in message
        # Without uri=true the whole query is dropped by the driver.
        plain = "sqlite:///MODE-case2.db?MODE=memory"
        assert self._driver_target(plain) == (
            "file",
            os.path.realpath("MODE-case2.db"),
        )
        self._assert_identity_matches_driver(plain)
        self._assert_target_refused(plain)

    def test_case_variant_cache_does_not_share_a_memory_database(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        from sqlalchemy import create_engine, text

        url = "sqlite:///file:cache-case?mode=memory&CACHE=shared&uri=true"
        # ``CACHE`` is not the key SQLite reads, so the database is private:
        # two independent physical connections are two different databases.
        assert self._driver_target(url)[0] == "memory"
        assert self._identity(url) == ("cache-case", "memory", None)
        writer = create_engine(url)
        try:
            with writer.connect() as conn:
                conn.execute(text("CREATE TABLE case_probe (x integer)"))
                conn.commit()
            reader = create_engine(url)
            try:
                with reader.connect() as conn:
                    seen = conn.execute(
                        text(
                            "SELECT count(*) FROM sqlite_master "
                            "WHERE type='table' AND name='case_probe'"
                        )
                    ).scalar()
            finally:
                reader.dispose()
        finally:
            writer.dispose()
        assert not seen, "an upper-case CACHE=shared must not share a memory database"
        message = self._assert_target_refused(url)
        assert "private in-memory SQLite target is not supported" in message

    def test_mixed_case_duplicate_parameters_follow_the_driver(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        # The exact key decides; the upper-case spelling is inert, in either
        # order, because SQLite reads only ``mode``.
        memory_first = "sqlite:///file:mix-a.db?mode=memory&MODE=rwc&uri=true"
        assert self._driver_target(memory_first)[0] == "memory"
        assert self._identity(memory_first) == ("mix-a.db", "memory", None)
        file_first = "sqlite:///file:mix-b.db?mode=rwc&MODE=memory&uri=true"
        assert self._driver_target(file_first) == (
            "file",
            os.path.realpath("mix-b.db"),
        )
        assert self._identity(file_first) == (
            os.path.realpath("mix-b.db"),
            "file",
            None,
        )
        # The memory-looking spelling still fails closed instead of being
        # silently normalised into the exact key.
        self._assert_target_refused(file_first)

    def test_named_private_memory_is_not_a_shared_target(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        from sqlalchemy import create_engine, text

        url = "sqlite:///file:private-room-regression?mode=memory&cache=private&uri=true"
        # Two independent physical connections opened with the *identical* URL
        # are two different databases: the URL string proves nothing.
        writer = create_engine(url)
        try:
            with writer.connect() as conn:
                conn.execute(text("CREATE TABLE private_probe (x integer)"))
                conn.commit()
            reader = create_engine(url)
            try:
                with reader.connect() as conn:
                    seen = conn.execute(
                        text(
                            "SELECT count(*) FROM sqlite_master "
                            "WHERE type='table' AND name='private_probe'"
                        )
                    ).scalar()
            finally:
                reader.dispose()
        finally:
            writer.dispose()
        assert not seen, "a private in-memory database is per physical connection"

        # Identity still reports the declared target ...
        assert self._identity(url) == (
            "private-room-regression",
            "memory",
            "private",
        )
        # ... but the policy refuses it, so identical strings are never accepted
        # as one shared migration target. This is the negative control: an
        # implementation that accepted private memory would pass no assertion
        # here.
        message = self._assert_target_refused(url)
        assert "private in-memory SQLite target is not supported" in message
        # The bare :memory: target has the same defect and is refused too.
        assert self._identity("sqlite:///:memory:") == (":memory:", "memory", None)
        self._assert_target_refused("sqlite:///:memory:")
        # Only the shared-cache form is genuinely shared, and it stays accepted.
        shared = "sqlite:///file:private-room-regression?mode=memory&cache=shared&uri=true"
        assert self._identity(shared) == (
            "private-room-regression",
            "memory",
            "shared",
        )
        assert self._assert_target_accepted(shared) == shared

    def test_shared_in_memory_is_shared_and_private_is_not(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from sqlalchemy import create_engine, text

        def sees_table(url, table):
            engine = create_engine(url)
            try:
                with engine.connect() as conn:
                    found = conn.execute(
                        text(
                            "SELECT count(*) FROM sqlite_master "
                            "WHERE type='table' AND name=:t"
                        ),
                        {"t": table},
                    ).scalar()
                return bool(found)
            finally:
                engine.dispose()

        shared = "sqlite:///file:shared-room?mode=memory&cache=shared&uri=true"
        private = "sqlite:///file:private-room?mode=memory&cache=private&uri=true"
        # Real behaviour: a shared-cache in-memory database is the same database
        # across connections, a private one is not.
        writer = create_engine(shared)
        try:
            with writer.connect() as conn:
                conn.execute(text("CREATE TABLE shared_probe (x integer)"))
                conn.commit()
                assert sees_table(shared, "shared_probe")
        finally:
            writer.dispose()
        writer = create_engine(private)
        try:
            with writer.connect() as conn:
                conn.execute(text("CREATE TABLE private_probe (x integer)"))
                conn.commit()
                assert not sees_table(private, "private_probe")
        finally:
            writer.dispose()

        shared_identity = self._identity(shared)
        private_identity = self._identity(private)
        assert shared_identity[1] == "memory" and private_identity[1] == "memory"
        assert shared_identity != private_identity
        assert self._identity(shared) == shared_identity

    def test_repeated_or_malformed_query_parameters_fail_closed(self):
        from app._migration_lease import LeaseError

        import pytest

        for url in (
            "sqlite:///file:x.db?mode=memory&mode=rwc&uri=true",
            "sqlite:///file:x.db?cache=shared&cache=private&uri=true",
            "sqlite:///file:x.db?uri=true&uri=true",
            "sqlite:///x.db?uri=maybe",
            "sqlite:///file:x.db?mode=sideways&uri=true",
            "sqlite:///file:x.db?cache=other&uri=true",
            # ?uri=true without a file: name makes SQLite skip URI processing.
            "sqlite:///x.db?uri=true",
            # A file: name without ?uri=true is a literal filename.
            "sqlite:///file:x.db",
        ):
            with pytest.raises(LeaseError):
                self._identity(url)

    def test_default_and_relative_paths_follow_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        assert self._identity("sqlite:///./relative.db") == (
            os.path.realpath(tmp_path / "relative.db"),
            "file",
            None,
        )
        absolute = "sqlite:////tmp/absolute-identity.db"
        assert self._identity(absolute) == (
            os.path.realpath("/tmp/absolute-identity.db"),
            "file",
            None,
        )
        # The default URL and the bare sqlite:/// form name one database.
        from app.db import _DEFAULT_DB_PATH

        assert self._identity(None) == (os.path.realpath(_DEFAULT_DB_PATH), "file", None)

    def test_private_memory_runtime_is_refused_with_a_sqlite_migration_url(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        private = "sqlite:///file:runtime-private?mode=memory&uri=true"
        assert self._identity(private) == ("runtime-private", "memory", None)
        message = self._assert_target_refused(private, private)
        assert "private in-memory SQLite target is not supported" in message

    def test_private_memory_runtime_is_refused_with_a_non_sqlite_migration_url(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        private = "sqlite:///file:runtime-private?mode=memory&uri=true"
        # A non-PostgreSQL, non-SQLite dialect never needs a real connection:
        # the runtime target is refused before the migration URL is examined.
        migration = "mysql+pymysql://migrator:pw@db.example:3306/options"
        message = self._assert_target_refused(private, migration)
        assert "private in-memory SQLite target is not supported" in message

    def test_bare_memory_runtime_is_refused_with_a_non_sqlite_migration_url(self):
        runtime = "sqlite:///:memory:"
        assert self._identity(runtime) == (":memory:", "memory", None)
        migration = "mysql+pymysql://migrator:pw@db.example:3306/options"
        message = self._assert_target_refused(runtime, migration)
        assert "private in-memory SQLite target is not supported" in message

    def test_file_backed_runtime_policy_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        file_url = "sqlite:///runtime-file.db"
        assert self._identity(file_url) == (
            os.path.realpath("runtime-file.db"),
            "file",
            None,
        )
        assert self._assert_target_accepted(file_url) == file_url
        # A non-SQLite migration URL is still returned unchanged for a file target.
        migration = "mysql+pymysql://migrator:pw@db.example:3306/options"
        assert self._assert_target_accepted(file_url, migration) == migration
        # A different SQLite file is still refused: the single-identity policy
        # that predates the runtime-target fix is untouched.
        message = self._assert_target_refused(file_url, "sqlite:///other.db")
        assert "separate SQLite migration URL is not supported" in message

    def test_shared_cache_memory_runtime_remains_accepted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        shared = "sqlite:///file:runtime-shared?mode=memory&cache=shared&uri=true"
        assert self._identity(shared) == ("runtime-shared", "memory", "shared")
        assert self._assert_target_accepted(shared) == shared
        # A shared-cache memory target is genuinely shared, so the non-SQLite
        # migration URL early return is still allowed.
        migration = "mysql+pymysql://migrator:pw@db.example:3306/options"
        assert self._assert_target_accepted(shared, migration) == migration
