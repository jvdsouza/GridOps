"""
Store reading submissions: validate them, keep every one as a RawReading, and turn
the valid ones into MeterReadings without double-counting.

Outcomes per submission:
    accepted   valid, and the first reading for its meter + interval -> new MeterReading
    duplicate  valid, but a reading already exists for that meter + interval
               (same value: a harmless retry; different value: also a duplicate_reading
               Discrepancy, and the stored value is left untouched)
    rejected   failed validation -> suspicious_reading Discrepancy, no MeterReading
               (a negative value is only valid on a meter with records_export)
    unknown    the meter does not exist; nothing can be stored, only counted
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import batched

from django.db import connection, transaction
from django.utils import timezone

from ..models import Discrepancy, Meter, MeterReading, RawReading
from .validators import KWH, ReadingSubmission, validate_reading

DEFAULT_CHUNK_SIZE = 5000

Key = tuple[int, datetime]  # (meter_id, interval_start)
Energy = tuple[Decimal, Decimal]  # (usage kWh, export kWh)


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    """Duplicates whose value differs from the stored reading (also counted in duplicates)."""
    rejected: int = 0
    unknown_meter: int = 0

    @property
    def total(self) -> int:
        return self.accepted + self.duplicates + self.rejected + self.unknown_meter


def ingest_readings(
    submissions: Iterable[ReadingSubmission],
    *,
    source: str,
    now: datetime | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> IngestResult:
    """
    Validate and store reading submissions.

    Each chunk is committed in its own transaction, so a large import that fails part
    way keeps the chunks already processed. Re-running the same import is safe:
    readings already stored come back as duplicates instead of being counted twice.
    """
    now = now or timezone.now()
    result = IngestResult()
    for chunk in batched(submissions, chunk_size):
        _ingest_chunk(chunk, source=source, now=now, result=result)
    return result


@transaction.atomic
def _ingest_chunk(chunk: tuple[ReadingSubmission, ...], *, source: str, now: datetime, result: IngestResult) -> None:
    meters = {
        pk: (interval_minutes, records_export)
        for pk, interval_minutes, records_export in Meter.objects.filter(
            pk__in={s.meter_id for s in chunk}
        ).values_list("pk", "interval_minutes", "records_export")
    }

    checked = []
    candidates: dict[Key, Energy] = {}  # first valid submission per key -> its (usage, export)
    for submission in chunk:
        meter = meters.get(submission.meter_id)
        if meter is None:
            result.unknown_meter += 1
            continue
        interval_minutes, records_export = meter
        outcome = validate_reading(
            submission, interval_minutes=interval_minutes, now=now, records_export=records_export
        )
        checked.append((submission, outcome))
        if outcome.is_valid:
            candidates.setdefault(
                (submission.meter_id, submission.interval_start), (outcome.usage_kwh, outcome.export_kwh)
            )

    inserted = _insert_new_readings(candidates)
    stored = _fetch_readings(candidates.keys())

    raw_readings: list[RawReading] = []
    discrepancy_types: list[str | None] = []
    claimed: set[Key] = set()
    for submission, outcome in checked:
        start = submission.interval_start
        raw = RawReading(
            meter_id=submission.meter_id,
            # A naive timestamp was rejected; store it as UTC rather than guess a zone.
            interval_start=start if start.utcoffset() is not None else start.replace(tzinfo=UTC),
            usage=outcome.raw_usage,
            usage_unit=submission.usage_unit[:10],
            source=source,
            received_at=now,
        )
        discrepancy_type = None

        if not outcome.is_valid:
            raw.status = RawReading.Status.REJECTED
            raw.rejection_reason = "; ".join(outcome.errors)
            discrepancy_type = Discrepancy.Type.SUSPICIOUS_READING
            result.rejected += 1
        else:
            key = (submission.meter_id, start)
            submitted = (outcome.usage_kwh, outcome.export_kwh)
            reading_id, stored_energy = stored[key]
            raw.meter_reading_id = reading_id
            if key in inserted and key not in claimed:
                claimed.add(key)
                raw.status = RawReading.Status.ACCEPTED
                result.accepted += 1
            else:
                raw.status = RawReading.Status.DUPLICATE
                result.duplicates += 1
                if stored_energy != submitted:
                    raw.rejection_reason = (
                        f"conflicts with stored reading {reading_id}: submitted "
                        f"{_describe(submitted)}, stored {_describe(stored_energy)}"
                    )
                    discrepancy_type = Discrepancy.Type.DUPLICATE_READING
                    result.conflicts += 1

        raw_readings.append(raw)
        discrepancy_types.append(discrepancy_type)

    RawReading.objects.bulk_create(raw_readings)  # sets pks on Postgres
    Discrepancy.objects.bulk_create(
        Discrepancy(meter_id=raw.meter_id, raw_reading=raw, type=dtype)
        for raw, dtype in zip(raw_readings, discrepancy_types)
        if dtype is not None
    )


def _describe(energy: Energy) -> str:
    usage, export = energy
    return f"{export} kWh export" if export else f"{usage} kWh"


def _insert_new_readings(candidates: dict[Key, Energy]) -> set[Key]:
    """
    Insert readings that don't exist yet and return the keys this call inserted.

    ON CONFLICT DO NOTHING makes this safe against a concurrent import of the same
    interval: the losing insert waits for the other transaction, then skips the row
    instead of raising, and the row is reported as a duplicate.
    """
    if not candidates:
        return set()
    table = connection.ops.quote_name(MeterReading._meta.db_table)
    meter_ids, starts = zip(*candidates.keys())
    usages, exports = zip(*candidates.values())
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {table} (meter_id, interval_start, usage, export, usage_unit)
            SELECT t.meter_id, t.interval_start, t.usage, t.export, %s
            FROM unnest(%s::bigint[], %s::timestamptz[], %s::numeric[], %s::numeric[])
                AS t(meter_id, interval_start, usage, export)
            ON CONFLICT (meter_id, interval_start) DO NOTHING
            RETURNING meter_id, interval_start
            """,
            [KWH, list(meter_ids), list(starts), list(usages), list(exports)],
        )
        return {(meter_id, start) for meter_id, start in cursor.fetchall()}


def _fetch_readings(keys: Iterable[Key]) -> dict[Key, tuple[int, Energy]]:
    """Return {key: (reading id, (usage, export))} for the given meter + interval pairs."""
    keys = list(keys)
    if not keys:
        return {}
    table = connection.ops.quote_name(MeterReading._meta.db_table)
    meter_ids, starts = zip(*keys)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT r.meter_id, r.interval_start, r.id, r.usage, r.export
            FROM {table} r
            JOIN unnest(%s::bigint[], %s::timestamptz[]) AS k(meter_id, interval_start)
              ON r.meter_id = k.meter_id AND r.interval_start = k.interval_start
            """,
            [list(meter_ids), list(starts)],
        )
        return {
            (meter_id, start): (reading_id, (usage, export))
            for meter_id, start, reading_id, usage, export in cursor.fetchall()
        }
