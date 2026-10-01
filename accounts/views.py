from allauth.account.utils import perform_login
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import SiteConfig

from .claim import site_is_claimed
from .forms import ClaimForm, ProfileForm
from .models import ApiToken


@login_required
def profile(request):
    """Everything about your own account, in one place.

    Reached by clicking your name in the nav. It owns almost nothing itself —
    passwords, email addresses and passkeys all live in allauth's own pages,
    which handle re-authentication properly. What it adds is a way in: those
    pages had no link anywhere in the site, so passkeys were a feature you had
    to already know the URL for.
    """
    form = ProfileForm(request.POST or None, instance=request.user)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Your display name has been updated.")
            return redirect("profile")
        status = 400
    else:
        status = 200

    return render(
        request,
        "accounts/profile.html",
        {
            "form": form,
            "passkey_count": Authenticator.objects.filter(
                user=request.user, type=Authenticator.Type.WEBAUTHN
            ).count(),
            "api_tokens": (
                request.user.api_tokens.active() if request.user.is_moderator else []
            ),
            "api_url": f"{settings.SITE_BASE_URL.rstrip('/')}{reverse('api_index')}",
            # Popped, not read: the key exists nowhere else, and it should be
            # on screen exactly once.
            "new_api_key": request.session.pop("new_api_key", None),
        },
        status=status,
    )


@login_required
@require_POST
def create_api_token(request):
    """Issue a key for an agent to submit events with.

    The key goes through the session and a redirect rather than straight into
    this response, so reloading the page that shows it does not post again
    and mint a second one.

    Moderators only. An agent can submit far faster than a person, and the
    queue it fills is the moderators' own time — so a key is something the
    people who pay that cost hand themselves, not something any account can
    mint. The API checks again on every request (see submissions.api), so a
    token stops working the moment its owner stops being a moderator.
    """
    if not request.user.is_moderator:
        raise PermissionDenied("Only moderators can make API tokens.")
    _, key = ApiToken.issue(request.user, request.POST.get("name", ""))
    request.session["new_api_key"] = key
    return redirect(f"{reverse('profile')}#api")


@login_required
@require_POST
def revoke_api_token(request, pk):
    token = get_object_or_404(request.user.api_tokens.active(), pk=pk)
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    messages.success(request, f"“{token.name}” can no longer submit events.")
    return redirect(f"{reverse('profile')}#api")


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
