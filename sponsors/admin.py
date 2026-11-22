from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path, reverse

import stripe

from core.admin import SingletonAdmin

from . import payments
from .models import SponsorConfig, SponsorOrder


class SponsorConfigForm(forms.ModelForm):
    """Keys are write-only: blank keeps what is stored, and nothing is echoed."""

    stripe_secret_key = forms.CharField(
        label="Stripe secret key", required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="sk_test_… or sk_live_… from Developers → API keys in the "
        "Stripe dashboard (a restricted rk_ key works too). Leave blank to keep "
        "the stored key. Stored encrypted; never shown again.",
    )
    clear_stripe_secret_key = forms.BooleanField(
        label="Remove the stored secret key", required=False
    )
    stripe_webhook_secret = forms.CharField(
        label="Webhook signing secret", required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Usually filled in for you by “Set up the webhook” above. "
        "Paste a whsec_… here only if you made the endpoint by hand.",
    )

    class Meta:
        model = SponsorConfig
        fields = "__all__"

    def clean_stripe_secret_key(self):
        key = self.cleaned_data["stripe_secret_key"].strip()
        if key and not key.startswith(("sk_", "rk_")):
            raise forms.ValidationError(
                "That isn't a secret key — it should start with sk_ or rk_. "
                "The publishable pk_ key isn't needed."
            )
        return key

    def clean_stripe_webhook_secret(self):
        secret = self.cleaned_data["stripe_webhook_secret"].strip()
        if secret and not secret.startswith("whsec_"):
            raise forms.ValidationError("A webhook signing secret starts with whsec_.")
        return secret

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().lower()

    def save(self, commit=True):
        config = super().save(commit=False)
        if self.cleaned_data.get("clear_stripe_secret_key"):
            config.stripe_secret_key = ""
            config.stripe_webhook_secret = ""
            config.stripe_webhook_endpoint_id = ""
            config.stripe_account_name = ""
        elif self.cleaned_data.get("stripe_secret_key"):
            config.stripe_secret_key = self.cleaned_data["stripe_secret_key"]
        if self.cleaned_data.get("stripe_webhook_secret"):
            config.stripe_webhook_secret = self.cleaned_data["stripe_webhook_secret"]
        if commit:
            config.save()
        return config


@admin.register(SponsorConfig)
class SponsorConfigAdmin(SingletonAdmin):
    """Prices and the Stripe account: superusers only, like mail settings.

    Whoever holds the secret key can refund, capture and read customers, so
    this is the same grant as deploying the code.
    """

    form = SponsorConfigForm
    change_form_template = "admin/sponsors/sponsorconfig/change_form.html"
    readonly_fields = ("stripe_status",)
    fieldsets = (
        (None, {"fields": ("enabled",)}),
        ("Stripe", {
            "fields": (
                "stripe_status", "stripe_secret_key", "clear_stripe_secret_key",
                "stripe_webhook_secret",
            ),
        }),
        ("Tax and currency", {
            "fields": ("currency", "automatic_tax", "tax_code"),
        }),
        ("Promote an event", {
            "fields": ("promote_price", "promote_max_days", "promote_daily_slots"),
        }),
    )

    @admin.display(description="Status")
    def stripe_status(self, obj):
        parts = [f"Secret key: {obj.key_hint}."]
        if obj.stripe_account_name:
            parts.append(f"Account: {obj.stripe_account_name}.")
        if obj.stripe_webhook_secret:
            parts.append("Webhook: set up.")
        elif obj.stripe_webhook_secret_enc:
            parts.append("Webhook: secret unreadable — set it up again.")
        else:
            parts.append("Webhook: not set up.")
        if obj.enabled and not obj.stripe_ready:
            parts.append("Sponsorships are switched on but can't be sold until both are in place.")
        return " ".join(parts)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def get_object(self, request, object_id, from_field=None):
        return SponsorConfig.load() if str(object_id) == "1" else None

    def changelist_view(self, request, extra_context=None):
        SponsorConfig.load()
        return redirect("admin:sponsors_sponsorconfig_change", 1)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "webhook_url": payments.absolute_url(reverse("sponsor_webhook")),
            "site_base_url": settings.SITE_BASE_URL,
        }
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        return [
            path("test/", self.admin_site.admin_view(self.test_stripe),
                 name="sponsors_sponsorconfig_test"),
            path("webhook/", self.admin_site.admin_view(self.make_webhook),
                 name="sponsors_sponsorconfig_webhook"),
        ] + super().get_urls()

    def _back(self):
        return redirect(reverse("admin:sponsors_sponsorconfig_change", args=[1]))

    def test_stripe(self, request):
        if request.method != "POST" or not request.user.is_superuser:
            return self._back()
        ok, sentence = payments.test_connection(SponsorConfig.load())
        (messages.success if ok else messages.error)(request, sentence)
        return self._back()

    def make_webhook(self, request):
        if request.method != "POST" or not request.user.is_superuser:
            return self._back()
        try:
            url = payments.set_up_webhook(SponsorConfig.load())
        except payments.NotConfigured as exc:
            messages.error(request, f"{exc} Save the secret key first.")
        except stripe.StripeError as exc:
            messages.error(request, f"Stripe refused: {exc.user_message or exc}")
        else:
            messages.success(request, f"Stripe will now notify {url}.")
        return self._back()


@admin.register(SponsorOrder)
class SponsorOrderAdmin(admin.ModelAdmin):
    """A record, not a workbench: decisions are made on /moderate/sponsors/."""

    list_display = ("pk", "product", "event", "buyer", "status", "starts_on", "ends_on", "total", "livemode")
    list_filter = ("status", "product", "livemode")
    search_fields = ("event__title", "buyer__email", "stripe_payment_intent_id", "stripe_checkout_session_id")
    raw_id_fields = ("event", "buyer", "decided_by")

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
