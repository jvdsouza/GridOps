from django.db import migrations, models
from django.db.models import Q


def backfill_identifiers(apps, schema_editor):
    """Give any existing meters a placeholder identifier so the column can be made unique."""
    Meter = apps.get_model("metering", "Meter")
    for meter in Meter.objects.filter(identifier__isnull=True).only("pk"):
        meter.identifier = f"meter-{meter.pk}"
        meter.save(update_fields=["identifier"])


class Migration(migrations.Migration):
    dependencies = [
        ("metering", "0001_initial"),
    ]

    operations = [
        # RawReading keeps the submitted value at source precision, and blanks as NULL.
        migrations.AlterField(
            model_name="rawreading",
            name="usage",
            field=models.DecimalField(
                blank=True,
                decimal_places=8,
                help_text="Value as submitted; may be negative. Empty if it was blank or not a number.",
                max_digits=20,
                null=True,
            ),
        ),
        # Meter.identifier: add nullable, backfill, then require + make unique.
        migrations.AddField(
            model_name="meter",
            name="identifier",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(backfill_identifiers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="meter",
            name="identifier",
            field=models.CharField(
                help_text="How data sources identify this meter, e.g. a substation name like 'Croudace Bay 33_11kV'.",
                max_length=64,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="meter",
            name="records_export",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Whether this meter measures flow in both directions. If so, a negative submitted "
                    "value is net export (e.g. solar); otherwise it is rejected as suspicious."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="meterreading",
            name="usage",
            field=models.DecimalField(decimal_places=4, help_text="Energy imported from the grid.", max_digits=12),
        ),
        migrations.AddField(
            model_name="meterreading",
            name="export",
            field=models.DecimalField(
                decimal_places=4,
                default=0,
                help_text="Energy exported to the grid. Only non-zero for meters that record export.",
                max_digits=12,
            ),
        ),
        migrations.AddConstraint(
            model_name="meterreading",
            constraint=models.CheckConstraint(
                condition=Q(export__gte=0),
                name="meterreading_export_non_negative",
            ),
        ),
    ]
