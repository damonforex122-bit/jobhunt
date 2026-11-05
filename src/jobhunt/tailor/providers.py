"""LLM providers for the tailoring agents, tried in order: Gemini, then Claude, then OpenRouter.

Each provider turns (prompt, pydantic schema) into a validated schema instance or raises
ProviderError. Chain retries a transient failure once on the same provider, then moves on to the
next one, and keeps using whichever provider last worked for the rest of the run.
"""
import time
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from ..config import Settings

CALL_TIMEOUT_S = 90  # Gemini and OpenRouter: one call; without a limit a stalled reply hangs forever
CLAUDE_TIMEOUT_S = 180  # Claude Opus thinks before answering, so it gets longer
RETRIES = 1  # extra attempts on the same provider after a transient failure
RETRY_DELAY_S = 3
MAX_TOKENS = 16000
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_TRANSIENT_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_sleep = time.sleep


class AgentError(Exception):
    """A tailoring step could not get a valid answer from any provider."""


class ProviderError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def has_ai_key(settings: Settings) -> bool:
    return bool(settings.gemini_api_key or settings.anthropic_api_key or settings.openrouter_api_key)


def _mask(text: str, settings: Settings) -> str:
    for key in (settings.gemini_api_key, settings.anthropic_api_key, settings.openrouter_api_key):
        if key:
            text = text.replace(key, "***")
    return text


def _status(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        if isinstance(getattr(exc, attr, None), int):
            return getattr(exc, attr)
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _is_timeout(exc: Exception) -> bool:
    return any("Timeout" in t.__name__ for t in type(exc).__mro__)


def as_provider_error(exc: Exception, settings: Settings) -> ProviderError:
    """Classify any SDK/HTTP failure; the message is short, single-line and has keys masked."""
    if isinstance(exc, ProviderError):
        return exc
    if _is_timeout(exc):
        timeout = CLAUDE_TIMEOUT_S if type(exc).__module__.startswith("anthropic") else CALL_TIMEOUT_S
        return ProviderError(f"no answer within {timeout}s", transient=True)
    transient = _status(exc) in _TRANSIENT_CODES or any(
        "Connection" in t.__name__ for t in type(exc).__mro__)
    msg = " ".join(_mask(f"{type(exc).__name__}: {exc}", settings).split())[:200]
    return ProviderError(msg, transient=transient)


class Gemini:
    name = "Gemini"

    def __init__(self, settings: Settings, client=None):
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=settings.gemini_api_key,
                                  http_options=types.HttpOptions(timeout=CALL_TIMEOUT_S * 1000))
        self.client, self.settings = client, settings

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model, contents=prompt,
                config={"response_mime_type": "application/json", "response_schema": schema,
                        "temperature": 0.2})
            parsed = response.parsed
        except Exception as exc:
            raise as_provider_error(exc, self.settings) from exc
        if not isinstance(parsed, schema):
            raise ProviderError(f"returned no valid {schema.__name__}")
        return parsed


class Claude:
    name = "Claude"

    def __init__(self, settings: Settings, client=None):
        if client is None:
            import anthropic

            # Retries happen in Chain, so the SDK's own retries are off.
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                         timeout=CLAUDE_TIMEOUT_S, max_retries=0)
        self.client, self.settings = client, settings

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        try:
            response = self.client.messages.parse(
                model=self.settings.claude_model, max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}], output_format=schema)
        except Exception as exc:
            raise as_provider_error(exc, self.settings) from exc
        if response.stop_reason == "refusal":
            raise ProviderError("declined the request")
        if not isinstance(response.parsed_output, schema):
            raise ProviderError(f"returned no valid {schema.__name__} (stop: {response.stop_reason})")
        return response.parsed_output


class OpenRouter:
    name = "OpenRouter"

    def __init__(self, settings: Settings, http=None):
        import httpx

        self.http = http or httpx.Client(timeout=CALL_TIMEOUT_S)
        self.settings = settings

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        body = {
            "model": self.settings.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema()}},
        }
        try:
            r = self.http.post(OPENROUTER_URL, json=body,
                               headers={"Authorization": f"Bearer {self.settings.openrouter_api_key}"})
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise as_provider_error(exc, self.settings) from exc
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise ProviderError(f"returned no valid {schema.__name__}") from exc


class Chain:
    def __init__(self, providers: list, settings: Settings,
                 on_switch: Callable[[str], None] | None = None):
        self.providers, self.settings = providers, settings
        self.on_switch = on_switch or (lambda _msg: None)
        self._preferred = 0  # the provider that last answered goes first next time

    def generate(self, prompt: str, schema: type[BaseModel], step: str) -> BaseModel:
        order = self.providers[self._preferred:] + self.providers[:self._preferred]
        failures = []
        for i, provider in enumerate(order):
            if i:
                self.on_switch(f"{order[i - 1].name} is unavailable — trying {provider.name}…")
            for attempt in range(RETRIES + 1):
                try:
                    result = provider.generate(prompt, schema)
                    self._preferred = self.providers.index(provider)
                    return result
                except ProviderError as err:
                    if err.transient and attempt < RETRIES:
                        _sleep(RETRY_DELAY_S)
                        continue
                    failures.append(f"{provider.name}: {err}")
                    break
        raise AgentError(f"{step.capitalize()} failed — " + "; ".join(failures))


def build_chain(settings: Settings, on_switch: Callable[[str], None] | None = None) -> Chain | None:
    """Every provider that has a key, in fallback order; None when none is configured."""
    providers = []
    if settings.gemini_api_key:
        providers.append(Gemini(settings))
    if settings.anthropic_api_key:
        providers.append(Claude(settings))
    if settings.openrouter_api_key:
        providers.append(OpenRouter(settings))
    return Chain(providers, settings, on_switch) if providers else None
