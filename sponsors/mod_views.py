"""The sponsorship queue. Every view is behind `moderator_required`."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from moderation.permissions import moderator_required

from . import services
from .forms import DecisionForm
from .models import SponsorOrder

Status = SponsorOrder.Status

VIEWS = {
    "review": (
        "Awaiting review",
        lambda qs, today: qs.filter(
            status__in=[Status.AWAITING_REVIEW, Status.PROCESSING]
        ).order_by("hold_expires_at"),
    ),
    "running": (
        "Running and booked",
        lambda qs, today: qs.filter(status=Status.APPROVED, ends_on__gte=today).order_by("starts_on"),
    ),
    "all": ("All", lambda qs, today: qs.exclude(status=Status.CHECKOUT)[:200]),
}


UNFINISHED = (
    "Stripe has done its part, but the record could not be saved just now. "
    "It will finish by itself within the hour, with your decision as made. "
    "There is no need to press the button again."
)


def _chrome(request, **extra):
    from moderation import services as mod_services

    return {"queue_counts": mod_services.queue_counts(request.user), **extra}


@moderator_required
def queue(request):
    view = request.GET.get("view", "review")
    if view not in VIEWS:
        view = "review"
    today = timezone.localdate()
    orders = VIEWS[view][1](
        SponsorOrder.objects.select_related("event", "buyer", "decided_by"), today
    )
    return render(request, "sponsors/moderate.html", _chrome(
        request,
        view=view,
        views=[(key, label) for key, (label, _f) in VIEWS.items()],
        orders=orders,
        today=today,
    ))


@moderator_required
def detail(request, pk):
    order = get_object_or_404(
        SponsorOrder.objects.select_related("event", "buyer", "decided_by"), pk=pk
    )
    others = []
    if order.product == SponsorOrder.Product.PROMOTE_EVENT:
        others = (
            SponsorOrder.objects.holding_a_slot()
            .overlapping(order.starts_on, order.ends_on)
            .exclude(pk=order.pk)
            .select_related("event")
            .order_by("starts_on")
        )
    return render(request, "sponsors/moderate_order.html", _chrome(
        request,
        order=order,
        others=others,
        full_days=(
            services.full_days(
                max(order.starts_on, timezone.localdate()), order.ends_on,
                exclude=order.pk, approved_only=True,
            ) if order.status == Status.AWAITING_REVIEW else []
        ),
        form=DecisionForm(),
        can_decide=request.user.has_perm(services.APPROVE_PERMISSION),
        buyer_relation=services.buyer_relation(order.event, order.buyer),
    ))


@moderator_required
@require_POST
def approve(request, pk):
    order = get_object_or_404(SponsorOrder, pk=pk)
    form = DecisionForm(request.POST)
    note = form.cleaned_data["note"] if form.is_valid() else ""
    try:
        order = services.approve(order, request.user, note=note)
    except services.SponsorError as exc:
        messages.error(request, str(exc))
        return redirect("mod_sponsor", pk=pk)
    if order.status == Status.PROCESSING:
        messages.warning(request, UNFINISHED)
        return redirect("mod_sponsor", pk=pk)
    messages.success(request, "Approved and charged. The buyer has been told.")
    return redirect("mod_sponsors")


@moderator_required
@require_POST
def reject(request, pk):
    order = get_object_or_404(SponsorOrder, pk=pk)
    form = DecisionForm(request.POST)
    note = form.cleaned_data["note"] if form.is_valid() else ""
    try:
        order = services.reject(order, request.user, note)
    except services.SponsorError as exc:
        messages.error(request, str(exc))
        return redirect("mod_sponsor", pk=pk)
    if order.status == Status.PROCESSING:
        messages.warning(request, UNFINISHED)
        return redirect("mod_sponsor", pk=pk)
    messages.success(request, "Rejected. The hold is released and the buyer has been told.")
    return redirect("mod_sponsors")
