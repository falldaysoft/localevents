from allauth.account.utils import perform_login
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from core.models import SiteConfig

from .claim import site_is_claimed
from .forms import ClaimForm


def claim(request):
    """Hand this site to its first administrator.

    Gone — a genuine 404, not a redirect — as soon as a superuser exists. A
    redirect would tell a passer-by that the page had once been here, which is
    an invitation to go looking for freshly deployed instances.
    """
    if site_is_claimed():
        raise Http404("This site has already been claimed.")

    if request.method != "POST":
        return render(request, "accounts/claim.html", {"form": ClaimForm()})

    form = ClaimForm(request.POST)
    if not form.is_valid():
        return render(request, "accounts/claim.html", {"form": form}, status=400)

    with transaction.atomic():
        # Two people posting at once would both have passed the check above.
        # Holding the SiteConfig row makes the second wait here and then lose,
        # rather than quietly producing a second superuser. It is a narrow
        # window, but it is the window in which the site is worth taking.
        SiteConfig.load()
        SiteConfig.objects.select_for_update().filter(pk=1).first()
        if get_user_model().objects.filter(is_superuser=True).exists():
            raise Http404("This site has already been claimed.")
        user = form.save()

    messages.success(
        request,
        f"{settings.SITE_NAME} is yours. Moderation and the admin are on the "
        "menu above.",
    )
    # The address was marked verified as the account was created, so this
    # clears the mandatory-verification stage without email having to be
    # deliverable yet — which it usually is not, this early.
    return perform_login(request, user, redirect_url=reverse("index"))
