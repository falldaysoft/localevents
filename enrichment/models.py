from django.db import models


class EnrichmentRunQuerySet(models.QuerySet):
    def spend_since(self, when):
        from decimal import Decimal

        total = self.filter(created_at__gte=when).aggregate(
            total=models.Sum("estimated_cost_usd")
        )["total"]
        return total or Decimal("0")


class EnrichmentRun(models.Model):
    """One attempt to read an event out of a page.

    Recorded for every attempt including the free ones, because the question
    this table exists to answer is "is the cheap model good enough" — and that
    needs the failures and the structured-data hits alongside the model calls,
    not just the successful ones.
    """

    class Method(models.TextChoices):
        STRUCTURED = "structured", "Page's own event data"
        LLM = "llm", "Language model"

    class Status(models.TextChoices):
        OK = "ok", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    submission = models.ForeignKey(
        "submissions.Submission",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enrichment_runs",
    )
    # A page is also read *after* publication, when a moderator refreshes a
    # listing from its source. Those calls cost the same money and count
    # against the same daily cap, so they belong in the same table — and
    # without somewhere to put the event they would show up as orphan rows
    # with no submission and no way to tell what they were for.
    event = models.ForeignKey(
        "events.Event",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="enrichment_runs",
    )
    source_url = models.URLField(blank=True)

    method = models.CharField(max_length=12, choices=Method.choices)
    status = models.CharField(max_length=10, choices=Status.choices)

    # The endpoint host rather than a vendor name — with one OpenAI-compatible
    # client the meaningful distinction is where the request went (OpenRouter,
    # OpenAI direct, a local Ollama), which is what you want when comparing
    # cost and latency.
    endpoint = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=6, default=0
    )

    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EnrichmentRunQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["created_at", "status"])]

    def __str__(self):
        label = self.model or self.get_method_display()
        return f"{label} — {self.status} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def was_free(self):
        return self.method == self.Method.STRUCTURED
