import csv
import io
import tempfile
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from .ingestion import ReadingSubmission, ingest_readings, validate_reading
from .ingestion.sources.ausgrid_csv import (
    AusgridFormatError,
    ParseStats,
    check_header,
    iter_submissions,
    period_end_utc,
    read_substations,
)
from .models import Customer, Discrepancy, Meter, MeterReading, RawReading

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
START = datetime(2025, 12, 31, 0, 0, tzinfo=UTC)


def sub(usage="1.5", unit="kWh", start=START, meter_id=1):
    return ReadingSubmission(meter_id=meter_id, interval_start=start, usage=usage, usage_unit=unit)


class ValidateReadingTests(SimpleTestCase):
    def check(self, submission, interval_minutes=30):
        return validate_reading(submission, interval_minutes=interval_minutes, now=NOW)

    def test_valid_kwh_reading(self):
        outcome = self.check(sub("1.5"))
        self.assertTrue(outcome.is_valid)
        self.assertEqual(outcome.usage_kwh, Decimal("1.5000"))

    def test_mw_converted_to_kwh_for_interval_length(self):
        # 2 MW averaged over 15 minutes = 2000 kW x 0.25 h = 500 kWh
        self.assertEqual(self.check(sub("2", "MW"), interval_minutes=15).usage_kwh, Decimal("500.0000"))
        self.assertEqual(self.check(sub("2", "MW"), interval_minutes=30).usage_kwh, Decimal("1000.0000"))

    def test_scientific_notation_and_zero_are_valid(self):
        self.assertEqual(self.check(sub("7.07E-4")).usage_kwh, Decimal("0.0007"))
        self.assertTrue(self.check(sub("0")).is_valid)

    def test_raw_usage_keeps_source_precision(self):
        self.assertEqual(self.check(sub("7.912261", "MW"), 15).raw_usage, Decimal("7.912261"))

    def test_negative_usage_rejected(self):
        outcome = self.check(sub("-0.13225", "MW"), 15)
        self.assertFalse(outcome.is_valid)
        self.assertIn("does not record export", outcome.errors[0])
        self.assertEqual(outcome.raw_usage, Decimal("-0.13225"))  # still kept for the raw record
        self.assertIsNone(outcome.usage_kwh)

    def test_negative_usage_is_export_on_export_meter(self):
        outcome = validate_reading(sub("-0.13225", "MW"), interval_minutes=15, now=NOW, records_export=True)
        self.assertTrue(outcome.is_valid)
        # 0.13225 MW x 1000 x 0.25 h = 33.0625 kWh exported
        self.assertEqual((outcome.usage_kwh, outcome.export_kwh), (Decimal("0"), Decimal("33.0625")))

    def test_positive_usage_on_export_meter_is_import(self):
        outcome = validate_reading(sub("2", "MW"), interval_minutes=15, now=NOW, records_export=True)
        self.assertEqual((outcome.usage_kwh, outcome.export_kwh), (Decimal("500"), Decimal("0")))

    def test_blank_or_non_numeric_usage_rejected(self):
        for value in ["", "  ", None, "abc", "NaN", "Infinity"]:
            with self.subTest(value=value):
                outcome = self.check(sub(value))
                self.assertFalse(outcome.is_valid)
                self.assertIsNone(outcome.raw_usage)

    def test_unsupported_unit_rejected(self):
        self.assertIn("unsupported unit", self.check(sub(unit="MWh")).errors[0])

    def test_naive_timestamp_rejected(self):
        self.assertIn("no timezone", self.check(sub(start=START.replace(tzinfo=None))).errors[0])

    def test_misaligned_interval_rejected(self):
        self.assertFalse(self.check(sub(start=START + timedelta(minutes=10))).is_valid)
        self.assertFalse(self.check(sub(start=START + timedelta(seconds=1))).is_valid)
        self.assertTrue(self.check(sub(start=START + timedelta(minutes=15)), interval_minutes=15).is_valid)

    def test_alignment_is_independent_of_submitted_timezone(self):
        sydney = timezone(timedelta(hours=11))
        self.assertTrue(self.check(sub(start=datetime(2025, 12, 31, 11, 30, tzinfo=sydney))).is_valid)

    def test_unfinished_interval_rejected(self):
        self.assertFalse(self.check(sub(start=NOW - timedelta(minutes=15))).is_valid)
        self.assertTrue(self.check(sub(start=NOW - timedelta(minutes=30))).is_valid)

    def test_all_errors_reported(self):
        outcome = self.check(sub("-1", "kWh", start=START + timedelta(minutes=7)))
        self.assertEqual(len(outcome.errors), 2)


class IngestReadingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        customer = Customer.objects.create(name="Test customer")
        cls.meter = Meter.objects.create(identifier="TEST-30", customer=customer, interval_minutes=30)
        cls.export_meter = Meter.objects.create(
            identifier="TEST-EXPORT-15", customer=customer, interval_minutes=15, records_export=True
        )

    def ingest(self, *submissions, **kwargs):
        return ingest_readings(submissions, source="test", now=NOW, **kwargs)

    def s(self, usage="1.5", **kwargs):
        return sub(usage, meter_id=self.meter.pk, **kwargs)

    def test_valid_reading_accepted(self):
        result = self.ingest(self.s("1.5"))
        self.assertEqual(result.accepted, 1)
        reading = MeterReading.objects.get()
        self.assertEqual((reading.usage, reading.usage_unit), (Decimal("1.5"), "kWh"))
        raw = RawReading.objects.get()
        self.assertEqual((raw.status, raw.meter_reading_id, raw.source), ("accepted", reading.pk, "test"))
        self.assertFalse(Discrepancy.objects.exists())

    def test_rejected_reading_kept_as_raw_with_discrepancy(self):
        result = self.ingest(self.s("-2"))
        self.assertEqual(result.rejected, 1)
        self.assertFalse(MeterReading.objects.exists())
        raw = RawReading.objects.get()
        self.assertEqual((raw.status, raw.usage), ("rejected", Decimal("-2")))
        self.assertIn("negative", raw.rejection_reason)
        discrepancy = Discrepancy.objects.get()
        self.assertEqual((discrepancy.type, discrepancy.status, discrepancy.raw_reading_id), ("suspicious_reading", "open", raw.pk))

    def test_blank_usage_stored_as_null(self):
        self.ingest(self.s(""))
        self.assertIsNone(RawReading.objects.get().usage)

    def test_identical_resubmission_is_duplicate_without_discrepancy(self):
        self.ingest(self.s("1.5"))
        result = self.ingest(self.s("1.50"))
        self.assertEqual((result.accepted, result.duplicates, result.conflicts), (0, 1, 0))
        reading = MeterReading.objects.get()
        duplicate = RawReading.objects.get(status="duplicate")
        self.assertEqual(duplicate.meter_reading_id, reading.pk)
        self.assertFalse(Discrepancy.objects.exists())

    def test_conflicting_resubmission_keeps_original_and_flags_it(self):
        self.ingest(self.s("1.5"))
        result = self.ingest(self.s("9.9"))
        self.assertEqual((result.duplicates, result.conflicts), (1, 1))
        self.assertEqual(MeterReading.objects.get().usage, Decimal("1.5"))  # not overwritten
        duplicate = RawReading.objects.get(status="duplicate")
        self.assertIn("conflicts with stored reading", duplicate.rejection_reason)
        discrepancy = Discrepancy.objects.get()
        self.assertEqual((discrepancy.type, discrepancy.raw_reading_id), ("duplicate_reading", duplicate.pk))

    def test_duplicates_within_one_batch(self):
        result = self.ingest(self.s("1.5"), self.s("1.5"), self.s("2.0"))
        self.assertEqual((result.accepted, result.duplicates, result.conflicts), (1, 2, 1))
        self.assertEqual(MeterReading.objects.count(), 1)
        self.assertEqual(RawReading.objects.count(), 3)

    def test_rerunning_an_import_does_not_double_count(self):
        batch = [self.s("1.0", start=START + timedelta(minutes=30 * i)) for i in range(10)]
        self.ingest(*batch)
        result = self.ingest(*batch)
        self.assertEqual((result.accepted, result.duplicates), (0, 10))
        self.assertEqual(MeterReading.objects.count(), 10)

    def test_out_of_order_readings_accepted(self):
        later, earlier = START + timedelta(hours=1), START
        result = self.ingest(self.s(start=later), self.s(start=earlier))
        self.assertEqual(result.accepted, 2)

    def test_same_instant_in_different_timezone_is_duplicate(self):
        self.ingest(self.s("1.5"))
        sydney = START.astimezone(timezone(timedelta(hours=11)))
        self.assertEqual(self.ingest(self.s("1.5", start=sydney)).duplicates, 1)

    def test_unknown_meter_counted_and_not_stored(self):
        result = self.ingest(sub(meter_id=999_999))
        self.assertEqual(result.unknown_meter, 1)
        self.assertFalse(RawReading.objects.exists())

    def test_chunks_are_processed_independently(self):
        batch = [self.s(start=START + timedelta(minutes=30 * i)) for i in range(7)]
        result = self.ingest(*batch, chunk_size=3)
        self.assertEqual((result.accepted, result.total), (7, 7))

    def test_mw_reading_stored_in_kwh_with_raw_unit_kept(self):
        meter_15 = Meter.objects.create(identifier="TEST-15", customer=self.meter.customer, interval_minutes=15)
        self.ingest(sub("7.912261", "MW", meter_id=meter_15.pk))
        self.assertEqual(MeterReading.objects.get().usage, Decimal("1978.0653"))
        raw = RawReading.objects.get()
        self.assertEqual((raw.usage, raw.usage_unit), (Decimal("7.912261"), "MW"))

    def test_negative_reading_on_export_meter_stored_as_export(self):
        result = ingest_readings([sub("-0.5", "MW", meter_id=self.export_meter.pk)], source="test", now=NOW)
        self.assertEqual(result.accepted, 1)
        reading = MeterReading.objects.get()
        self.assertEqual((reading.usage, reading.export), (Decimal("0"), Decimal("125")))
        self.assertEqual(RawReading.objects.get().usage, Decimal("-0.5"))  # raw keeps the sign
        self.assertFalse(Discrepancy.objects.exists())

    def test_export_conflict_detected(self):
        ingest_readings([sub("-0.5", "MW", meter_id=self.export_meter.pk)], source="test", now=NOW)
        result = ingest_readings([sub("0.5", "MW", meter_id=self.export_meter.pk)], source="test", now=NOW)
        self.assertEqual(result.conflicts, 1)
        self.assertIn("125.0000 kWh export", RawReading.objects.get(status="duplicate").rejection_reason)


