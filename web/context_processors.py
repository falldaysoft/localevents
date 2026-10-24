"""Template context the public pages need about the viewer."""

from events import interest


def viewer(request):
    """Whether the nav should offer "My events" to someone not signed in.

    A signed-in reader always gets the link. An anonymous one gets it once
    they have marked something — the cookie exists only then — because an
    empty "My events" page in front of a first-time visitor is a promise the
    site has not yet made to them.
    """
    return {"HAS_VISITOR_COOKIE": interest.visitor_id(request) is not None}
