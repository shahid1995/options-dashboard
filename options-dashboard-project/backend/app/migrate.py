"""Guarded migration operator CLI (ADR-017 §9.2 and §13.3).

Supported commands::

    python -m app.migrate upgrade head
    python -m app.migrate bootstrap-lease
    python -m app.migrate bootstrap-lease --close-barrier
    python -m app.migrate bootstrap-lease --upgrade-protocol N
    python -m app.migrate status

``upgrade`` is the only supported PostgreSQL migration path: it acquires the
lease, runs Alembic inside the protected transaction and releases. A bare
``alembic upgrade head`` against PostgreSQL fails closed in ``alembic/env.py``
because there is no lease admission and no way to prove ownership.

``bootstrap-lease`` installs the protocol objects on a genuinely empty
database or verifies an existing installation. Verification never changes the
installed protocol version; only ``--upgrade-protocol`` may, and only for a
declared authorized source. ``status`` is read-only.

No command here ever prints a raw owner token or a connection string.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _bootstrap_engine():
    """Engine for the migration identity (never the runtime identity)."""
    from app.db import _migration_engine

    return _migration_engine()


def cmd_upgrade(args: argparse.Namespace) -> int:
    if args.revision != "head":
        print(
            f"unsupported revision {args.revision!r}: only 'head' is supported "
            "by the guarded migration path",
            file=sys.stderr,
        )
        return 2
    from app.db import _run_alembic_migrations

    _run_alembic_migrations()
    print("migration upgrade head: OK (lease acquired, entered, committed, released)")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from app._migration_lease import bootstrap_lease, read_protocol_info

    engine = _bootstrap_engine()
    try:
        before = _safe_info(read_protocol_info, engine)
        if args.close_barrier:
            report = bootstrap_lease(engine, mode="close_barrier")
        elif args.upgrade_protocol is not None:
            report = bootstrap_lease(
                engine, mode="upgrade", upgrade_to=args.upgrade_protocol
            )
        else:
            report = bootstrap_lease(engine, mode="verify")
        after = _safe_info(read_protocol_info, engine)
    finally:
        engine.dispose()

    print("state before: " + json.dumps(before, sort_keys=True, default=str))
    print("bootstrap-lease: " + json.dumps(report, sort_keys=True, default=str))
    print("state after:  " + json.dumps(after, sort_keys=True, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from app._migration_lease import read_protocol_info

    from sqlalchemy import text

    engine = _bootstrap_engine()
    try:
        info = read_protocol_info(engine)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT owner_fingerprint, owner_role, acquired_at, expires_at, "
                    "released_at, generation, protocol_version "
                    "FROM migration_internal.migration_lease "
                    "WHERE lease_name = 'migration-lease'"
                )
            ).first()
            conn.commit()
    finally:
        engine.dispose()
    report = {
        "installed_version": info.installed_version,
        "min_supported_version": info.min_supported_version,
        "max_supported_version": info.max_supported_version,
        "barrier_state": info.barrier_state,
        "bootstrap_state": info.bootstrap_state,
        "lease_row": dict(row._mapping) if row is not None else None,
    }
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


def _safe_info(reader, engine) -> dict | str:
    try:
        info = reader(engine)
    except Exception as exc:  # noqa: BLE001 - reporting only
        return f"unavailable ({type(exc).__name__})"
    return {
        "installed_version": info.installed_version,
        "min_supported_version": info.min_supported_version,
        "max_supported_version": info.max_supported_version,
        "barrier_state": info.barrier_state,
        "bootstrap_state": info.bootstrap_state,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.migrate",
        description="Guarded StrikeNova migration operator commands (ADR-017/018).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    upgrade = sub.add_parser("upgrade", help="run Alembic inside the ADR-017 lease")
    upgrade.add_argument("revision", help="only 'head' is supported")
    upgrade.set_defaults(func=cmd_upgrade)

    bootstrap = sub.add_parser(
        "bootstrap-lease",
        help="install or verify the migration lease, close the barrier, or upgrade the protocol",
    )
    mode = bootstrap.add_mutually_exclusive_group()
    mode.add_argument(
        "--close-barrier",
        action="store_true",
        help="close the deployment barrier without waiting on a live owner",
    )
    mode.add_argument(
        "--upgrade-protocol",
        type=int,
        metavar="N",
        help="perform the one authorized protocol transition to N",
    )
    mode.add_argument(
        "--verify",
        action="store_true",
        help="verify an existing installation (default; never changes a version column)",
    )
    bootstrap.set_defaults(func=cmd_bootstrap)

    status = sub.add_parser("status", help="read-only protocol and lease state")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    from app._migration_lease import LeaseError

    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except LeaseError as exc:
        state = f" (SQLSTATE {exc.sqlstate})" if exc.sqlstate else ""
        print(f"{args.command} refused{state}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            f"{args.command} interrupted; the open transaction was rolled back "
            "and no committed state changed",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
