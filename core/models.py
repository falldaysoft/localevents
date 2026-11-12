from django.db import models

from .themes import DEFAULT_THEME, theme_choices


class SingletonModel(models.Model):
    """A model with exactly one row, edited in the admin.

    Used for settings that a moderator should be able to change without a
    redeploy. Anything that must be known before the database is available
    (hostnames, secrets, map bounds) belongs in settings.py instead.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SiteConfig(SingletonModel):
    """Editable prose for this instance.

    The hard identity (site name, map bounds, timezone) lives in settings.py so
    it is fixed at deploy time. This is the soft half — the text a moderator
    will actually want to revise.
    """

    about_text = models.TextField(
        blank=True,
        help_text="Shown on the About page. Markdown-flavoured plain text.",
    )
    submission_guidelines = models.TextField(
        blank=True,
        help_text="Shown above the submission form. What belongs here, what doesn't.",
    )
    code_of_conduct = models.TextField(blank=True)
    footer_links = models.JSONField(
        default=list,
        blank=True,
        help_text='List of {"label": ..., "url": ...} objects.',
    )
    head_html = models.TextField(
        blank=True,
        verbose_name="extra <head> HTML",
        help_text=(
            "Injected verbatim into &lt;head&gt; on every page: a search-console "
            "verification tag, an analytics snippet. Not escaped and not "
            "checked — whatever is pasted here runs on every page, for every "
            "visitor. Superusers only."
        ),
    )
    theme = models.CharField(
        max_length=32,
        default=DEFAULT_THEME,
        choices=theme_choices,
        help_text=(
            "How the public pages look. Changing this restyles the site for "
            "every visitor immediately; it does not touch any listing."
        ),
    )
    quiet_horizon_days = models.PositiveSmallIntegerField(
        default=14,
        verbose_name="quiet listings horizon (days)",
        help_text=(
            "How far ahead the front page shows Quiet listings, folded under "
            "their day. Beyond this they appear only on venue, category and "
            "search pages. Featured and Listed events are not affected."
        ),
    )
    script_hosts = models.TextField(
        blank=True,
        verbose_name="script hosts to allow",
        help_text=(
            "One origin per line, e.g. https://plausible.io. Anything the HTML "
            "above loads a script from, or sends data to, has to be listed "
            "here or the Content-Security-Policy blocks it — and a blocked "
            "snippet looks exactly like one that was never pasted."
        ),
    )

    class Meta:
        verbose_name = "site configuration"
        verbose_name_plural = "site configuration"

    def __str__(self):
        return "Site configuration"

    @property
    def script_host_list(self):
        """`script_hosts` as origins, ignoring blank lines and stray commas."""
        return [
            host.strip().rstrip(";,")
            for host in self.script_hosts.splitlines()
            if host.strip()
        ]


def site_config_for(request):
    """`SiteConfig.load()`, once per request.

    The head HTML is wanted in two places on every page — the template that
    renders it and the middleware that widens the CSP to let it work — and
    without this that is two queries per request for one singleton row.
    """
    config = getattr(request, "_site_config", None)
    if config is None:
        config = SiteConfig.load()
        request._site_config = config
    return config


class AIConfig(SingletonModel):
    """Which model enriches submissions, and how much it may spend.

    One code path, pointed at any OpenAI-compatible endpoint. OpenRouter is the
    default because it reaches every model worth using behind a single URL,
    but the same client works against OpenAI directly, a self-hosted LiteLLM
    proxy, or Ollama on the same machine.

    Kept in the database rather than the environment so the model can be
    swapped and compared without a redeploy — see enrichment.models
    .EnrichmentRun for the cost record that makes that comparison meaningful.
    """

    enabled = models.BooleanField(
        default=False,
        help_text="When off, submissions skip AI enrichment and go straight to "
        "the submitter for manual completion.",
    )
    base_url = models.URLField(
        blank=True,
        default="https://openrouter.ai/api/v1",
        help_text="Any OpenAI-compatible endpoint. Leave as-is for OpenRouter.",
    )
    api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Leave blank to fall back to the OPENROUTER_API_KEY "
        "environment variable.",
    )
    model = models.CharField(
        max_length=100,
        default="anthropic/claude-sonnet-5",
        help_text="A model slug the endpoint understands, e.g. "
        "'anthropic/claude-sonnet-5' or 'google/gemini-2.0-flash-001'.",
    )
    max_tokens = models.PositiveIntegerField(
        default=8192,
        help_text="Caps thinking and reply together on models that think, so "
        "leave headroom above the size of the JSON you expect back.",
    )

    # Learned, not configured. OpenRouter fronts models whose `json_schema`
    # support varies — that variety is the reason to use it — and the only way
    # to find out is to ask. Asking is expensive: a rejection measured 203
    # seconds before it came back, as a 200 with an error body rather than an
    # HTTP error, and every enrichment paid it again. So the answer is
    # remembered against the model it was learned for, and a model change
    # retires it automatically rather than needing anyone to remember.
    json_schema_probed_model = models.CharField(
        max_length=100, blank=True, editable=False
    )
    json_schema_supported = models.BooleanField(null=True, editable=False)

    # Kept here rather than hardcoded because rates change and differ per model
    # — an estimate that silently goes stale is worse than no estimate.
    input_cost_per_mtok = models.DecimalField(
        max_digits=8, decimal_places=2, default=3,
        help_text="USD per million input tokens, for cost estimates.",
    )
    output_cost_per_mtok = models.DecimalField(
        max_digits=8, decimal_places=2, default=15,
        help_text="USD per million output tokens, for cost estimates.",
    )
    daily_spend_cap_usd = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=5,
        help_text="Enrichment stops for the day once estimated spend exceeds "
        "this. 0 disables the cap.",
    )

    class Meta:
        verbose_name = "AI configuration"
        verbose_name_plural = "AI configuration"

    def __str__(self):
        return f"AI configuration ({self.model})"

    def resolved_api_key(self):
        from django.conf import settings

        return self.api_key or settings.OPENROUTER_API_KEY

    def json_schema_support(self):
        """True, False, or None when it has not been established yet.

        Deliberately answers None as soon as `model` changes: what one model
        accepts says nothing about the next one.
        """
        if self.json_schema_probed_model != self.model:
            return None
        return self.json_schema_supported

    def remember_json_schema_support(self, supported):
        """Record what the endpoint just told us about the current model.

        A targeted UPDATE rather than save(): this runs on a worker in the
        middle of an extraction, and a full save would write back whatever
        else this in-memory copy is holding, clobbering an admin edit made
        while the model was thinking.
        """
        if self.json_schema_support() is supported:
            return
        self.json_schema_probed_model = self.model
        self.json_schema_supported = supported
        type(self).objects.filter(pk=1).update(
            json_schema_probed_model=self.model,
            json_schema_supported=supported,
        )

    def estimate_cost(self, input_tokens, output_tokens):
        """USD for one call, from the configured rates."""
        from decimal import Decimal

        return (
            Decimal(input_tokens or 0) / Decimal(1_000_000) * self.input_cost_per_mtok
            + Decimal(output_tokens or 0)
            / Decimal(1_000_000)
            * self.output_cost_per_mtok
        )

    def spent_today(self):
        from django.utils import timezone

        from enrichment.models import EnrichmentRun

        return EnrichmentRun.objects.spend_since(
            timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        )

    def is_within_budget(self):
        """False once today's estimated spend exceeds the cap. 0 disables it."""
        if not self.daily_spend_cap_usd:
            return True
        return self.spent_today() < self.daily_spend_cap_usd


