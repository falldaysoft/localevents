"""Whether this instance has an administrator yet.

A freshly deployed instance has no superuser, and making one used to mean
`kubectl exec` into the pod for `createsuperuser` and then `verify_email` —
two commands, an interactive TTY, and a machine on the cluster's IP allowlist,
all to get past a screen a browser could perfectly well show. `/claim/`
replaces that: the first person to reach an unclaimed site becomes its
administrator, and the page stops existing the moment anyone does.

That is first-come-first-served on purpose. The exposure is the window between
`make deploy` and the operator opening the page, which is theirs to keep
short. The alternatives all gate the claim behind a secret, and delivering
that secret costs exactly the `kubectl` round-trip this exists to remove.
"""

from django.contrib.auth import get_user_model

_claimed = False


def site_is_claimed():
    """True once any superuser exists.

    Latched, because every page render consults this to decide whether to show
    the banner, and the answer changes once in a site's lifetime. The latch
    fails closed: deleting the last superuser does not reopen `/claim/` until
    the process restarts. For a page that hands out root, erring towards
    "closed" is the only acceptable direction to be wrong in.
    """
    global _claimed
    if not _claimed:
        _claimed = get_user_model().objects.filter(is_superuser=True).exists()
    return _claimed


def reset_claim_cache():
    """Drop the latch.

    Only tests need this — they create and destroy superusers within one
    process, which no running site does.
    """
    global _claimed
    _claimed = False
