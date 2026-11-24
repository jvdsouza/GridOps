# Project Context
Last updated: 2026-11-23

## Main goal
GridOps: an internal tool for an energy retailer (portfolio project aimed at Kraken). It ingests meter readings, detects missing, duplicate and suspicious readings, calculates charges, and resolves billing discrepancies without losing history. It's built in four milestones (see `discovery/milestones.md`): Django/Postgres domain and API → React + GraphQL operator UI → Celery for bill calculation, retries and idempotency → observability and an incident write-up. `discovery/key-actions.md` lists what the project should demonstrate: domain reasoning, a reproduced concurrency or retry bug with a postmortem, and documented trade-offs.

## Current goal
Milestone 1: finish the validation layer for reading ingestion. Done means real CSV data can be imported, gaps are detected and recorded as discrepancies, and re-running an import doesn't create duplicate discrepancies.

## Current state
- **Infrastructure:** `docker-compose.yml` runs `postgres:18-alpine` and `redis:8.8-alpine`, published on 127.0.0.1 only. Credentials are in `.env` (git-ignored); `.env.example` is the template. Docker Desktop must be running first.
- **Django 6.1 project:** in `gridops/`.
  - Settings read `.env` through python-dotenv. Hosts default to `127.0.0.1`, because `localhost` hangs on IPv6 on this Windows machine.
  - Celery app is in `gridops/celery.py`; run the worker with `--pool=solo` on Windows.
  - `/health/` checks Postgres, Redis and the Celery broker.
- **`metering` app:**
  - Models: Customer, Meter (`identifier` unique, `interval_minutes`, `records_export`), MeterReading (`usage` and `export`, both ≥ 0, unique per meter and `interval_start`), RawReading, BillingPeriod, Discrepancy (optional `raw_reading`).
  - `domain-model.md` matches the models.
- **Migrations:** `0001` and `0002` are committed and applied to the dev DB (2026-11-24). The user runs `migrate` themselves.
- **Validation layer:** `metering/ingestion/`.
  - `validators.py`: pure checks on one reading (numeric, unit kWh/MW, timezone-aware, aligned to the interval, interval finished, sign versus `records_export`).
  - `service.py`: `ingest_readings()` processes chunks of 5,000, each in its own transaction. It inserts with `INSERT … ON CONFLICT DO NOTHING` via `unnest`. Outcomes: accepted, duplicate, conflict (duplicate with a different value, which adds a discrepancy), rejected (adds a `suspicious_reading` discrepancy), unknown meter (counted only).
- **CSV import:** built.
  - `metering/ingestion/sources/ausgrid_csv.py` is a pure adapter: `check_header`, `read_substations`, `period_end_utc` (DST round trip) and `iter_submissions`.
  - Commands: `seed_substations` and `import_readings` (two passes, `--dry-run`).
  - On the real files the adapter submits 208,230 values and skips 1,218 blanks and 24 DST labels, which reconciles with all 209,472 values.
  - Run end to end on 2026-11-24. The dev DB now holds 208,230 MeterReadings and 416,460 RawReadings (two runs), with 0 discrepancies; export is on Croudace Bay only (3,750 readings). About 4 s per 35k-value file; the second run gave all duplicates. In a `!` command use bash syntax (`data/*.csv`), not PowerShell.
- **Tests:** `uv run python manage.py test metering` gives 48 tests, all passing.
- **Uncommitted work (2026-11-24):** the CSV import, its tests, the reworked `discovery/csv-import.svg` and `.md`, and README updates.

