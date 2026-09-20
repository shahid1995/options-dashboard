# StrikeNova Overnight Gap Intelligence — Phase 3: Non-Expiry Historical Snapshot Expansion

**Issue:** #78 — Research Phase 3 (data availability + historical-snapshot expansion only)
**Phase 1 record:** `GAP_INTELLIGENCE_PHASE1.md` · **Phase 2 record:** `GAP_INTELLIGENCE_PHASE2.md`
**Scope guard:** research/backtest only — nothing here feeds the dashboard, paper
execution, broker execution, auth, or any user-facing forecast.

---

## 1. Source audit (mandatory first step)

Authorized local candidates measured (row counts, not schema presence):

| Source | Location | Content measured |
|---|---|---|
| Greeks backup | `paper_journal_backup_greeks_20260826_013136.db` (project backend dir) | `nifty_candles` 57,675 (IST, 2024-10-01→2026-08-24); `option_candles` 514,610 (IST, 96 **expiry-day** dates 2024-10-03→2026-08-18, 4,019 instruments, OI NOT NULL); `contract_specs` 20,584 |
| Daily-backfill backup | `paper_journal_backup_20260824_232636.db` (same dir) | `option_candles` 538,955 (**UTC**, 27 dates 2024-10-01→2024-11-07, 285–362 instruments/day — full daily chains incl. **21 non-expiry sessions**); `nifty_candles` 18,875 (IST, 2026-01-02→2026-08-13 only); `contract_specs` 20,584 |
| Canonical greeks engine | `app/services/historical_greeks.py` | reused (Phase 2); not modified |

Futures, India VIX, bid/ask: **0 rows in every authorized source** — genuinely
unavailable, reported as `unavailable`, never zero-filled.

## 2. Timestamp-semantics finding (drives everything below)

The two backups store `open_time` in **different conventions**:

* daily-backup option candles are **UTC** (03:45→09:57 UTC = 09:15→15:27 IST),
  including the 2024-11-01 Muhurat evening session (10:00→13:29 UTC);
