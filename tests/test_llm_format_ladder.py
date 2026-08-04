"""The response_format ladder, and not paying for the same answer twice.

OpenRouter fronts models whose `json_schema` support varies. When a model
refuses it, the refusal arrives as HTTP 200 with an error body and no choices
— and it is slow: one measured refusal took 203 seconds. Re-asking on every
extraction turned a one-minute wait into a four-minute one, which is what
these tests exist to prevent.
"""

import pytest

from core.models import AIConfig
from enrichment import llm
from enrichment.llm import LLMError, extract

GOOD_JSON = '{"title": "Pancake Breakfast", "occurrences": []}'


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 20


class FakeResponse:
    """Either a real reply, or the choice-less 200 that means 'unsupported'."""

    def __init__(self, content=None, error=None):
        self.choices = [FakeChoice(content)] if content is not None else None
        self.error = error
        self.usage = FakeUsage()


class FakeClient:
    """Records the response_format of every call it is given."""

    def __init__(self, responder):
        self.calls = []
        self.chat = self
        self.completions = self
        self._responder = responder

    def create(self, **kwargs):
        self.calls.append(kwargs.get("response_format"))
        return self._responder(kwargs.get("response_format"))

    @property
    def formats_tried(self):
        return [
            (f or {}).get("type") if isinstance(f, dict) else None for f in self.calls
        ]


@pytest.fixture
def config(db):
    config = AIConfig.load()
    config.enabled = True
    config.api_key = "test-key"
    config.model = "anthropic/claude-sonnet-5"
    config.save()
    return config


def run(monkeypatch, config, responder):
    client = FakeClient(responder)
    monkeypatch.setattr(llm, "OpenAI", lambda **kw: client, raising=False)
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kw: client
    )  # extract() imports it inside the function
    return client


def refuses_json_schema(response_format):
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        return FakeResponse(error={"message": "Schema is too complex.", "code": 400})
    return FakeResponse(content=GOOD_JSON)


def accepts_everything(response_format):
    return FakeResponse(content=GOOD_JSON)


def test_a_refusal_is_remembered_against_the_model(monkeypatch, config):
    client = run(monkeypatch, config, refuses_json_schema)

    extract(config, "a page", "https://example.com/e", ["music"])

    assert client.formats_tried == ["json_schema", "json_object"]
    config.refresh_from_db()
    assert config.json_schema_supported is False
    assert config.json_schema_probed_model == "anthropic/claude-sonnet-5"


def test_the_next_extraction_skips_the_rung_that_refused(monkeypatch, config):
    """The whole point: the 203-second detour is paid once, not every time."""
    run(monkeypatch, config, refuses_json_schema)
    extract(config, "a page", "https://example.com/e", ["music"])

    client = run(monkeypatch, config, refuses_json_schema)
    extract(config, "another page", "https://example.com/f", ["music"])

    assert client.formats_tried == ["json_object"]


def test_acceptance_is_remembered_too(monkeypatch, config):
    client = run(monkeypatch, config, accepts_everything)

    extract(config, "a page", "https://example.com/e", ["music"])

    assert client.formats_tried == ["json_schema"]
    config.refresh_from_db()
    assert config.json_schema_supported is True


def test_changing_the_model_retires_what_was_learned(monkeypatch, config):
    """What one model accepts says nothing about the next one."""
    run(monkeypatch, config, refuses_json_schema)
    extract(config, "a page", "https://example.com/e", ["music"])
    assert config.json_schema_support() is False

    config.model = "google/gemini-2.0-flash-001"
    config.save()
    assert config.json_schema_support() is None

    client = run(monkeypatch, config, refuses_json_schema)
    extract(config, "a page", "https://example.com/e", ["music"])
    assert client.formats_tried == ["json_schema", "json_object"]


def test_remembering_does_not_clobber_a_concurrent_admin_edit(monkeypatch, config):
    """The worker holds a stale copy while the model thinks; a full save()
    would write its whole in-memory row back over the admin's change."""
    run(monkeypatch, config, refuses_json_schema)

    # Someone edits the cap in the admin while the extraction is in flight.
    AIConfig.objects.filter(pk=1).update(daily_spend_cap_usd=99)

    extract(config, "a page", "https://example.com/e", ["music"])

    fresh = AIConfig.load()
    assert fresh.daily_spend_cap_usd == 99
    assert fresh.json_schema_supported is False


def test_every_rung_refusing_still_fails_cleanly(monkeypatch, config):
    def refuses_all(response_format):
        return FakeResponse(error={"message": "nope", "code": 400})

    run(monkeypatch, config, refuses_all)

    with pytest.raises(LLMError) as exc:
        extract(config, "a page", "https://example.com/e", ["music"])
    assert "no choices" in str(exc.value)


def test_the_fallback_is_logged_loudly_enough_to_see(monkeypatch, config, caplog):
    """An info line here is how the stall stayed invisible for a whole session."""
    run(monkeypatch, config, refuses_json_schema)

    with caplog.at_level("WARNING", logger="enrichment.llm"):
        extract(config, "a page", "https://example.com/e", ["music"])

    assert any(
        "retrying with a looser one" in r.getMessage() for r in caplog.records
    )
    assert all(r.levelname == "WARNING" for r in caplog.records)