class MailConfig(SingletonModel):
    """Mail server settings, editable without a redeploy.

    Each half is all-or-nothing: with an SMTP host here, *every* outbound
    setting comes from this row, and with none, every one comes from the
    environment as before. Mixing the two — a host from here and a password
    from .env — is how a change of provider half-applies and fails in a way
    nobody can read off either screen.

    Passwords are stored as entered, like the AI key beside them; the admin
    for this model is superuser-only for that reason, and never echoes them.
    """

    smtp_host = models.CharField(
        "SMTP host", max_length=200, blank=True,
        help_text="Leave blank to use the EMAIL_* environment variables.",
    )
    smtp_port = models.PositiveIntegerField("SMTP port", default=587)
    smtp_use_tls = models.BooleanField(
        "STARTTLS", default=True, help_text="Port 587 wants this on."
    )
    smtp_use_ssl = models.BooleanField(
        "Implicit TLS", default=False,
        help_text="Port 465 wants this on and STARTTLS off.",
    )
    smtp_user = models.CharField("SMTP user", max_length=200, blank=True)
    smtp_password = models.CharField("SMTP password", max_length=500, blank=True)
    from_email = models.CharField(
        "Send as", max_length=254, blank=True,
        help_text="e.g. 'Town Events <noreply@example.org>'. Blank uses "
        "DEFAULT_FROM_EMAIL.",
    )

    imap_host = models.CharField(
        "IMAP host", max_length=200, blank=True,
        help_text="The mailbox people email events to. Leave blank to use the "
        "INBOUND_* environment variables; blank in both means email "
        "submissions are off.",
    )
    imap_port = models.PositiveIntegerField("IMAP port", default=993)
    imap_user = models.CharField("IMAP user", max_length=200, blank=True)
    imap_password = models.CharField("IMAP password", max_length=500, blank=True)
    imap_folder = models.CharField("IMAP folder", max_length=200, default="INBOX")
    inbound_address = models.EmailField(
        "Submissions address", blank=True,
        help_text="The address shown on the submit page and set as Reply-To "
        "on our answers.",
    )

    class Meta:
        verbose_name = "mail configuration"
        verbose_name_plural = "mail configuration"

    def __str__(self):
        return "Mail configuration"

    def outbound(self):
        """SMTP keyword arguments, or None to use the environment's backend."""
        if not self.smtp_host:
            return None
        return {
            "host": self.smtp_host,
            "port": self.smtp_port,
            "username": self.smtp_user,
            "password": self.smtp_password,
            "use_tls": self.smtp_use_tls and not self.smtp_use_ssl,
            "use_ssl": self.smtp_use_ssl,
        }

    def resolved_from_email(self):
        from django.conf import settings

        return self.from_email or settings.DEFAULT_FROM_EMAIL

    def inbound(self):
        from django.conf import settings

        if self.imap_host:
            return ImapSettings(
                self.imap_host, self.imap_port, self.imap_user,
                self.imap_password, self.imap_folder or "INBOX",
                self.inbound_address,
            )
        return ImapSettings(
            settings.INBOUND_IMAP_HOST, settings.INBOUND_IMAP_PORT,
            settings.INBOUND_IMAP_USER, settings.INBOUND_IMAP_PASSWORD,
            settings.INBOUND_IMAP_FOLDER,
            self.inbound_address or settings.INBOUND_EMAIL_ADDRESS,
        )


class ImapSettings:
    def __init__(self, host, port, user, password, folder, address):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.folder = folder
        self.address = address

    @property
    def configured(self):
        return bool(self.host and self.user)
