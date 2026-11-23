"""
Metering domain models. See discovery/domain-model.md for the design.

Relationships:
    Customer 1──N Meter 1──N MeterReading
    Meter    1──N RawReading N──1 MeterReading
    Customer 1──N BillingPeriod
    Meter    1──N Discrepancy N──1 RawReading

Every submission is first stored as a RawReading exactly as it arrived. Only
validated submissions become a MeterReading, so nothing that was received is lost.

Foreign keys use PROTECT so that deleting a customer or meter can never silently
wipe out readings, billing history or discrepancy records.
"""

from django.contrib.postgres.indexes import BrinIndex
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Customer(models.Model):
    """The account receiving electricity service."""

    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Meter(models.Model):
    """A device supplying usage data at a fixed reading interval."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="meters")
    interval_minutes = models.PositiveSmallIntegerField(
        default=30,
        help_text="Expected length of each reading interval, in minutes.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(interval_minutes__gt=0),
                name="meter_interval_minutes_positive",
            ),
        ]

    def __str__(self):
        return f"Meter {self.pk} ({self.customer})"


class MeterReading(models.Model):
    """Electricity consumed by a meter during one interval (not a cumulative total)."""

    class UsageUnit(models.TextChoices):
        KWH = "kWh", "kWh"

    # db_index=False: the (meter, interval_start) unique constraint already serves meter lookups.
    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="readings", db_index=False)
    interval_start = models.DateTimeField(help_text="Start of the interval this reading covers.")
    usage = models.DecimalField(max_digits=12, decimal_places=4)
    usage_unit = models.CharField(max_length=10, choices=UsageUnit.choices, default=UsageUnit.KWH)

    class Meta:
        ordering = ["meter", "interval_start"]
        constraints = [
            # At most one reading per meter per interval, so a duplicate submission
            # cannot double-count consumption. Also indexes (meter, interval_start)
            # for range queries and missing-interval detection.
            models.UniqueConstraint(
                fields=["meter", "interval_start"],
                name="meterreading_unique_meter_interval",
            ),
            models.CheckConstraint(
                condition=Q(usage__gte=0),
                name="meterreading_usage_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.meter_id} @ {self.interval_start:%Y-%m-%d %H:%M}: {self.usage} {self.usage_unit}"


class RawReading(models.Model):
    """
    A reading submission exactly as received, before validation.

    Values are stored as submitted (unit, sign and all), so rejected and duplicate
    submissions remain auditable. Accepted and duplicate submissions link to the
    MeterReading they produced or collided with.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        DUPLICATE = "duplicate", "Duplicate"

    # db_index=False: the (meter, interval_start) index already serves meter lookups.
    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="raw_readings", db_index=False)
    interval_start = models.DateTimeField(help_text="Start of the interval, as submitted.")
    usage = models.DecimalField(max_digits=12, decimal_places=4, help_text="Value as submitted; may be negative.")
    usage_unit = models.CharField(max_length=10, help_text="Unit as submitted, e.g. kWh or MW.")
    source = models.CharField(max_length=255, help_text="Where the submission came from, e.g. a file name or 'api'.")
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True)
    meter_reading = models.ForeignKey(
        MeterReading,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="raw_submissions",
        help_text="The reading this submission was accepted as, or duplicated.",
    )

    class Meta:
        ordering = ["received_at", "id"]
        indexes = [
            models.Index(fields=["meter", "interval_start"], name="rawreading_meter_interval"),
            # Rows are append-only, so received_at follows physical order and a BRIN
            # index is a tiny fraction of the size of a B-tree.
            BrinIndex(fields=["received_at"], name="rawreading_received_brin"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="rejected") | ~Q(rejection_reason=""),
                name="rawreading_rejected_has_reason",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["accepted", "duplicate"]) | Q(meter_reading__isnull=False),
                name="rawreading_accepted_has_reading",
            ),
        ]

    def __str__(self):
        return f"{self.meter_id} @ {self.interval_start:%Y-%m-%d %H:%M}: {self.usage} {self.usage_unit} ({self.status})"


class BillingPeriod(models.Model):
    """The window over which a customer's usage is aggregated: [start_at, end_at)."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="billing_periods")
    start_at = models.DateTimeField(help_text="Inclusive start of the period.")
    end_at = models.DateTimeField(help_text="Exclusive end of the period.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["customer", "start_at"]
        indexes = [
            models.Index(fields=["customer", "start_at"], name="billingperiod_customer_start"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")),
                name="billingperiod_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.customer}: {self.start_at:%Y-%m-%d} to {self.end_at:%Y-%m-%d} ({self.status})"


class Discrepancy(models.Model):
    """A detected data problem on a meter that needs investigation."""

    class Type(models.TextChoices):
        MISSING_READING = "missing_reading", "Missing reading"
        DUPLICATE_READING = "duplicate_reading", "Duplicate reading"
        SUSPICIOUS_READING = "suspicious_reading", "Suspicious reading"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    meter = models.ForeignKey(Meter, on_delete=models.PROTECT, related_name="discrepancies")
    raw_reading = models.ForeignKey(
        RawReading,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="discrepancies",
        help_text="The submission that triggered this discrepancy, if any (missing readings have none).",
    )
    type = models.CharField(max_length=30, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    class Meta:
        verbose_name_plural = "discrepancies"
        indexes = [
            models.Index(fields=["meter", "status"], name="discrepancy_meter_status"),
        ]

    def __str__(self):
        return f"{self.get_type_display()} on meter {self.meter_id} ({self.status})"