## Data facts (`data/*.csv`, Ausgrid zone substation FY26)
- Source: Ausgrid's "Distribution Zone Substation Data 2026" zip (May 2025 to April 2026). Ausgrid keeps the copyright, so `data/` is git-ignored. The README explains how to download it.
- Six substations with 15-minute MW values; the unit is always MW. 209,472 values, 1,218 blank, one written as `7.07E-4`, up to 6 decimal places.
- Darlinghurst has 8 days missing (2026-03-13 to 2026-03-20), which makes a good test case for gap detection.
- Only Croudace Bay has negative values: 3,750, all between 08:00 and 17:59, peaking around midday and seasonal. That's very likely solar export, but nothing in the data confirms it.
- Timestamps are Sydney wall-clock time (AEST/AEDT; the load profiles don't shift around the changeovers), yet every day has exactly 96 values. On 2025-10-05, labels `02:00`–`02:45` don't exist (most files repeat one value across `02:00`–`03:00`). On 2026-04-05, the repeated hour is sent only once. See `discovery/csv-import.md`. Each label is the end of its period (`00:15` to `24:00`). The `h:00` columns sit after `h:45` in the file, but the labels are correct (checked statistically), so map columns by header name.
- Ausgrid publishes the data unvalidated: switching spikes and dips, some MW values estimated from current.

## Previous state
- 2026-11-23: the session began with only discovery docs and an empty `pyproject.toml` (Django 6.1.3, Celery 5.7.0).

## Decisions
- 2026-11-23 — Use postgres:18 and redis:8.8 — why: newest versions supported by Django 6.1 (PG 15+, 18 was out when 6.1 shipped) and redis-py 8.1 (Redis 7.2–8.8) — rejected: PG 19.0 (weeks old), Redis 8.10 (not listed by redis-py).
- 2026-11-23 — Django runs on the host; only Postgres and Redis are in compose — why: simplest for development — rejected: containerising web and worker for now (offered, not done).
- 2026-11-23 — Store every submission as a RawReading first — why: the requirement not to lose track of what happened; supports audits and corrections — rejected: putting the rejected value on Discrepancy (gives a thinner record).
- 2026-11-23 — Storage at scale: about 320 bytes per reading across the raw and accepted tables (measured) — why it's acceptable: tiny at current scale — deferred: monthly partitions, retention or archiving, an IngestionBatch table. BRIN index on `received_at`; turned off Django's redundant foreign-key indexes on meter.
- 2026-11-23 — Validation lives in a service layer (`metering/ingestion/`), not in `Model.clean` or serializers — why: `bulk_create` skips `clean()`, and checks need batch and database context — rejected: doing it in the model or API.
- 2026-11-23 — Identical resubmission is a duplicate with no discrepancy; a different value is a duplicate plus a `duplicate_reading` discrepancy, and the stored value is never overwritten — why: retries shouldn't create noise, and history mustn't change silently.
- 2026-11-23 — Export is a separate non-negative `MeterReading.export` column, allowed only when `Meter.records_export` is set — why: keeps the "usage ≥ 0" database check and supports separate feed-in pricing — rejected: allowing negative usage (loses the database backstop), and guessing export from each value.
- 2026-11-23 — `Meter.identifier` is a generic unique text field — why: the CSVs identify substations by name, not serial — later: NMI and meter serial for real retail meters.
- 2026-11-23 — Interval alignment is measured from UTC midnight — why: simple, and correct for Sydney offsets.
- 2026-11-24 — The CSV import skips blank values and counts them; gap detection records them as `missing_reading` — why: one discrepancy of the right type instead of `suspicious_reading` plus a missing one — rejected: submitting blanks as rejected readings.
- 2026-11-24 — An unknown substation stops the import before anything is stored; meters come from a one-off `seed_substations` — why: a typo fails loudly — rejected: a `--create-meters` flag, and a nullable `RawReading.meter` with `meter_ref`.
- 2026-11-24 — On the DST-start day the CSV import skips labels whose local time doesn't exist; on the DST-end day it uses `fold=0` for ambiguous labels — why: `zoneinfo` would silently shift a time that doesn't exist onto a real instant and clash with it.

## Next steps
The full checklist is in `discovery/milestone-1-todo.md`: import run, gap detection, **Tariff model (not started)**, **API (none yet, framework not chosen)**, corrections write-up.
1. Commit the CSV import work. (Done 2026-11-24: the import was run for real, counts checked, re-run gave all duplicates, timings recorded.)
2. Add a time range (`period_start`/`period_end`) to Discrepancy. Then:
   - build a Celery task for missing intervals using `generate_series`, recording one discrepancy per gap and resolving it when late readings arrive;
   - skip creating a discrepancy when an open one already exists, so re-running an import doesn't add more. This has to cover `duplicate_reading` too: a conflicting value raises one again on every run.
3. Write a real concurrency test (`TransactionTestCase` with two threads racing on the same interval). This feeds `key-actions.md` #2.
4. Spike detection against each meter's history, as a Celery task.
5. (Done: a full 35,000-row file imports in about 4 s.)
6. Later: a design for discrepancy resolution and corrections (`resolved_at`, notes, replacing a reading while keeping the original); the Milestone 1 API.

## Open questions / blockers
- Croudace Bay negatives: confirm with Ausgrid (sharinginformation@ausgrid.com.au) or record as an assumption. The plan is to set `records_export=True` for Croudace Bay only.
- Not answered yet: should BillingPeriod overlaps be blocked with an exclusion constraint (needs `btree_gist`)?
- Discrepancy types (`missing_reading`, `duplicate_reading`, `suspicious_reading`) and BillingPeriod statuses (`open`, `closed`) were chosen by Claude; the user hasn't confirmed them.
