from django.db import models


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

    class Meta:
        verbose_name = "site configuration"
        verbose_name_plural = "site configuration"

    def __str__(self):
        return "Site configuration"


class AIConfig(SingletonModel):
    """Which model enriches submissions, and how much it may spend.

    Kept in the database rather than the environment so the provider and model
    can be swapped and compared without a redeploy — the whole point of having
    an OpenRouter path is being able to try a cheaper model and measure it. See
    enrichment.models.EnrichmentRun for the cost record that makes that
    comparison possible.
    """

    class Provider(models.TextChoices):
        ANTHROPIC = "anthropic", "Anthropic"
        OPENROUTER = "openrouter", "OpenRouter"

    enabled = models.BooleanField(
        default=False,
        help_text="When off, submissions skip AI enrichment and go straight to "
        "the submitter for manual completion.",
    )
    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.ANTHROPIC
    )
    base_url = models.URLField(
        blank=True,
        default="https://openrouter.ai/api/v1",
        help_text="OpenRouter only. Ignored by the Anthropic provider.",
    )
    api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Leave blank to fall back to the ANTHROPIC_API_KEY / "
        "OPENROUTER_API_KEY environment variable.",
    )
    model = models.CharField(
        max_length=100,
        default="claude-opus-5",
        help_text="Anthropic model id, or an OpenRouter model slug such as "
        "'anthropic/claude-haiku-4.5' or 'google/gemini-2.0-flash-001'.",
    )
    max_tokens = models.PositiveIntegerField(default=4096)
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
        return f"AI configuration ({self.provider}: {self.model})"

    def resolved_api_key(self):
        from django.conf import settings

        if self.api_key:
            return self.api_key
        if self.provider == self.Provider.ANTHROPIC:
            return settings.ANTHROPIC_API_KEY
        return settings.OPENROUTER_API_KEY
