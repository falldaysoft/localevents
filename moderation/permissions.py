"""One access rule for the whole app.

A decorator rather than a mixin or a `LoginRequiredMiddleware` exemption list,
so that adding a view without protecting it is a visible omission at the top of
the function rather than a missing entry in a file somewhere else.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def moderator_required(view):
    """Signed in *and* a moderator.

    Anonymous visitors get the login page — they may well be a moderator who
    is simply logged out. A signed-in non-moderator gets 403 rather than 404:
    the existence of a moderation queue is not a secret, and a bare 404 would
    send a real moderator hunting for a broken link.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_moderator:
            raise PermissionDenied("Moderators only.")
        return view(request, *args, **kwargs)

    return wrapped