* greeks-backup candles (and the daily-backup's own 2026 `nifty_candles`)
  are **IST wall-clock**.

The earlier "morning-cutoff" hypothesis from the first audit was wrong: the
09:57 terminal candle **is** 15:27 IST — a full end-of-session snapshot.
Both regular sessions and Muhurat are usable as end-of-session snapshots.

## 3. Merged working store (`merge-stores`)

`build_merged_store()` unions the two backups into a NEW research working
copy (sources strictly read-only). Two defects found and fixed during
verification:

1. **Id-collision data loss** — the first implementation copied
   `SELECT *`, so source-2 rows whose surrogate `id` collided with
   source-1 rows were silently dropped (measured: 2024-10-03 lost 362→53
   instruments; exactly the id-tail 538,955−514,610 = 24,345 rows
   survived). Fixed by copying explicit columns **excluding `id`**, so
   dedup happens on the natural unique keys (first source wins).
2. **Mixed timestamp conventions** — fixed with **per-date, evidence-based
   normalization**: for each (table, date) both hypotheses (no shift,
   +330 min UTC→IST) are tested against the authoritative IST session
   window for that date taken from the accumulated index candles; the
   hypothesis placing strictly more candle opens inside the window wins.
   A date with no index anchor or no separating evidence is **REFUSED**
   (rows not copied, refusal recorded in `_store_provenance`) — never
   guessed. Real run: 27 option dates shifted, 151 kept, **0 refused**.

Result: `nifty_candles` 58,550 · `option_candles` **1,044,815** ·
`contract_specs` 20,584 · **117 option dates** (96 + 27 − 6 overlap).
2024-10-04 carries its full 285 instruments with all four weekly expiries
(front expiry 2024-10-10) — the pre-fix DTE>7 artifact (surviving rows
happened to hold only the monthly contract) is gone.

Provenance: `_store_provenance` records per source the absolute path,
SHA-256, priority, row counts, and every per-date tz decision.

## 4. Coverage matrix (measured, Phase 3 expanded dataset)

| Data family | Non-expiry sessions | Earliest | Latest | Option rows | IV/Greeks | Bid/Ask | Futures | VIX |
|---|---:|---|---|---:|---|---|---|---|
| Greeks backup (IST) | 0 | 2024-10-03 | 2026-08-18 | 514,610 | reconstructed (engine) | 0% | 0% | 0% |
| Daily backfill (UTC) | 21 (incl. Muhurat) | 2024-10-01 | 2024-11-07 | 538,955 | reconstructed (engine) | 0% | 0% | 0% |
| **Merged (research)** | **20 eligible** | 2024-10-03 | 2026-08-18 | 1,044,815 | reconstructed (engine) | 0% | 0% | 0% |

### Session composition (extracted research sample)

* total sessions **114** (was 93 in Phase 2); expiry **94**; non-expiry **20**
  (17.5%); non-expiry span 2024-10-04→2024-11-06 (includes 2024-11-01 Muhurat,
  cutoff = Muhurat window end; 21 candidates → 20 after the continuity/target
  rules, the sample remains honestly eligibility-filtered).

### DTE distribution (measured, matches the source audit)

| Bucket | Sessions | % |
|---|---:|---:|
| DTE 0 | 94 | 82.5% |
| DTE 1–2 | 10 | 8.8% |
| DTE 3–7 | 10 | 8.8% |
| DTE >7 | 0 | 0% |

Genuine DTE diversity now exists (DTE 1–7), but expiry-day dominance
remains the binding constraint — reported as-is, not forced.

## 5. Snapshot reconstruction & provenance

* Cutoff = session window end from index candles (latest index candle of the
  date); chain rows admitted only with `open_time ≤ cutoff` (tested).
* Front expiry = min expiry ≥ session date among that day's instruments
  (`contract_specs`); no fallback ambiguity (tested).
* IV/Greeks computed **only at cutoff candles** via the canonical
  `HistoricalGreeksEngine` (IST→UTC −5:30 per its contract) — unchanged
  from Phase 2.
* **Value provenance (new, hard contract):** `gap_option_chain_snapshots.
  value_provenance` (JSON, migration `f4a9b8c2d1e7`) records per value:
  `observed` (LTP, volume, OI — genuinely in the source), `derived`
  (change-in-OI), `reconstructed` (IV + Black-Scholes Greeks — **never
  reported as observed**), `unavailable` (bid/ask). Legacy Phase 1/2 rows
  keep NULL (documented as pre-Phase-3). Missing ≠ zero preserved
  (NO_IV 1,490 rows and 120 expired-at-cutoff rows stay NULL and counted).
* Data note: deep-OTM rows with stored OI = 0 (e.g. 12,078/34,540 on
  2024-10-04) are source observations (column NOT NULL upstream), not
  missingness — reported, not "fixed".

## 6. Completeness (Phase 3 sample, measured)

chain/spot/OI/volume 100% · IV rows 71.8% · Delta/Gamma/Vega/Theta rows
73.9% · bid/ask 0% · futures 0% · India VIX 0% · profile **CORE+GREEKS**
(FULL remains unreachable — futures/VIX/bid-ask do not exist historically).

## 7. Determinism

Two full runs from the same immutable source produced byte-identical
summaries (canonical SHA-256 `2b34a4385c412c7e…`); session set, provenance,
DTE distribution, predictions, targets, and metrics all equal. Artifacts:
`tmp-issue78/sample_P3_A2.json` / `sample_P3_B2.json`.

## 8. Model results (Phase 3 expanded sample — no ranking)

| Model | scored/n | NO_EDGE | acc | bal-acc | precision | recall | F1 | AUC | Brier | MAE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 112/114 | 2 | 0.330 | 0.314 | 0.220 | 0.314 | 0.257 | 0.398 | 0.248 | — |
| POS-style | 114/114 | 0 | 0.360 | 0.338 | 0.255 | 0.338 | 0.290 | 0.573 | 0.250 | — |
| SOS | 22/114 | 92 | 0.545 | 0.443 | 0.417 | 0.443 | 0.396 | 0.589 | 0.237 | 69.7 pts |

Target distribution: FLAT 33 / GAP_UP 42 / GAP_DOWN 39. Majority base rates
are printed alongside each model's metrics (none beaten out-of-sample).
SOS NO_EDGE rose (Phase 2: 74/93 → 92/114): denser chains widen component
dispersion, so confluence abstains more — honest behavior, not a defect.

### Chronological 70/30 out-of-sample discipline (descriptive)

| Model | train n / AUC | test n / AUC |
|---|---|---|
| baseline | 78 / 0.486 | 34 / **0.383** |
| POS-style | 79 / 0.628 | 35 / **0.510** |
| SOS | 79 / 0.639 | 35 / **0.514** |

In-sample AUC ≈ 0.63 collapses to chance out-of-sample for both POS-style
and SOS. **No predictive edge demonstrated.**

## 9. Phase 1 vs Phase 2 vs Phase 3 (samples never pooled)

| | Phase 1 (candle-only) | Phase 2 (expiry enriched) | Phase 3 (non-expiry expanded) |
|---|---|---|---|
| Sessions | 93 | 93 | 114 |
| Non-expiry | 0 | 0 | 20 |
| IV rows | 0% | 70.6% | 71.8% |
| Greeks rows | 0% | 73.6% | 73.9% |
| SOS NO_EDGE | 51/93 | 74/93 | 92/114 |
| POS acc (base rate) | 0.419 (0.352) | 0.419 (0.352) | 0.360 (0.368) |
| OOS test AUC (POS) | — | 0.495 | 0.510 |

Attribution guard: metric movement here is **sample composition**, not model
quality — different sessions, denser chains (300+ instruments vs ~40 in
Oct–Nov 2024 daily backfill vs Phase 2's expiry captures), different base
rates. Models are byte-identical; only the dataset changed. The Phase 2
control run on the greeks-only store reproduced Phase 2 exactly, proving
sample identity.

## 10. Tests (Issue #78 additions)

`pytest tests/test_gap_research.py -q` → **59 passed** (exit 0), including
the 13 Phase-3-focused behaviors: post-cutoff rejection, cutoff boundary,
expiry selection, non-expiry identification (from contract metadata, never
from data absence), DTE bucket assignment, intraday-cutoff detection,
value provenance (observed/reconstructed/unavailable + legacy NULL),
missing ≠ zero, deterministic reconstruction, causal target ordering,
no-look-ahead, and the three new merge tests: id-collision preservation,
UTC→IST normalization with cross-source natural-key dedup, and refusal of
unanchorable dates without guessing. Full backend suite: **5,949 passed,
6 failed** — the 6 are the pre-existing failures verified on pristine base
`65022ea` during Phase 1 (live_verification, phase721, strategy_resolver ×2,
naive-now in `trade_lifecycle/`, upstox adapter); zero new failures.

## 11. Limitations

* 82.5% of sessions remain DTE 0 — expansion capacity of the authorized
  history is exhausted; deeper DTE diversity requires new (authorized)
  capture, which does not exist locally.
* IV/Greeks remain engine-reconstructed, never observed (no IV surface in
  any authorized source).
* The 2024-11-01 Muhurat session is a special abbreviated session; its
  realized gap spans a holiday weekend (kept, flagged, not hidden).
* Futures/VIX/bid-ask unavailability caps SOS's component set regardless
  of expansion.

## 12. Phase 4 recommendation

Do **not** iterate further on models with this dataset — the out-of-sample
evidence is negative and the authorized history is now fully exploited
(117 option dates, 114 eligible sessions, DTE>7 unreachable). The only
material next step is **new data**: enable the existing authorized
end-of-session chain capture (with observed IV and, if the broker provides
it, bid/ask) to accumulate going-forward snapshots with genuine DTE
diversity, then re-run this same deterministic pipeline on that
prospective sample after it has accumulated enough sessions. Until then
the Overnight Gap Intelligence remains research-only with no demonstrated
edge — correctly producing no user-facing output.
