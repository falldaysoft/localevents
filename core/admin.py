from django import forms
from django.contrib import admin, messages
from django.core.mail import EmailMessage
from django.shortcuts import redirect
from django.urls import path, reverse

from .models import AIConfig, MailConfig, SiteConfig


class SingletonAdmin(admin.ModelAdmin):
    """Admin for a one-row model: no add, no delete, no changelist worth showing."""

    def has_add_permission(self, request):
        return not self.model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteConfig)
class SiteConfigAdmin(SingletonAdmin):
    # Everything else on this model is prose. These two put arbitrary script on
    # every page and decide which origins the CSP trusts, which is a different
    # kind of decision — so they are visible to any admin and editable only by
    # someone who could already deploy the same code.
    SUPERUSER_ONLY = ("head_html", "script_hosts")

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if request.user.is_superuser:
            return readonly
        return readonly + self.SUPERUSER_ONLY


@admin.register(AIConfig)
class AIConfigAdmin(SingletonAdmin):
    list_display = ("model", "enabled", "daily_spend_cap_usd")


class MailConfigForm(forms.ModelForm):
    """Passwords are write-only: blank keeps what is stored."""

    PASSWORDS = ("smtp_password", "imap_password")

    smtp_password = forms.CharField(
        label="SMTP password", required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the stored password.",
    )
    clear_smtp_password = forms.BooleanField(
        label="Clear the stored SMTP password", required=False
    )
    imap_password = forms.CharField(
        label="IMAP password", required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the stored password.",
    )
    clear_imap_password = forms.BooleanField(
        label="Clear the stored IMAP password", required=False
    )

    class Meta:
        model = MailConfig
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        for name in self.PASSWORDS:
            if cleaned.get(f"clear_{name}"):
                cleaned[name] = ""
            elif not cleaned.get(name):
                cleaned[name] = getattr(self.instance, name)
        if cleaned.get("smtp_use_tls") and cleaned.get("smtp_use_ssl"):
            self.add_error(
                "smtp_use_ssl",
                "Choose one: STARTTLS (usually port 587) or implicit TLS (465).",
            )
        return cleaned


@admin.register(MailConfig)
class MailConfigAdmin(SingletonAdmin):
    """Mail servers and their passwords: superusers only.

    Whoever controls outbound mail controls every password-reset link the
    site sends, which is the whole of every account — the same grant as the
    raw <head> HTML on SiteConfig, so the same gate.
    """

    form = MailConfigForm
    change_form_template = "admin/core/mailconfig/change_form.html"
    fieldsets = (
        ("Outbound (SMTP)", {
            "description": "Blank host: the EMAIL_* environment variables are used, as before.",
            "fields": (
                "smtp_host", ("smtp_port", "smtp_use_tls", "smtp_use_ssl"),
                "smtp_user", "smtp_password", "clear_smtp_password", "from_email",
            ),
        }),
        ("Inbound (IMAP) — event submissions by email", {
            "description": "Blank host: the INBOUND_* environment variables are "
            "used; blank in both turns email submissions off. Use a mailbox "
            "that only receives submissions — every unread message in the "
            "folder is read and marked read.",
            "fields": (
                "imap_host", "imap_port", "imap_user", "imap_password",
                "clear_imap_password", "imap_folder", "inbound_address",
            ),
        }),
    )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def get_object(self, request, object_id, from_field=None):
        # The Test buttons and docs link straight to /1/change/; on a fresh
        # site that row does not exist yet and the admin would bounce away.
        return MailConfig.load() if str(object_id) == "1" else None

    def changelist_view(self, request, extra_context=None):
        # One row: go straight to it rather than a list of one.
        MailConfig.load()
        return redirect("admin:core_mailconfig_change", 1)

    def get_urls(self):
        return [
            path("test-outbound/", self.admin_site.admin_view(self.test_outbound),
                 name="core_mailconfig_test_outbound"),
            path("test-inbound/", self.admin_site.admin_view(self.test_inbound),
                 name="core_mailconfig_test_inbound"),
        ] + super().get_urls()

    def _back(self):
        return redirect(reverse("admin:core_mailconfig_change", args=[1]))

    def test_outbound(self, request):
        """Send one real message to the person pressing the button."""
        from .mail import delivery_connection

        if request.method != "POST" or not request.user.is_superuser:
            return self._back()
        config = MailConfig.load()
        recipient = request.user.email
        if not recipient:
            messages.error(request, "Your account has no email address to send a test to.")
            return self._back()
        connection, via = delivery_connection(config)
        message = EmailMessage(
            subject="Test message from the site's mail configuration",
            body="If you are reading this, outbound mail works.\n\n"
            f"Sent via {via} as {config.resolved_from_email()}.",
            from_email=config.resolved_from_email(),
            to=[recipient],
            connection=connection,
        )
        try:
            message.send()
        except Exception as exc:
            messages.error(
                request, f"Sending through {via} failed: {type(exc).__name__}: {exc}"
            )
        else:
            note = (
                " The console backend prints mail to the server log instead of "
                "delivering it." if "console" in via.lower() else ""
            )
            messages.success(
                request, f"Sent a test message to {recipient} via {via}.{note}"
            )
        return self._back()

    def test_inbound(self, request):
        from submissions.inbound import test_mailbox

        if request.method != "POST" or not request.user.is_superuser:
            return self._back()
        ok, sentence = test_mailbox()
        (messages.success if ok else messages.error)(request, sentence)
        return self._back()
