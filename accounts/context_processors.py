from .claim import site_is_claimed


def claim(request):
    """Tell every page whether this site still has no administrator.

    An unclaimed site is a temporary state that nobody should be able to miss —
    it is both the operator's next step and, until they take it, an open door.
    Hence a banner on every page rather than a line in the README.
    """
    return {"SITE_IS_UNCLAIMED": not site_is_claimed()}
