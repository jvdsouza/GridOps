"""
Import Ausgrid zone substation CSVs. See discovery/csv-import.md.

Every file's header and substations are checked before anything is stored, so an
unknown substation or a malformed file stops the whole import up front. After that,
each file is streamed through ingest_readings, which commits in chunks and is safe
to re-run: values already stored come back as duplicates.
"""

import csv
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from metering.ingestion import ingest_readings
from metering.ingestion.sources.ausgrid_csv import (
    INTERVAL_MINUTES,
    AusgridFormatError,
    ParseStats,
    check_header,
    iter_submissions,
    read_substations,
)
from metering.models import Meter

SOURCE_PREFIX = "ausgrid-csv:"


def open_csv(path: Path):
    # utf-8-sig: tolerate a byte-order mark if a file was re-saved by a spreadsheet app.
    return open(path, newline="", encoding="utf-8-sig")


class Command(BaseCommand):
    help = "Import Ausgrid zone substation CSV files as meter readings."

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="+", type=Path, help="Ausgrid zone substation CSV files.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check and parse the files without storing anything.",
        )

    def handle(self, *args, files: list[Path], dry_run: bool, **options):
        meter_ids = self.resolve_meters(files)
        for path in files:
            self.import_file(path, meter_ids, dry_run=dry_run)

    def resolve_meters(self, files: list[Path]) -> dict[str, int]:
        """Check every file and map every substation to a meter, before storing anything."""
        names: set[str] = set()
        for path in files:
            try:
                with open_csv(path) as f:
                    reader = csv.DictReader(f)
                    check_header(reader.fieldnames)
                    names |= read_substations(reader)
            except (OSError, AusgridFormatError) as error:
                raise CommandError(f"{path}: {error}") from error

        meters = {
            identifier: (pk, interval_minutes)
            for identifier, pk, interval_minutes in Meter.objects.filter(identifier__in=names).values_list(
                "identifier", "pk", "interval_minutes"
            )
        }
        unknown = sorted(names - meters.keys())
        if unknown:
            raise CommandError(
                f"no meter for substation(s): {', '.join(unknown)}. "
                "Nothing was imported. Create the meters first (e.g. manage.py seed_substations)."
            )
        wrong_interval = sorted(name for name, (_, minutes) in meters.items() if minutes != INTERVAL_MINUTES)
        if wrong_interval:
            raise CommandError(
                f"meter(s) not set to {INTERVAL_MINUTES}-minute intervals: {', '.join(wrong_interval)}. "
                "Nothing was imported."
            )
        return {name: pk for name, (pk, _) in meters.items()}

    def import_file(self, path: Path, meter_ids: dict[str, int], *, dry_run: bool) -> None:
        stats = ParseStats()
        started = time.perf_counter()
        try:
            with open_csv(path) as f:
                submissions = iter_submissions(csv.DictReader(f), meter_ids, stats)
                if dry_run:
                    for _ in submissions:
                        pass
                    result = None
                else:
                    result = ingest_readings(submissions, source=f"{SOURCE_PREFIX}{path.name}")
        except (OSError, AusgridFormatError) as error:
            # Chunks committed before the error are kept; re-running the file is safe.
            raise CommandError(f"{path}: {error}") from error
        elapsed = time.perf_counter() - started

        skipped = f"skipped {stats.skipped_blank} blank, {stats.skipped_dst} DST"
        if result is None:
            self.stdout.write(f"{path.name}: {stats.rows} days, would submit {stats.submitted} values, {skipped}")
            return
        self.stdout.write(
            f"{path.name}: {stats.rows} days, {result.accepted} accepted, {result.duplicates} duplicate "
            f"({result.conflicts} conflicting), {result.rejected} rejected, {skipped} "
            f"[{elapsed:.1f}s]"
        )
