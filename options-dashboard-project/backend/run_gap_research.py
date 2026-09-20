#!/usr/bin/env python
"""Issue #17 — Overnight Gap Intelligence research CLI (Phase 1).

Research-only entry point following the repository's run_*.py CLI pattern.
No web server; no broker calls; no production impact.

Usage::

    python run_gap_research.py ingest    --session 2026-09-18 --cutoff "2026-09-18T15:30:00" --underlying u.json --chain c.json
    python run_gap_research.py features  --session 2026-09-18
    python run_gap_research.py predict   --session 2026-09-18
    python run_gap_research.py target    --session 2026-09-18 --next-session 2026-09-22 --next-open 25120.5 --next-open-ts "2026-09-22T09:15:00"
    python run_gap_research.py backtest  --model sos
    python run_gap_research.py status
    python run_gap_research.py historical-sample --store-url sqlite:///store.db
    python run_gap_research.py capture --user-id <id> --connection-id <id> \
        --session 2026-09-18 --cutoff "2026-09-18T15:30:00" --prior-close 25000.0

``ingest`` expects a JSON underlying snapshot (spot/futures/VIX keys) and a
JSON option-chain array (strike/option_type/expiry/quote+greeks keys). Data
must come from the project's authorized free/broker data paths — no paid
vendor and no restricted-exchange scraping is implemented or permitted here.

``capture`` (Issue #80, research-only) fetches ONE front-expiry end-of-session
chain snapshot through the customer's OWN authorized broker session (existing
gateway + adapter path; same token-resolution rules as GEX capture) and
persists it into the Phase-1 research schema with observed-value provenance.
It is operator-invoked; nothing here schedules, signals, or serves users.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import engine, SessionLocal  # noqa: E402
from app.models import GapPredictionSession  # noqa: E402
from app.research.gap_pipeline import (  # noqa: E402
    BASELINE,
    POS_STYLE,
    SOS,
    SessionExistsError,
    attach_realized_target,
    build_and_store_features,
    generate_and_store_predictions,
    ingest_session_snapshots,
    run_comparison_backtest,
    store_backtest_result,
)


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_ingest(args) -> int:
    db = SessionLocal()
    try:
        session = ingest_session_snapshots(
            db,
            session_date=args.session,
            cutoff_timestamp=datetime.fromisoformat(args.cutoff),
            prior_close=float(args.prior_close) if args.prior_close else None,
            underlying=_load_json(args.underlying),
            chain=_load_json(args.chain),
            replace=args.replace,
        )
        print(f"ingested session {session.session_date} (id={session.id})")
        return 0
    except SessionExistsError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


def _cmd_features(args) -> int:
    db = SessionLocal()
    try:
        features = build_and_store_features(db, args.session)
        if features is None:
            print(f"no snapshots for {args.session}", file=sys.stderr)
            return 1
        print(f"features stored for {args.session}: {len(features)} features")
        return 0
    finally:
        db.close()


def _cmd_predict(args) -> int:
    db = SessionLocal()
    try:
        out = generate_and_store_predictions(db, args.session)
        if out is None:
            print(f"no features for {args.session}", file=sys.stderr)
            return 1
        for model, payload in out.items():
            print(
                f"{model}: state={payload.get('state')} "
                f"direction={payload.get('direction_score')}"
            )
        return 0
    finally:
        db.close()


def _cmd_target(args) -> int:
    db = SessionLocal()
    try:
        ts = datetime.fromisoformat(args.next_open_ts) if args.next_open_ts else None
        session = attach_realized_target(
            db, args.session, args.next_session, float(args.next_open), ts
        )
        if session is None:
            print(f"unknown session {args.session}", file=sys.stderr)
            return 1
        print(
            f"target attached: gap_points={session.gap_points} "
            f"gap_pct={session.gap_pct} class={session.gap_class}"
        )
        return 0
    finally:
        db.close()


def _cmd_backtest(args) -> int:
    db = SessionLocal()
    try:
        result = run_comparison_backtest(db, args.model)
        if not result.get("n_observations"):
            print(f"no evaluated sessions for model={args.model}; nothing stored")
            return 1
        metrics = result["metrics"]
        print(json.dumps(metrics, indent=2, default=str))
        if result["by_regime"]:
            print("by regime:")
            print(json.dumps(result["by_regime"], indent=2, default=str))
        # Persist the ACTUAL evaluated date range — never a placeholder.
        store_backtest_result(
            db,
            args.model,
            result["period_start"],
            result["period_end"],
            result["metrics"],
        )
        print(
            f"backtest result stored for model={args.model} "
            f"period={result['period_start']}..{result['period_end']}"
        )
        return 0
    finally:
        db.close()


def _cmd_status(_args) -> int:
    db = SessionLocal()
    try:
        sessions = db.query(GapPredictionSession).order_by(GapPredictionSession.session_date).all()
        print(f"research sessions: {len(sessions)}")
        for s in sessions:
            print(
                f"  {s.session_date} completeness={s.completeness} "
                f"target_class={s.gap_class}"
            )
        return 0
    finally:
        db.close()


def _cmd_historical_sample(args) -> int:
    """Run the full Phase-1 sample from the authorized local candle store.

    The store is an INPUT database: this command never creates or modifies
    tables in it — it only validates the required source tables exist.
    """
    from app.research.gap_historical import REQUIRED_SOURCE_TABLES, run_historical_sample
    from sqlalchemy import create_engine, inspect

    store_url = args.store_url or os.environ.get("DATABASE_URL")
    if not store_url:
        print("error: --store-url or DATABASE_URL required (candle store DB)")
        return 2
    store_engine = create_engine(store_url)
    existing = set(inspect(store_engine).get_table_names())
    missing = [t for t in REQUIRED_SOURCE_TABLES if t not in existing]
    if missing:
        print(
            "error: candle store is missing required source tables: "
            f"{', '.join(missing)}. The source DB is read-only for this "
            "command — no tables were created."
        )
        store_engine.dispose()
        return 2
    store = sessionmaker(bind=store_engine)()
    db = SessionLocal()
    try:
        summary = run_historical_sample(
            db,
            store,
            start=args.start,
            end=args.end,
            flat_band_pct=args.flat_band_pct,
            enrich_greeks=args.enrich_greeks,
        )
    finally:
        db.close()
        store.close()
        store_engine.dispose()
    print(json.dumps(summary, indent=2, default=str))
    return 0


def _cmd_capture(args) -> int:
    """Issue #80 — capture ONE prospective end-of-session research snapshot.

    Research-only. Uses the customer's own authorized broker session via the
    existing Day-11 gateway (UPSTOX analytics/OAuth token priority identical
    to the GEX capture loop) and persists through the Phase-1 research
    pipeline with observed-value provenance. Operator-invoked: no scheduling,
    no UI, no signal, no production impact.
    """
    import asyncio
    from datetime import date as _date
    from app.research.gap_capture import (
        capture_session,
        observed_iv_coverage,
        resolve_capture_token,
    )

    cutoff = datetime.fromisoformat(args.cutoff)
    session_date = args.session
    # session date must be a real calendar date (classification depends on it)
    _date.fromisoformat(session_date)

    db = SessionLocal()
    try:
        from datetime import timezone as _tz

        from app.brokers.domain.enums import BROKER_ID_UPSTOX
        from app.brokers.gateway import gateway

        # Token resolution: same ownership rules as the GEX capture loop
        # (analytics token on the explicitly-default connection, OAuth
        # fallback) - implemented in the research module so this CLI never
        # imports the FastAPI app.
        token, connection_id, token_source = resolve_capture_token(
            args.user_id, args.connection_id
        )

        adapter = gateway.create(BROKER_ID_UPSTOX, access_token=token)

        async def _fetch() -> tuple:
            contracts = await adapter.get_option_contracts("NIFTY")
            expiries = contracts.get("expiries", [])
            if not expiries:
                raise RuntimeError("broker returned no option expiries")
            expiry_date = expiries[0]  # front expiry from broker metadata
            observation = await adapter.get_option_chain("NIFTY", expiry_date)
            return expiry_date, observation

        expiry_date, observation = asyncio.run(_fetch())

        if observation.underlying_spot_price is None:
            print("error: broker chain observation carries no underlying spot")
            return 2

        # prior OI map for the derived change_in_oi (from the previously
        # captured research session, when one exists for the same expiry).
        from app.models import OptionChainSnapshot

        prev = (
            db.query(OptionChainSnapshot)
            .filter(
                OptionChainSnapshot.symbol == "NIFTY",
                OptionChainSnapshot.expiry == expiry_date,
                OptionChainSnapshot.session_date < session_date,
            )
            .order_by(OptionChainSnapshot.session_date.desc())
            .first()
        )
        prior_oi = {}
        if prev is not None:
            for row in db.query(OptionChainSnapshot).filter(
                OptionChainSnapshot.symbol == "NIFTY",
                OptionChainSnapshot.session_date == prev.session_date,
                OptionChainSnapshot.expiry == expiry_date,
            ):
                if row.open_interest is not None:
                    prior_oi[(row.expiry, float(row.strike), row.option_type)] = float(
                        row.open_interest
                    )

        summary = capture_session(
            db,
            observation,
            session_date=session_date,
            cutoff_timestamp=cutoff,
            prior_close=args.prior_close,
            prior_oi=prior_oi,
            replace=args.replace,
        )
        summary["observed_iv_coverage"] = observed_iv_coverage(db, session_date)
        summary["token_source"] = token_source
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except SessionExistsError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        db.close()


def _cmd_merge_stores(args) -> int:
    """Build a Phase-3 working candle store from authorized local backups."""
    from app.research.gap_historical import build_merged_store

    if os.path.exists(args.output):
        print(f"error: output {args.output} already exists (refusing to overwrite)")
        return 2
    summary = build_merged_store(args.output, args.source)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_ing = sub.add_parser("ingest", help="persist immutable session snapshots")
    p_ing.add_argument("--session", required=True)
    p_ing.add_argument("--cutoff", required=True)
    p_ing.add_argument("--underlying", required=True, help="JSON file path")
    p_ing.add_argument("--chain", required=True, help="JSON file path")
    p_ing.add_argument("--prior-close", default=None)
    p_ing.add_argument("--replace", action="store_true")
    p_ing.set_defaults(func=_cmd_ingest)

    p_feat = sub.add_parser("features", help="build + store research features")
    p_feat.add_argument("--session", required=True)
    p_feat.set_defaults(func=_cmd_features)

    p_pred = sub.add_parser("predict", help="compute + store model predictions")
    p_pred.add_argument("--session", required=True)
    p_pred.set_defaults(func=_cmd_predict)

    p_tgt = sub.add_parser("target", help="attach realized next-open target")
    p_tgt.add_argument("--session", required=True)
    p_tgt.add_argument("--next-session", required=True)
    p_tgt.add_argument("--next-open", required=True, type=float)
    p_tgt.add_argument("--next-open-ts", default=None)
    p_tgt.set_defaults(func=_cmd_target)

    p_bt = sub.add_parser("backtest", help="run + store the comparison backtest")
    p_bt.add_argument("--model", default=SOS, choices=[BASELINE, POS_STYLE, SOS])
    p_bt.set_defaults(func=_cmd_backtest)

    p_st = sub.add_parser("status", help="list research sessions")
    p_st.set_defaults(func=_cmd_status)

    p_hs = sub.add_parser(
        "historical-sample",
        help="Phase-1 sample run from the authorized local candle store",
    )
    p_hs.add_argument(
        "--store-url",
        default=None,
        help="SQLAlchemy URL of the candle-store DB (defaults to DATABASE_URL)",
    )
    p_hs.add_argument("--start", default=None, help="session date >= start")
    p_hs.add_argument("--end", default=None, help="session date <= end")
    p_hs.add_argument(
        "--flat-band-pct",
        type=float,
        default=0.001,
        help="flat-gap band (fraction of prior close)",
    )
    p_hs.add_argument(
        "--enrich-greeks",
        action="store_true",
        help=(
            "Phase 2: attach IV/Greeks via the canonical HistoricalGreeksEngine "
            "(cutoff candle only; IST→UTC per engine contract)"
        ),
    )
    p_hs.set_defaults(func=_cmd_historical_sample)

    p_cap = sub.add_parser(
        "capture",
        help="Issue #80 (research-only): capture one end-of-session chain snapshot"
    )
    p_cap.add_argument("--user-id", required=True, help="StrikeNova user owning the broker session")
    p_cap.add_argument(
        "--connection-id",
        default=None,
        help="explicit BrokerConnection id (default: the user's default UPSTOX connection)",
    )
    p_cap.add_argument("--session", required=True, help="trading date YYYY-MM-DD")
    p_cap.add_argument(
        "--cutoff", required=True, help="research cutoff ISO datetime (end of session)"
    )
    p_cap.add_argument(
        "--prior-close",
        type=float,
        default=None,
        help="session close (reference for the next-session gap target)",
    )
    p_cap.add_argument(
        "--replace",
        action="store_true",
        help="replace an already-captured session (default: refuse, immutable)",
    )
    p_cap.set_defaults(func=_cmd_capture)

    p_ms = sub.add_parser(
        "merge-stores",
        help="union authorized candle-store backups into a Phase-3 working store",
    )
    p_ms.add_argument("--output", required=True, help="new working-store DB path")
    p_ms.add_argument(
        "--source",
        action="append",
        required=True,
        help="source backup DB (repeatable; first source wins on conflicts)",
    )
    p_ms.set_defaults(func=_cmd_merge_stores)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
