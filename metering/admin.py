from django.contrib import admin

from .models import BillingPeriod, Customer, Discrepancy, Meter, MeterReading, RawReading


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["id", "name"]
    search_fields = ["name"]


@admin.register(Meter)
class MeterAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "interval_minutes"]
    list_select_related = ["customer"]


@admin.register(MeterReading)
class MeterReadingAdmin(admin.ModelAdmin):
    list_display = ["meter", "interval_start", "usage", "usage_unit"]
    list_filter = ["meter"]
    date_hierarchy = "interval_start"


@admin.register(RawReading)
class RawReadingAdmin(admin.ModelAdmin):
    list_display = ["meter", "interval_start", "usage", "usage_unit", "status", "source", "received_at"]
    list_filter = ["status", "usage_unit"]
    search_fields = ["source", "rejection_reason"]


@admin.register(BillingPeriod)
class BillingPeriodAdmin(admin.ModelAdmin):
    list_display = ["customer", "start_at", "end_at", "status"]
    list_filter = ["status"]


@admin.register(Discrepancy)
class DiscrepancyAdmin(admin.ModelAdmin):
    list_display = ["id", "meter", "type", "status"]
    list_filter = ["type", "status"]