# --- Ausgrid CSV import ---------------------------------------------------------------

# Column order as Ausgrid writes it: each h:00 comes after h:45.
AUSGRID_LABEL_ORDER = (
    ["00:15", "00:30", "00:45"]
    + [f"{h:02d}:{m}" for h in range(1, 24) for m in ("15", "30", "45", "00")]
    + ["24:00"]
)
AUSGRID_HEADER = ["year", "Zone Substation", "Date", "Unit", *AUSGRID_LABEL_ORDER]
SUBSTATION = "Test Sub 11kV"


def ausgrid_csv(days: dict[str, dict[str, str] | str], substation=SUBSTATION) -> str:
    """
    Build an Ausgrid-format CSV. `days` maps a date to {label: value} (other labels blank),
    or to a single value used for every label.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(AUSGRID_HEADER)
    for day, values in days.items():
        if isinstance(values, str):
            values = dict.fromkeys(AUSGRID_LABEL_ORDER, values)
        writer.writerow(["2026", substation, day, "MW", *(values.get(label, "") for label in AUSGRID_LABEL_ORDER)])
    return out.getvalue()


def parse(text: str, meter_id=1):
    stats = ParseStats()
    submissions = list(iter_submissions(csv.DictReader(io.StringIO(text)), {SUBSTATION: meter_id}, stats))
    return submissions, stats


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class AusgridAdapterTests(SimpleTestCase):
    def test_test_header_matches_real_file_layout(self):
        self.assertEqual(len(AUSGRID_LABEL_ORDER), 96)
        self.assertEqual(AUSGRID_LABEL_ORDER[3:7], ["01:15", "01:30", "01:45", "01:00"])
        check_header(AUSGRID_HEADER)

    def test_columns_mapped_by_name_not_position(self):
        # The 01:00 column sits after 01:45 but is the period 00:45-01:00 (AEST, UTC+10).
        (submission,), _ = parse(ausgrid_csv({"2025-06-01": {"01:00": "5.5"}}))
        self.assertEqual(submission.interval_start, utc(2025, 5, 31, 14, 45))
        self.assertEqual((submission.usage, submission.usage_unit, submission.meter_id), ("5.5", "MW", 1))

    def test_label_24_00_is_next_days_midnight(self):
        (submission,), _ = parse(ausgrid_csv({"2025-06-01": {"24:00": "1"}}))
        self.assertEqual(submission.interval_start, utc(2025, 6, 1, 13, 45))  # 23:45-24:00 AEST

    def test_normal_day_gives_96_contiguous_intervals(self):
        submissions, stats = parse(ausgrid_csv({"2025-06-01": "1"}))
        expected = [utc(2025, 5, 31, 14) + timedelta(minutes=15 * i) for i in range(96)]
        self.assertEqual(sorted(s.interval_start for s in submissions), expected)
        self.assertEqual((stats.rows, stats.submitted, stats.skipped_blank, stats.skipped_dst), (1, 96, 0, 0))

    def test_values_passed_through_as_text(self):
        (submission,), _ = parse(ausgrid_csv({"2025-06-01": {"00:15": " 7.07E-4 "}}))
        self.assertEqual(submission.usage, "7.07E-4")

    def test_blank_values_skipped_and_counted(self):
        submissions, stats = parse(ausgrid_csv({"2025-06-01": {"00:15": "1", "00:30": "2"}}))
        self.assertEqual(len(submissions), 2)
        self.assertEqual(stats.skipped_blank, 94)

    def test_dst_start_skips_the_hour_that_does_not_exist(self):
        # 2025-10-05: clocks go from 02:00 AEST to 03:00 AEDT, a 23-hour day.
        submissions, stats = parse(ausgrid_csv({"2025-10-05": "1"}))
        self.assertEqual((stats.submitted, stats.skipped_dst), (92, 4))
        expected = [utc(2025, 10, 4, 14) + timedelta(minutes=15 * i) for i in range(92)]
        self.assertEqual(sorted(s.interval_start for s in submissions), expected)

    def test_dst_end_repeated_hour_resolves_to_first_pass(self):
        # 2026-04-05: clocks go from 03:00 AEDT back to 02:00 AEST, a 25-hour day sent as 96 values.
        submissions, stats = parse(ausgrid_csv({"2026-04-05": "1"}))
        starts = {s.interval_start for s in submissions}
        self.assertEqual((len(starts), stats.skipped_dst), (96, 0))
        whole_day = {utc(2026, 4, 4, 13) + timedelta(minutes=15 * i) for i in range(100)}
        missing = {utc(2026, 4, 4, 15, 45), utc(2026, 4, 4, 16), utc(2026, 4, 4, 16, 15), utc(2026, 4, 4, 16, 30)}
        self.assertEqual(whole_day - starts, missing)  # left for gap detection

    def test_period_end_utc(self):
        self.assertIsNone(period_end_utc(date(2025, 10, 5), "02:15"))
        self.assertEqual(period_end_utc(date(2025, 10, 5), "03:00"), utc(2025, 10, 4, 16))
        self.assertEqual(period_end_utc(date(2026, 4, 5), "02:15"), utc(2026, 4, 4, 15, 15))  # AEDT
        self.assertEqual(period_end_utc(date(2026, 4, 5), "03:00"), utc(2026, 4, 4, 17))  # AEST

    def test_missing_columns_rejected(self):
        with self.assertRaisesMessage(AusgridFormatError, "missing columns: 24:00"):
            check_header(AUSGRID_HEADER[:-1])

    def test_invalid_date_and_unknown_substation_rejected(self):
        with self.assertRaisesMessage(AusgridFormatError, "line 2: invalid date"):
            parse(ausgrid_csv({"01/06/2025": "1"}))
        with self.assertRaisesMessage(AusgridFormatError, "line 2: unknown substation"):
            parse(ausgrid_csv({"2025-06-01": "1"}, substation="Somewhere Else"))

    def test_read_substations(self):
        text = ausgrid_csv({"2025-06-01": "1"}) + ausgrid_csv({"2025-06-02": "1"}, "Other").split("\n", 1)[1]
        self.assertEqual(read_substations(csv.DictReader(io.StringIO(text))), {SUBSTATION, "Other"})


class ImportReadingsCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        customer = Customer.objects.create(name="Test customer")
        cls.meter = Meter.objects.create(identifier=SUBSTATION, customer=customer, interval_minutes=15)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_import(self, *paths, **options):
        out = io.StringIO()
        call_command("import_readings", *map(str, paths), stdout=out, **options)
        return out.getvalue()

    def test_imports_readings_with_source(self):
        path = self.write("sub.csv", ausgrid_csv({"2025-06-01": {"00:15": "2", "00:30": "-1"}}))
        output = self.run_import(path)
        self.assertIn("1 accepted", output)
        self.assertIn("1 rejected", output)  # negative on a meter that doesn't record export
        self.assertIn("skipped 94 blank, 0 DST", output)
        reading = MeterReading.objects.get()
        self.assertEqual((reading.interval_start, reading.usage), (utc(2025, 5, 31, 14), Decimal("500")))
        self.assertEqual(set(RawReading.objects.values_list("source", flat=True)), {"ausgrid-csv:sub.csv"})

    def test_rerun_adds_no_readings_or_discrepancies(self):
        path = self.write("sub.csv", ausgrid_csv({"2025-06-01": "1", "2025-10-05": "2"}))
        self.run_import(path)
        self.assertEqual(MeterReading.objects.count(), 96 + 92)
        output = self.run_import(path)
        self.assertIn("0 accepted, 188 duplicate (0 conflicting)", output)
        self.assertEqual(MeterReading.objects.count(), 188)
        self.assertFalse(Discrepancy.objects.exists())

    def test_unknown_substation_stops_before_anything_is_stored(self):
        good = self.write("good.csv", ausgrid_csv({"2025-06-01": "1"}))
        bad = self.write("bad.csv", ausgrid_csv({"2025-06-01": "1"}, substation="Nowhere 11kV"))
        with self.assertRaisesMessage(CommandError, "no meter for substation(s): Nowhere 11kV"):
            self.run_import(good, bad)
        self.assertFalse(RawReading.objects.exists())

    def test_meter_with_wrong_interval_stops_import(self):
        Meter.objects.filter(pk=self.meter.pk).update(interval_minutes=30)
        path = self.write("sub.csv", ausgrid_csv({"2025-06-01": "1"}))
        with self.assertRaisesMessage(CommandError, "not set to 15-minute intervals"):
            self.run_import(path)

    def test_malformed_file_stops_import(self):
        path = self.write("bad.csv", "a,b,c\n1,2,3\n")
        with self.assertRaisesMessage(CommandError, "missing columns"):
            self.run_import(path)

    def test_dry_run_stores_nothing(self):
        path = self.write("sub.csv", ausgrid_csv({"2025-06-01": "1"}))
        self.assertIn("would submit 96 values", self.run_import(path, dry_run=True))
        self.assertFalse(RawReading.objects.exists())


class SeedSubstationsCommandTests(TestCase):
    def test_creates_meters_once(self):
        call_command("seed_substations", stdout=io.StringIO())
        output = io.StringIO()
        call_command("seed_substations", stdout=output)
        self.assertEqual(Meter.objects.count(), 6)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(set(Meter.objects.values_list("interval_minutes", flat=True)), {15})
        self.assertEqual(
            list(Meter.objects.filter(records_export=True).values_list("identifier", flat=True)),
            ["Croudace Bay 33_11kV"],
        )
        self.assertNotIn("created", output.getvalue())

    def test_existing_meter_with_other_settings_is_left_unchanged(self):
        customer = Customer.objects.create(name="Someone")
        Meter.objects.create(identifier="Croydon 132_11kV", customer=customer, interval_minutes=30)
        output = io.StringIO()
        call_command("seed_substations", stdout=output)
        self.assertIn("left unchanged", output.getvalue())
        self.assertEqual(Meter.objects.get(identifier="Croydon 132_11kV").interval_minutes, 30)
