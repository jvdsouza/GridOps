# Milestone 1: what's left

Milestone 1 (from [milestones.md](milestones.md)): *Model customers, meters, readings, tariffs and billing periods.
Implement a Django/PostgreSQL API that ingests readings and enforces basic constraints.*

**Done:** the domain model with database constraints, the validation and ingestion service, the CSV import
(`seed_substations`, `import_readings`), 48 tests and the README.

## 1. Run the import for real and commit
- [x] `migrate` the dev database (0001, 0002)
- [x] `seed_substations`
- [x] `import_readings --dry-run data/*.csv`; the counts matched [csv-import.md](csv-import.md)
- [x] `import_readings data/*.csv`: every value accepted, 3.7–4.0 s per file
- [x] Check the row counts in Postgres: 208,230 MeterReadings, 416,460 RawReadings, 0 Discrepancies
- [x] Run the import again: every value came back as a duplicate, with no new discrepancies
- [x] Record the timings in `csv-import.md` and the README
- [ ] Commit the CSV import work

## 2. Gap detection (the first Celery task)
- [ ] Add a time range (`period_start`, `period_end`) to Discrepancy
- [ ] A Celery task that finds missing intervals per meter with `generate_series` and opens one `missing_reading` per gap
- [ ] Don't open a discrepancy when an open one already covers it; this applies to `duplicate_reading` too
- [ ] Resolve a gap when a late reading fills it
- [ ] `import_readings` queues gap detection for each meter it touched
- [ ] Tests: Darlinghurst's 8 missing days, blanks, the 4 gaps on the DST-end day, and running it twice

## 3. Tariff model
- [ ] A Tariff with a rate per kWh, a feed-in rate for export, and perhaps time-of-use bands
- [ ] Assign it to a customer or meter for an effective date range (start inclusive, end exclusive)
- [ ] Database constraints: rates ≥ 0, valid ranges, ideally no overlapping assignments
- [ ] Update `domain-model.md` and the diagram. Bill calculation stays in Milestone 3.

## 4. API
- [ ] Choose a framework: Django REST Framework, django-ninja, or plain Django views
- [ ] `POST` readings: goes through `ingest_readings()`, returns the outcome counts, safe to retry
- [ ] `GET` readings for a meter and date range
- [ ] `GET` discrepancies, filtered by meter, type and status
- [ ] Tests, plus API examples in the README

## 5. How corrections work (key-actions #1)
- [ ] Write up how a reading, a usage calculation, an issued bill and an adjustment differ
- [ ] Explain how a correction replaces a reading while keeping the original
- [ ] Discrepancy resolution fields (`resolved_at`, notes)

## Can slip to a later milestone
- [ ] A concurrency race test: a `TransactionTestCase` with two threads on the same interval (it feeds the Milestone 4 postmortem)
- [ ] Spike detection against each meter's history
- [ ] Decide whether billing periods may overlap (an exclusion constraint needs `btree_gist`)
- [ ] Confirm the discrepancy types and billing period statuses
