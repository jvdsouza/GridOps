"""
Ausgrid zone substation CSVs -> ReadingSubmissions.

File format (see discovery/csv-import.md):
    year, Zone Substation, Date, Unit, then 96 period-ending labels 00:15 ... 24:00.
    One row per day. The h:00 columns come after h:45, so columns are looked up by name.
    Times are Sydney wall-clock time, but every day has 96 values, even across DST changes:
      - DST start (e.g. 2025-10-05): labels 02:00-02:45 name times that don't exist. Skipped.
      - DST end (e.g. 2026-04-05): the repeated hour is sent once. Resolved to the first pass
        (fold=0, AEDT); the intervals with no value are left for gap detection.

Everything here is pure: no database access. Meters are resolved by the caller.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..validators import ReadingSubmission

SYDNEY = ZoneInfo("Australia/Sydney")
INTERVAL_MINUTES = 15
INTERVAL = timedelta(minutes=INTERVAL_MINUTES)

SUBSTATION = "Zone Substation"
DATE = "Date"
UNIT = "Unit"
REQUIRED_COLUMNS = ("year", SUBSTATION, DATE, UNIT)
# Period-ending labels in time order: 00:15, 00:30, ..., 23:45, 24:00.
LABELS = tuple(f"{m // 60:02d}:{m % 60:02d}" for m in range(INTERVAL_MINUTES, 24 * 60 + 1, INTERVAL_MINUTES))


class AusgridFormatError(ValueError):
    """The file isn't in the expected Ausgrid zone substation format."""


@dataclass
class ParseStats:
    rows: int = 0
    submitted: int = 0
    skipped_blank: int = 0
    skipped_dst: int = 0
    """Labels naming a wall-clock time that doesn't exist (the hour lost when DST starts)."""


def check_header(fieldnames: Iterable[str] | None) -> None:
    """Raise AusgridFormatError unless every required column and all 96 labels are present."""
    present = set(fieldnames or ())
    missing = [column for column in (*REQUIRED_COLUMNS, *LABELS) if column not in present]
    if missing:
        shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        raise AusgridFormatError(f"missing columns: {shown}")


def read_substations(rows: Iterable[Mapping[str, str]]) -> set[str]:
    """The distinct substation names in a file, so meters can be resolved before importing."""
    return {row[SUBSTATION] for row in rows}


def period_end_utc(day: date, label: str) -> datetime | None:
    """
    The UTC instant at which the period labelled `label` on `day` ends.

    Returns None when that Sydney wall-clock time doesn't exist (DST start). Ambiguous
    times (DST end) resolve to the first occurrence, AEDT.
    """
    hours, minutes = map(int, label.split(":"))
    # Wall-clock arithmetic on a naive datetime, so 24:00 becomes the next day's 00:00.
    wall = datetime(day.year, day.month, day.day) + timedelta(hours=hours, minutes=minutes)
    end = wall.replace(tzinfo=SYDNEY, fold=0).astimezone(UTC)
    # zoneinfo silently moves a time that doesn't exist onto a real one (02:15 -> 03:15 AEDT),
    # which would collide with the real 03:15 value. A round trip exposes it.
    if end.astimezone(SYDNEY).replace(tzinfo=None) != wall:
        return None
    return end


def iter_submissions(
    rows: Iterable[Mapping[str, str]],
    meter_ids: Mapping[str, int],
    stats: ParseStats,
) -> Iterator[ReadingSubmission]:
    """
    Yield one ReadingSubmission per non-blank value, updating `stats` as it goes.

    `meter_ids` maps each substation name to its Meter pk. Every name in the file must be
    present; resolve them with read_substations first. Values are passed on as text so
    validation sees them exactly as written (6 decimal places, 7.07E-4).
    """
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        try:
            meter_id = meter_ids[row[SUBSTATION]]
            day = date.fromisoformat(row[DATE])
        except KeyError as error:
            raise AusgridFormatError(f"line {line}: unknown substation {row.get(SUBSTATION)!r}") from error
        except (TypeError, ValueError) as error:
            raise AusgridFormatError(f"line {line}: invalid date {row.get(DATE)!r}") from error
        unit = row[UNIT]
        stats.rows += 1

        for label in LABELS:
            end = period_end_utc(day, label)
            if end is None:
                stats.skipped_dst += 1
                continue
            value = (row.get(label) or "").strip()
            if not value:
                stats.skipped_blank += 1  # gap detection records it as missing
                continue
            stats.submitted += 1
            yield ReadingSubmission(meter_id=meter_id, interval_start=end - INTERVAL, usage=value, usage_unit=unit)
