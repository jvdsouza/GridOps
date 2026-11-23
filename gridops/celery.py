import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gridops.settings")

app = Celery("gridops")

# Read Celery config from Django settings; keys are prefixed with CELERY_ (e.g. CELERY_BROKER_URL).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load tasks.py from every installed Django app.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=False)
def debug_task(self):
    """Round-trip check: returns the worker hostname that ran it."""
    return f"pong from {self.request.hostname}"
