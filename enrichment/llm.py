"""Reading an event out of a page with a language model.

One backend: OpenRouter, which fronts every model worth using including
Anthropic's. A direct Anthropic client was removed once it was clear it only
duplicated what OpenRouter already reaches, at the cost of a second code path
that would never be exercised.

The consequence is worth stating plainly, because it shapes everything below.
Anthropic's own API enforces structured output server-side, so a reply that
parses is guaranteed to match the schema. OpenRouter forwards `response_format`
to whichever model is behind it, and that model may ignore it — observed live,
with `claude-sonnet-5` returning no choices at all for a strict schema request.
So this module assumes nothing: it degrades through progressively weaker format
requests, keeps the schema in the prompt regardless, and validates every reply
itself with one corrective retry.
"""

import json
import logging
import re

from django.conf import settings
from pydantic import ValidationError

from .schemas import EventDraft

logger = logging.getLogger("enrichment.llm")

MAX_PAGE_CHARS = 24_000

# The SDK default is ten minutes, which would block a worker while a submitter
# watches a spinner. Note this is a *read* timeout, not a wall-clock budget: a
# slowly streaming reply can still exceed it, as one measured 116s run did.
REQUEST_TIMEOUT = 90.0

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    """Extraction failed. The message is for logs, not for the submitter."""


SYSTEM_PROMPT = """\
You read a web page and extract the single event it is advertising, for a \
community events listing.

Rules:
- Report only what the page states. Never infer a date, price, or venue that \
is not there. An empty field is fine and expected; a plausible guess is not, \
because a moderator will trust it.
- If the page lists several dates for the same event, return them all as \
occurrences and set is_series when they follow a repeating pattern.
- If the page is about several different events, extract the one it is \
primarily about.
- Times are local to the venue. Use ISO 8601 without a timezone offset unless \
the page states one.
- Include the year exactly as the page gives it. If the page shows a date with \
no year, use the next occurrence of that date in the future — never a past one.
- Use category slugs only from the list provided. If none fit, return none.
- Put anything ambiguous in notes_for_submitter, addressed to the person who \
submitted the link.
"""


def _user_prompt(text, source_url, category_slugs):
    return (
        f"Source URL: {source_url}\n"
        f"Available category slugs: {', '.join(category_slugs)}\n\n"
        f"Page content:\n{text[:MAX_PAGE_CHARS]}"
    )


def _extract_json(raw):
    """Pull a JSON object out of a reply that may be wrapped in prose or fences.

    Cheaper models routinely wrap JSON in ```json fences or add a sentence of
    preamble despite being told not to. Salvaging that is much better than
    discarding an otherwise-correct extraction over formatting.
    """
    raw = raw.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except ValueError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except ValueError:
            pass

    raise LLMError("Reply contained no usable JSON.")


def _error_detail(response):
    """Dig the provider's own explanation out of a choice-less response.

    OpenRouter reports an unsupported parameter in the response body rather
    than as an HTTP error, so `choices` is absent instead of empty. Reading the
    reason out beats the TypeError you get from indexing a missing list, which
    is what the first live run produced.
    """
    error = getattr(response, "error", None)
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error:
        return str(error)

    for attr in ("model_dump_json", "to_json"):
        dump = getattr(response, attr, None)
        if callable(dump):
            try:
                return dump()[:400]
            except Exception:
                pass
    return repr(response)[:400]


def _usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }


def extract(config, text, source_url, category_slugs):
    """Extract an EventDraft from page text. Returns (draft, usage_dict)."""
    if not config.resolved_api_key():
        raise LLMError(
            "No API key configured. Set one in the admin under AI "
            "configuration, or as OPENROUTER_API_KEY in the environment."
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError(
            "The openai package is not installed; run pip install -r requirements.txt."
        ) from exc

    client = OpenAI(
        api_key=config.resolved_api_key(),
        base_url=config.base_url or DEFAULT_BASE_URL,
        timeout=REQUEST_TIMEOUT,
        max_retries=1,
    )

    schema = EventDraft.model_json_schema()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _user_prompt(text, source_url, category_slugs)
            + "\n\nReply with a single JSON object matching this schema. "
            "No prose, no code fences.\n\n"
            + json.dumps(schema),
        },
    ]

    # Ask for the strongest guarantee first and fall back. The schema is in the
    # prompt at every rung and the reply is validated regardless, so the
    # weakest still yields a correct draft or a clean failure.
    formats = [
        {"type": "json_schema",
         "json_schema": {"name": "event_draft", "strict": False, "schema": schema}},
        {"type": "json_object"},
        None,
    ]
    format_index = 0
    last_error = None
    usage = {}

    # Two validation attempts. The second shows the model exactly what was
    # wrong with the first, which recovers most cheap-model failures.
    for attempt in range(2):
        while True:
            kwargs = {
                "model": config.model,
                "max_tokens": config.max_tokens,
                "messages": messages,
                "extra_headers": {
                    "HTTP-Referer": f"https://{settings.ALLOWED_HOSTS[0]}",
                    "X-Title": settings.SITE_NAME,
                },
            }
            if formats[format_index] is not None:
                kwargs["response_format"] = formats[format_index]

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc:  # openai raises a family of transport errors
                raise LLMError(f"Request failed: {exc}") from exc

            if getattr(response, "choices", None):
                break

            detail = _error_detail(response)
            if format_index + 1 < len(formats):
                logger.info(
                    "%s rejected response_format %s (%s); retrying with a looser one",
                    config.model, format_index, detail,
                )
                format_index += 1
                continue
            raise LLMError(f"{config.model} returned no choices: {detail}")

        usage = _usage(response)
        content = response.choices[0].message.content or ""

        try:
            return EventDraft.model_validate(_extract_json(content)), usage
        except (LLMError, ValidationError) as exc:
            last_error = exc
            logger.info(
                "attempt %s did not match the schema: %s", attempt + 1, exc
            )
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That did not match the schema: {exc}\n"
                        "Reply again with only the corrected JSON object."
                    ),
                }
            )

    raise LLMError(f"Model did not return valid JSON after two attempts: {last_error}")
