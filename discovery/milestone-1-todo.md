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
- [ ] **Legitimate revisions (estimated, then actual).** Retail data from the NEM12 format carries a quality flag on every value:
  A (actual), E (estimated), S (substituted), F (final substitute).
  - Today a different value for a stored interval is a conflict and opens a `duplicate_reading` discrepancy,
    so every normal estimate → actual update would raise one.
  - Add the quality flag to readings, plus a ranking rule: an actual replaces an estimate, an estimate never replaces an actual,
    and two values of the same quality still conflict.
  - Keep earlier versions (RawReading already holds the history) instead of overwriting.

## Can slip to a later milestone
- [ ] A concurrency race test: a `TransactionTestCase` with two threads on the same interval (it feeds the Milestone 4 postmortem)
- [ ] Spike detection against each meter's history
- [ ] Decide whether billing periods may overlap (an exclusion constraint needs `btree_gist`)
- [ ] Confirm the discrepancy types and billing period statuses
- [ ] **Separate import and export channels.** NEM12 sends consumption (E1) and export (B1) as two positive data streams,
  not one signed value.
  - Add a channel (direction) to `ReadingSubmission` in place of splitting a signed value.
  - When the second channel arrives for an interval that already has a reading, fill in its empty column rather than
    counting it as a duplicate. `ON CONFLICT DO NOTHING` becomes a conditional update.
- [ ] **A NEM12 adapter** next to `ausgrid_csv.py`. Its times are fixed AEST market time with no daylight saving,
  which is simpler than Ausgrid's.
- [ ] **Retail identifiers:** a NMI with its checksum, the meter serial, and the NMI suffix for each channel, instead of free text.
- [ ] **Basic (accumulation) meters** report a running total read every quarter. They're out of scope: state it as a limitation
  rather than building it.

## Notes
- Zone substations stand in for customer meters, because individual customer data is private. They're assigned to a
  single placeholder customer, so charges calculated from them won't be realistic. (Recorded in the README and `domain-model.md`.)
- An optional `Meter.kind` with values `revenue` and `network` would keep substations out of billing. Consider it alongside the Tariff model.
