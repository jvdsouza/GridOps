from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

from .celery import app as celery_app


def health(request):
    """Report whether Postgres, Redis (cache) and the Celery broker are reachable."""
    checks = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    try:
        cache.set("healthcheck", "ok", timeout=5)
        checks["redis"] = "ok" if cache.get("healthcheck") == "ok" else "error: cache read mismatch"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    try:
        with celery_app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1)
        checks["celery_broker"] = "ok"
    except Exception as exc:
        checks["celery_broker"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    return JsonResponse({"status": "ok" if healthy else "degraded", **checks}, status=200 if healthy else 503)
