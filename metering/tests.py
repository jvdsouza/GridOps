from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from .ingestion import ReadingSubmission, ingest_readings, validate_reading
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
