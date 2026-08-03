"""What we try to pull out of a page.

Every field is optional except the title. A partial extraction is genuinely
useful — the submitter is going to review it either way, and a form filled in
badly is still far less work than a form filled in from scratch. Refusing to
return anything because one field was ambiguous would be the wrong trade.

The same schema is used whichever path produced it, so the confirmation form
does not have to know where the values came from.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ExtractedOccurrence(BaseModel):
    start: datetime = Field(description="Local start time, ISO 8601.")
    end: datetime | None = Field(
        default=None, description="Local end time if stated, otherwise null."
    )
    note: str = Field(
        default="", max_length=200,
        description="Anything specific to this date, e.g. a guest performer.",
    )


class EventDraft(BaseModel):
    """A proposed listing, pending the submitter's review."""

    title: str = Field(description="The event's name, as the page gives it.")
    summary: str = Field(
        default="", max_length=300,
        description="One plain sentence describing the event.",
    )
    description: str = Field(
        default="", description="Fuller description, plain text, no markup."
    )

    venue_name: str = Field(default="", max_length=200)
    venue_address: str = Field(default="", max_length=300)
    venue_city: str = Field(default="", max_length=120)
    organizer_name: str = Field(default="", max_length=200)

    is_free: bool = Field(
        default=False, description="True only if the page says it is free."
    )
    price_min: float | None = None
    price_max: float | None = None
    price_note: str = Field(
        default="", max_length=120,
        description="e.g. 'Pay what you can', 'Members free'.",
    )

    ticket_url: str = Field(default="", max_length=500)
    image_url: str = Field(default="", max_length=500)

    is_family_friendly: bool = False
    age_min: int | None = None
    age_max: int | None = None
    accessibility_notes: str = ""

    category_slugs: list[str] = Field(
        default_factory=list,
        description="Zero or more slugs from the supplied list. Do not invent.",
    )

    occurrences: list[ExtractedOccurrence] = Field(
        default_factory=list,
        description="Every date the page states, soonest first.",
    )
    is_series: bool = Field(
        default=False,
        description="True if this repeats on a schedule, e.g. weekly.",
    )

    notes_for_submitter: str = Field(
        default="", max_length=400,
        description="Anything ambiguous or missing that the submitter should "
        "check. This is shown to them, so write it for a person.",
    )

    def occurrence_count(self):
        return len(self.occurrences)
