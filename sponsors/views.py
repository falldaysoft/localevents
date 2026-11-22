"""The buyer's side, and the Stripe webhook."""

import logging
from datetime import timedelta

import stripe
from django.contrib import messages
from django.db import DatabaseError
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from events.models import Event

from . import payments, services
from .forms import PromoteForm
from .models import SponsorConfig, SponsorOrder

logger = logging.getLogger("sponsors")


def sponsor_info(request):
    """What can be bought, for how much, and how it is decided."""
    config = SponsorConfig.load()
    if not config.on_sale:
        raise Http404
    return render(request, "sponsors/info.html", {"config": config})


@login_required
def promote(request, slug):
    config = SponsorConfig.load()
    if not config.on_sale:
        raise Http404
    event = get_object_or_404(Event, slug=slug, status=Event.Status.PUBLISHED)
    today = timezone.localdate()
    last_day = services.last_upcoming_day(event)

    if request.method == "POST":
        form = PromoteForm(request.POST)
        if form.is_valid():
            try:
                _order, url = services.start_promotion(
                    event, request.user, form.cleaned_data["starts_on"],
                    note=form.cleaned_data["note"],
                )
            except services.SponsorError as exc:
                form.add_error(None, str(exc))
            else:
                # Stripe's origin is in the CSP's form-action, which some
                # browsers apply to the redirect that follows a form POST.
                return _see_other(url)
    else:
        form = PromoteForm(initial={"starts_on": _suggested_start(event, today, last_day, config)})

    first = (
        form.cleaned_data.get("starts_on") if form.is_bound and hasattr(form, "cleaned_data")
        else form.initial.get("starts_on")
    )
    window = None
    if first and last_day:
        try:
            window = services.promote_window(event, first, config)
        except services.SponsorError:
            pass

    return render(request, "sponsors/promote.html", {
        "config": config,
        "event": event,
        "form": form,
        "last_day": last_day,
        "window": window,
        "today": today,
    })


def _see_other(url):
    response = HttpResponse(status=303)
    response["Location"] = url
    return response


def _suggested_start(event, today, last_day, config):
    """The latest start that still uses every day: the run ends at the event."""
    if last_day is None:
        return today
    return max(today, last_day - timedelta(days=config.promote_max_days - 1))


@login_required
def my_orders(request):
    orders = (
        SponsorOrder.objects.filter(buyer=request.user)
        .exclude(status=SponsorOrder.Status.CHECKOUT, stripe_checkout_session_id="")
        .select_related("event")
    )
    return render(request, "sponsors/orders.html", {"orders": orders})


def _own_order(request, pk):
    order = get_object_or_404(SponsorOrder.objects.select_related("event"), pk=pk)
    if order.buyer_id != request.user.pk and not request.user.is_moderator:
        raise Http404
    return order


@login_required
def order_detail(request, pk):
    order = _own_order(request, pk)
    return render(request, "sponsors/order.html", {"order": order})


@login_required
def order_return(request, pk):
    """Where Stripe sends the buyer after paying.

    The webhook is the source of truth, but it can arrive a second after the
    buyer does, so this reads the session too rather than showing "at
    checkout" to someone who has just paid.
    """
    order = _own_order(request, pk)
    if order.status == SponsorOrder.Status.CHECKOUT:
        try:
            order = services.sync_checkout(order)
        except (stripe.StripeError, payments.NotConfigured):
            logger.exception("could not read checkout for sponsor order %s", order.pk)
    return redirect("sponsor_order", pk=order.pk)


@login_required
@require_POST
def order_withdraw(request, pk):
    order = _own_order(request, pk)
    try:
        services.withdraw(order, request.user)
    except services.SponsorError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Withdrawn. Nothing was charged.")
    return redirect("sponsor_order", pk=order.pk)


@csrf_exempt
@require_POST
def webhook(request):
    """Stripe's notifications. Authenticated by signature, not by session."""
    try:
        event = payments.parse_webhook(
            request.body, request.headers.get("Stripe-Signature", "")
        )
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponseBadRequest("bad signature")
    try:
        services.handle_webhook(event)
    except (stripe.StripeError, payments.NotConfigured, DatabaseError):
        # A 500 asks Stripe to deliver it again later, which is the point.
        logger.exception("webhook %s could not be applied", event.id)
        return HttpResponse(status=500)
    return HttpResponse("ok")
