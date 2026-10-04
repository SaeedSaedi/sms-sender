from functools import wraps

from django.shortcuts import render

from .roles import can, role_of


def requires(capability: str):
    """Only users whose role allows `capability` (spec 4.12); anyone else
    gets a Persian 403 page that says why."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not can(request.user, capability):
                return render(
                    request, "403.html", {"no_role": role_of(request.user) is None}, status=403,
                )
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
