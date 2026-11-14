"""The reader's side of the weekly email: sign up, confirm, leave.

Nothing here changes on a GET. Mail scanners and link prefetchers open every
link in a message, so a confirmation or unsubscribe link that acted when
fetched would subscribe people who never clicked and unsubscribe people who
never asked — each link opens a page whose button POSTs.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from events import interest

from . import services
from .forms import SignupForm
from .models import Subscriber

def signup(request):
    """The signup form, or, for a verified reader, their own switch.

    The response to a valid POST is the same "check your inbox" whatever the
    address's state, so the form cannot tell anyone who already subscribes.
    The visitor cookie is handed out here if the reader has none, because the
    email's "your weekend" section is built from that device's marks.
    """
    own_email = services.verified_email(request.user)
    own_subscription = (
        Subscriber.objects.filter(email=own_email).first() if own_email else None
    )
    form = SignupForm(request.POST or None)

    if request.method == "POST":
        if not interest.within_rate_limit(
            request, bucket="digest-signups", limit=settings.DIGEST_SIGNUPS_PER_HOUR
        ):
            return HttpResponse(
                "Too many signups from this address. Try later.", status=429
            )
        if form.is_valid():
            new_visitor = None
            if request.user.is_authenticated:
                fingerprint = interest.fingerprint(request) or ""
            else:
                vid = interest.visitor_id(request)
                if vid is None:
                    vid = new_visitor = interest.new_visitor_id()
                fingerprint = interest.fingerprint_for(vid)
            services.request_subscription(
                form.cleaned_data["email"],
                fingerprint=fingerprint,
                user=request.user if request.user.is_authenticated else None,
            )
            response = redirect("digest_sent")
            if new_visitor is not None:
                interest.set_visitor_cookie(response, new_visitor)
            return response
        status = 400
    else:
        status = 200

    return render(
        request,
        "digest/signup.html",
        {
            "form": form,
            "own_email": own_email,
            "own_subscription": own_subscription,
        },
        status=status,
    )


def sent(request):
    return render(request, "digest/sent.html")


def confirm(request, key):
    subscriber = services.subscriber_for_key(key)
    if subscriber is None:
        return render(request, "digest/confirm.html", {"invalid": True}, status=404)
    if request.method == "POST":
        services.confirm(subscriber)
        return render(request, "digest/confirm.html", {"done": True, "subscriber": subscriber})
    return render(
        request,
        "digest/confirm.html",
        {"subscriber": subscriber, "already": subscriber.is_active},
    )


@csrf_exempt
def unsubscribe(request, token):
    """The link in every email, and its RFC 8058 one-click POST.

    CSRF-exempt because a mail provider's "Unsubscribe" button POSTs
    `List-Unsubscribe=One-Click` here with no cookie and no form token. What
    stands in for CSRF is the token in the path: unguessable, and naming only
    the address it was sent to — the worst a forged POST can do is the thing
    the link's owner can do with one click anyway.
    """
    subscriber = Subscriber.objects.filter(token=token).first()
    if subscriber is None:
        raise Http404
    if request.method == "POST":
        services.unsubscribe(subscriber)
        return render(
            request, "digest/unsubscribe.html", {"done": True, "subscriber": subscriber}
        )
    return render(
        request,
        "digest/unsubscribe.html",
        {"subscriber": subscriber, "already": not subscriber.is_active},
    )


@login_required
@require_POST
def profile_toggle(request):
    """The switch on the profile and on /digest/ for a verified reader."""
    if request.POST.get("on") == "1":
        subscriber = services.subscribe_user(
            request.user, fingerprint=interest.fingerprint(request) or ""
        )
        if subscriber is None:
            messages.error(
                request, "Verify your email address first, then turn this on."
            )
        else:
            messages.success(
                request, f"The weekly email will go to {subscriber.email}."
            )
    else:
        email = services.verified_email(request.user)
        subscriber = (
            Subscriber.objects.filter(email=email).first()
            if email
            else getattr(request.user, "digest_subscription", None)
        )
        if subscriber is not None:
            services.unsubscribe(subscriber)
        messages.success(request, "You won't get the weekly email any more.")
    back = request.POST.get("next")
    return redirect("digest_signup" if back == "digest" else "profile")
