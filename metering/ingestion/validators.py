"""
Rules for a single reading submission.

Everything here is pure: no database access, and the current time is passed in. The
rules can be unit tested in isolation and reused by every ingestion path (CSV
import, API, Celery).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

KWH = "kWh"
MW = "MW"
SUPPORTED_UNITS = frozenset({KWH, MW})

# Match the database columns so a valid reading can never overflow on insert.
KWH_QUANTUM = Decimal("0.0001")  # MeterReading.usage: numeric(12, 4)
MAX_KWH = Decimal("99999999.9999")
RAW_QUANTUM = Decimal("0.00000001")  # RawReading.usage: numeric(20, 8)
MAX_RAW_ABS = Decimal("999999999999.99999999")


@dataclass(frozen=True)
class ReadingSubmission:
    """One reading as it arrived from a source, before any checks."""

    meter_id: int
    interval_start: datetime
    usage: Decimal | str | int | float | None
    usage_unit: str


@dataclass(frozen=True)
class ValidationOutcome:
    raw_usage: Decimal | None
    """The submitted value parsed as a number, ready for RawReading. None if unusable."""

    usage_kwh: Decimal | None
    """Energy imported, in kWh, for MeterReading.usage. None unless the reading is valid."""

    export_kwh: Decimal | None
    """Energy exported, in kWh, for MeterReading.export. None unless the reading is valid."""

    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_usage(value) -> Decimal | None:
    """Parse a submitted usage value; None if it is blank or not a finite number."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    else:
        # str() first so floats like 0.1 don't become 0.1000000000000000055...
        text = str(value).strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    return number if number.is_finite() else None


def to_kwh(value: Decimal, unit: str, interval_minutes: int) -> Decimal:
    """
    Convert a submitted value to kWh consumed over one interval.

    MW is average power across the interval, so energy = MW x 1000 x hours.
    """
    if unit == KWH:
        return value
    if unit == MW:
        return value * 1000 * interval_minutes / 60
    raise ValueError(f"unsupported unit {unit!r}")


def is_aligned(interval_start: datetime, interval_minutes: int) -> bool:
    """True if interval_start falls on an interval boundary (measured from UTC midnight)."""
    seconds = int((interval_start - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())
    return interval_start.microsecond == 0 and seconds % (interval_minutes * 60) == 0


def validate_reading(
    submission: ReadingSubmission,
    *,
    interval_minutes: int,
    now: datetime,
    records_export: bool = False,
) -> ValidationOutcome:
    """
    Check one submission against the rules that need no other readings.

    A negative value is net export when the meter records export, and is split into
    usage = 0 and export = |value|. On any other meter it is rejected.
    """
    errors: list[str] = []

    usage = parse_usage(submission.usage)
    if usage is None:
        errors.append(f"usage {submission.usage!r} is not a number")
    elif abs(usage) > MAX_RAW_ABS:
        errors.append(f"usage {usage} is out of range")
        usage = None
    else:
        usage = usage.quantize(RAW_QUANTUM, rounding=ROUND_HALF_UP)

    unit_ok = submission.usage_unit in SUPPORTED_UNITS
    if not unit_ok:
        errors.append(f"unsupported unit {submission.usage_unit!r}; expected one of {sorted(SUPPORTED_UNITS)}")

    start = submission.interval_start
    if start.tzinfo is None or start.utcoffset() is None:
        errors.append("interval_start has no timezone")
    else:
        if not is_aligned(start, interval_minutes):
            errors.append(
                f"interval_start {start.isoformat()} is not on a {interval_minutes}-minute interval boundary"
            )
        if start + timedelta(minutes=interval_minutes) > now:
            errors.append(f"interval starting {start.isoformat()} has not finished yet")

    usage_kwh = export_kwh = None
    if usage is not None and unit_ok:
        if usage < 0 and not records_export:
            errors.append(
                f"usage {usage.normalize()} {submission.usage_unit} is negative "
                "and this meter does not record export"
            )
        else:
            kwh = to_kwh(abs(usage), submission.usage_unit, interval_minutes).quantize(
                KWH_QUANTUM, rounding=ROUND_HALF_UP
            )
            if kwh > MAX_KWH:
                errors.append(f"{kwh} kWh is out of range")
            zero = Decimal(0).quantize(KWH_QUANTUM)
            usage_kwh, export_kwh = (zero, kwh) if usage < 0 else (kwh, zero)

    valid = not errors
    return ValidationOutcome(
        raw_usage=usage,
        usage_kwh=usage_kwh if valid else None,
        export_kwh=export_kwh if valid else None,
        errors=tuple(errors),
    )
