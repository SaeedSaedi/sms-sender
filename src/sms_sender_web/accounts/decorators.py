from functools import wraps

from django.shortcuts import render

from .roles import can, role_of


def forbidden(request):
    """The Persian 403 page, saying whether the user has no role yet or their
    role doesn't allow this."""
    return render(request, "403.html", {"no_role": role_of(request.user) is None}, status=403)


def requires(capability: str):
    """Only users whose role allows `capability` (spec 4.12); anyone else
    gets a Persian 403 page that says why."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not can(request.user, capability):
                return forbidden(request)
            return view(request, *args, **kwargs)
        # Read by tests/web/test_access.py: every view says who may open it.
        wrapped.required_capability = capability
        return wrapped
    return decorator
