"""Create the meters for the Ausgrid sample data. Safe to run more than once."""

from django.core.management.base import BaseCommand
from django.db import transaction

from metering.ingestion.sources.ausgrid_csv import INTERVAL_MINUTES
from metering.models import Customer, Meter

# Every meter needs a customer. Substations aren't retail customers, so they share one
# placeholder account; real retail meters would belong to real accounts.
CUSTOMER_NAME = "Ausgrid zone substations (sample data)"

# (identifier as written in the CSVs, records_export)
SUBSTATIONS = [
    # Only substation with negative values: 08:00-18:00, peaking at midday, seasonal. Assumed solar export.
    ("Croudace Bay 33_11kV", True),
    ("Crows Nest 132_11kV", False),
    ("Croydon 132_11kV", False),
    ("Dalley St 132_11kV", False),
    ("Darling Harbour 132_11kV", False),
    ("Darlinghurst 33_11kV", False),
]


class Command(BaseCommand):
    help = "Create the Ausgrid sample substation meters (15-minute; Croudace Bay records export)."

    @transaction.atomic
    def handle(self, *args, **options):
        customer, _ = Customer.objects.get_or_create(name=CUSTOMER_NAME)
        for identifier, records_export in SUBSTATIONS:
            meter, created = Meter.objects.get_or_create(
                identifier=identifier,
                defaults={"customer": customer, "interval_minutes": INTERVAL_MINUTES, "records_export": records_export},
            )
            if created:
                self.stdout.write(f"created  {identifier}")
            elif (meter.interval_minutes, meter.records_export) != (INTERVAL_MINUTES, records_export):
                # Never change an existing meter silently: its stored readings depend on these settings.
                self.stdout.write(
                    self.style.WARNING(
                        f"exists   {identifier}, but with interval_minutes={meter.interval_minutes}, "
                        f"records_export={meter.records_export}; left unchanged"
                    )
                )
            else:
                self.stdout.write(f"exists   {identifier}")
