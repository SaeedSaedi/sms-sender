from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@login_not_required
@require_GET
def healthz(request):
    """For Docker and DevOps health checks: the app and its database answer.
    Says nothing else, so it can stay open without a login."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})
