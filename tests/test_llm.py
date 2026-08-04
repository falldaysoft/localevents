"""The OpenRouter client: the response_format ladder, the retry, and the salvage.

Everything here drives `llm.extract` against a stand-in for the OpenAI client.
The pipeline that decides *whether* to call it lives in `test_enrichment.py`,
which patches `llm.extract` wholesale and needs no fake client at all — keeping
the two apart is what stops a second fake growing next to this one and drifting
out of step with the real call.

The ladder exists because OpenRouter fronts models whose `json_schema` support
varies. When a model refuses it, the refusal arrives as HTTP 200 with an error
body and no choices — and it is slow: one measured refusal took 203 seconds.
Re-asking on every extraction turned a one-minute wait into a four-minute one,
which is what the remembering tests exist to prevent.
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
    """Either a real reply, or the choice-less 200 that means 'unsupported'.

    Note what a refusal actually looks like: a 200 with no `choices` and an
    `error` object, not an HTTP error. That is what made the first live call
    fail with 'NoneType is not subscriptable'.
    """

    def __init__(self, content=None, error=None):
        self.choices = [FakeChoice(content)] if content is not None else None
        self.error = error
        self.usage = FakeUsage()


class FakeClient:
    """Records every call it is given."""

    def __init__(self, responder):
        self.calls = []
        self.chat = self
        self.completions = self
        self._responder = responder

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responder(kwargs.get("response_format"))

    @property
    def formats_tried(self):
        formats = [c.get("response_format") for c in self.calls]
        return [
            (f or {}).get("type") if isinstance(f, dict) else None for f in formats
        ]

    @property
    def messages_sent(self):
        return [[dict(m) for m in c["messages"]] for c in self.calls]


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


# --- responders -------------------------------------------------------------


def refuses_json_schema(response_format):
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        return FakeResponse(error={"message": "Schema is too complex.", "code": 400})
    return FakeResponse(content=GOOD_JSON)


def accepts_everything(response_format):
    return FakeResponse(content=GOOD_JSON)


def in_turn(*replies):
    """Answer with each reply in order, whatever format was asked for.

    A reply is either a string, returned as the message content, or a
    FakeResponse for the cases that need a refusal partway through.
    """
    queue = list(replies)

    def responder(response_format):
        reply = queue.pop(0)
        return reply if isinstance(reply, FakeResponse) else FakeResponse(content=reply)

    return responder


# --- the ladder -------------------------------------------------------------


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


def test_every_rung_refusing_fails_with_the_providers_own_words(monkeypatch, config):
    """Running out of rungs must leave a message someone can act on.

    The first live call crashed with 'NoneType is not subscriptable', which
    said nothing about the actual cause.
    """
    def refuses_all(response_format):
        return FakeResponse(error={"message": "model is overloaded", "code": 400})

    run(monkeypatch, config, refuses_all)

    with pytest.raises(LLMError) as exc:
        extract(config, "a page", "https://example.com/e", ["music"])

    assert "no choices" in str(exc.value)
    assert "overloaded" in str(exc.value)


def test_the_fallback_is_logged_loudly_enough_to_see(monkeypatch, config, caplog):
    """An info line here is how the stall stayed invisible for a whole session."""
    run(monkeypatch, config, refuses_json_schema)

    with caplog.at_level("WARNING", logger="enrichment.llm"):
        extract(config, "a page", "https://example.com/e", ["music"])

    assert any(
        "retrying with a looser one" in r.getMessage() for r in caplog.records
    )
    assert all(r.levelname == "WARNING" for r in caplog.records)


def test_the_schema_is_always_in_the_prompt(monkeypatch, config):
    """The weakest rung sends no response_format at all, so the prompt carries
    the contract."""
    client = run(monkeypatch, config, accepts_everything)

    extract(config, "a page", "https://example.com/e", ["music"])

    user_message = client.messages_sent[0][-1]["content"]
    assert "notes_for_submitter" in user_message
    assert "occurrences" in user_message


# --- what comes back --------------------------------------------------------


def test_json_wrapped_in_prose_or_fences_is_salvaged(monkeypatch, config):
    """Cheap models wrap JSON in fences despite being told not to.

    Discarding an otherwise-correct extraction over formatting would be the
    wrong trade, so the reply is salvaged rather than rejected.
    """
    run(
        monkeypatch,
        config,
        in_turn('Sure! Here you go:\n```json\n{"title": "Craft Fair"}\n```'),
    )

    draft, _ = extract(config, "a page", "https://example.com/e", [])
    assert draft.title == "Craft Fair"


def test_a_reply_that_fails_validation_is_retried_once_with_the_error(
    monkeypatch, config
):
    """OpenRouter forwards response_format to the upstream model and not all of
    them honour it — which is exactly the models you would use OpenRouter to
    reach. The second attempt shows the model what was wrong with the first."""
    client = run(
        monkeypatch,
        config,
        in_turn(
            '{"summary": "no title field here"}',  # fails validation
            '{"title": "Craft Fair"}',             # corrected
        ),
    )

    draft, _ = extract(config, "a page", "https://example.com/e", [])

    assert draft.title == "Craft Fair"
    assert len(client.calls) == 2, "expected exactly one retry"
    # The retry must actually tell the model what was wrong.
    assert any(
        "did not match the schema" in m["content"] for m in client.messages_sent[1]
    )


def test_two_unusable_replies_is_where_it_stops(monkeypatch, config):
    run(monkeypatch, config, in_turn("nonsense", "still nonsense"))

    with pytest.raises(LLMError):
        extract(config, "a page", "https://example.com/e", [])


# --- configuration ----------------------------------------------------------


def test_no_api_key_is_a_clear_error_not_a_crash(config):
    config.api_key = ""
    config.save()

    with pytest.raises(LLMError, match="No API key"):
        extract(config, "a page", "https://example.com/e", [])


def test_the_suite_never_sees_a_real_api_key(settings):
    """Guard on test isolation, not on product behaviour.

    settings.py loads .env so local development works without exporting
    anything. The side effect is that on a developer's machine these tests
    would otherwise reach a real endpoint — billable, slow enough to look like
    a hang, and dependent on someone else's uptime. This caught exactly that
    once already.
    """
    assert settings.OPENROUTER_API_KEY == ""
