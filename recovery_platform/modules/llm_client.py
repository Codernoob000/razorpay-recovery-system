"""
recovery_platform/modules/llm_client.py
========================================
Thin, resilient wrapper around the ``google-genai`` SDK.

Responsibilities
----------------
* Instantiate ``google.genai.Client`` once from settings.
* Provide ``generate_structured_json`` which:
  - Sends a prompt to ``gemini-2.5-flash`` with enforced JSON schema output.
  - Retries automatically on transient errors (429 rate-limit, 503 unavailable)
    using exponential back-off via ``tenacity``.
  - Raises the original exception after exhausting retries so callers can
    decide on fallback behaviour.

Usage
-----
    from recovery_platform.modules.llm_client import GeminiClient

    client = GeminiClient()
    result: MyModel = client.generate_structured_json(prompt, MyModel)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from recovery_platform.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Retry predicate – only retry on network / rate-limit / availability errors
# ---------------------------------------------------------------------------

_TRANSIENT_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# google-genai surfaces rate-limit and service errors as google.api_core
# exceptions, but we also need to handle plain Python exceptions for tests.
# We keep the predicate broad and check HTTP status codes where possible.


def _is_transient(exc: BaseException) -> bool:
    """Return True if the exception is a transient error worth retrying."""
    # Plain Python transient errors
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return True
    exc_name = type(exc).__name__
    exc_str  = str(exc).lower()
    # Daily quota limit cannot be resolved by immediate backoff
    if "free_tier_requests" in exc_str or "generaterequestsperday" in exc_str:
        return False
    if any(k in exc_str for k in ("429", "503", "rate limit", "unavailable", "timeout")):
        return True
    if "serviceunavailable" in exc_name.lower():
        return True
    return False


# ---------------------------------------------------------------------------
# GeminiClient
# ---------------------------------------------------------------------------


class GeminiClient:
    """
    Resilient wrapper around ``google.genai.Client``.

    Parameters
    ----------
    api_key:
        Gemini API key.  Defaults to ``get_settings().gemini_api_key``.
    model:
        Gemini model identifier.  Defaults to ``gemini-2.5-flash``.
    max_retries:
        Maximum number of tenacity retry attempts.  Defaults to 3.
    """

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
    ) -> None:
        import google.genai as genai  # imported lazily to ease unit-testing

        self._api_key    = api_key or get_settings().gemini_api_key
        self._model      = model or self.DEFAULT_MODEL
        self._max_retries = max_retries
        self._client     = genai.Client(api_key=self._api_key)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_structured_json(
        self,
        prompt: str,
        response_schema: type[T],
    ) -> T:
        """
        Send *prompt* to Gemini and parse the response into *response_schema*.

        The method instructs the API to return ``application/json`` conforming
        to the Pydantic schema's JSON schema.  Tenacity retries transient
        failures up to ``self._max_retries`` times with exponential back-off.

        Parameters
        ----------
        prompt:
            Full prompt text (system + user instructions combined).
        response_schema:
            A Pydantic ``BaseModel`` subclass that defines the expected output.

        Returns
        -------
        T
            Parsed and validated instance of *response_schema*.

        Raises
        ------
        Exception
            Re-raises the last exception after all retries are exhausted.
        """
        return self._call_with_retry(prompt, response_schema)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_with_retry(self, prompt: str, schema: type[T]) -> T:
        """Build and execute the tenacity-wrapped API call at call time."""

        @retry(
            retry=retry_if_exception(_is_transient),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "Gemini API transient error – retrying (attempt %d/%d): %s",
                rs.attempt_number, self._max_retries, rs.outcome.exception(),
            ),
        )
        def _attempt() -> T:
            return self._do_generate(prompt, schema)

        return _attempt()


    def _do_generate(self, prompt: str, schema: type[T]) -> T:
        """Single (non-retried) API call."""
        from google.genai import types as genai_types

        json_schema = schema.model_json_schema()

        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=json_schema,
            temperature=0.1,   # low temp for deterministic structured output
        )

        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=config,
        )

        raw_text = response.text
        logger.debug("Gemini raw response: %s", raw_text)

        data = json.loads(raw_text)
        return schema.model_validate(data)
