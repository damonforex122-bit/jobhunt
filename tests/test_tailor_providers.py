"""Provider chain: Gemini first, then Claude, then OpenRouter. No network: every client is faked."""
import json

import httpx
import pytest
from pydantic import BaseModel

from jobhunt.config import Settings
from jobhunt.tailor import providers
from jobhunt.tailor.agents import AgentError
from jobhunt.tailor.providers import Chain, Claude, Gemini, OpenRouter, ProviderError


class Out(BaseModel):
    value: str


class Busy(Exception):
    def __init__(self, code=503):
        super().__init__(f"{code} UNAVAILABLE high demand")
        self.code = code


class ReadTimeout(Exception):
    pass


class FakeProvider:
    def __init__(self, name, results):
        self.name = name
        self.results = list(results)  # each item: an Out to return or an exception to raise
        self.calls = 0

    def generate(self, prompt, schema):
        self.calls += 1
        r = self.results.pop(0) if self.results else Out(value=self.name)
        if isinstance(r, Exception):
            raise providers.as_provider_error(r, Settings())
        return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(providers, "_sleep", slept.append)
    return slept


def chain(*ps, switches=None):
    return Chain(list(ps), Settings(), on_switch=(switches.append if switches is not None else None))


def test_first_provider_answers():
    g, c = FakeProvider("Gemini", [Out(value="g")]), FakeProvider("Claude", [])
    assert chain(g, c).generate("p", Out, "writing the resume") == Out(value="g")
    assert c.calls == 0


def test_transient_error_is_retried_once_on_same_provider(no_sleep):
    g = FakeProvider("Gemini", [Busy(), Out(value="g")])
    assert chain(g).generate("p", Out, "reading the posting") == Out(value="g")
    assert g.calls == 2 and len(no_sleep) == 1


def test_busy_twice_falls_back_to_next_provider_and_says_so():
    switches = []
    g, c = FakeProvider("Gemini", [Busy(), Busy(504)]), FakeProvider("Claude", [Out(value="c")])
    assert chain(g, c, switches=switches).generate("p", Out, "reading the posting") == Out(value="c")
    assert switches == ["Gemini is unavailable — trying Claude…"]


def test_non_transient_error_falls_back_without_retry(no_sleep):
    g, c = FakeProvider("Gemini", [ValueError("bad schema")]), FakeProvider("Claude", [Out(value="c")])
    assert chain(g, c).generate("p", Out, "matching").value == "c"
    assert g.calls == 1 and not no_sleep


def test_chain_sticks_with_the_provider_that_worked():
    g = FakeProvider("Gemini", [Busy(), Busy()])
    c = FakeProvider("Claude", [Out(value="c1"), Out(value="c2")])
    ch = chain(g, c)
    ch.generate("p", Out, "reading the posting")
    assert ch.generate("p", Out, "matching").value == "c2"
    assert g.calls == 2  # not asked again for the second step


def test_all_providers_fail_names_step_and_each_reason():
    g = FakeProvider("Gemini", [ReadTimeout("read timed out"), ReadTimeout("read timed out")])
    c = FakeProvider("Claude", [ValueError("nope")])
    with pytest.raises(AgentError) as e:
        chain(g, c).generate("p", Out, "writing the resume")
    msg = str(e.value)
    assert msg.startswith("Writing the resume failed")
    assert f"Gemini: no answer within {providers.CALL_TIMEOUT_S}s" in msg and "Claude: ValueError: nope" in msg


def test_error_messages_mask_every_key():
    s = Settings(gemini_api_key="g-secret", anthropic_api_key="a-secret", openrouter_api_key="o-secret")
    err = providers.as_provider_error(ValueError("keys g-secret a-secret o-secret"), s)
    assert "secret" not in str(err) and str(err).count("***") == 3


def test_build_chain_uses_only_configured_providers_in_order(monkeypatch):
    monkeypatch.setattr(providers, "Gemini", lambda s: ("Gemini", s))
    monkeypatch.setattr(providers, "Claude", lambda s: ("Claude", s))
    monkeypatch.setattr(providers, "OpenRouter", lambda s: ("OpenRouter", s))
    names = lambda s: [p[0] for p in providers.build_chain(s).providers]  # noqa: E731
    assert providers.build_chain(Settings()) is None
    assert names(Settings(gemini_api_key="g", openrouter_api_key="o")) == ["Gemini", "OpenRouter"]
    assert names(Settings(gemini_api_key="g", anthropic_api_key="a", openrouter_api_key="o")) == [
        "Gemini", "Claude", "OpenRouter"]
    assert providers.has_ai_key(Settings(anthropic_api_key="a"))
    assert not providers.has_ai_key(Settings())


# --- the three real adapters, with faked clients ------------------------------

def test_gemini_adapter_sets_time_limit_and_parses(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
            self.models = self

        def generate_content(self, **kw):
            seen["call"] = kw
            return type("R", (), {"parsed": Out(value="g")})()

    import google.genai
    monkeypatch.setattr(google.genai, "Client", FakeClient)
    g = Gemini(Settings(gemini_api_key="k", gemini_model="m1"))
    assert g.generate("p", Out) == Out(value="g")
    assert seen["http_options"].timeout == providers.CALL_TIMEOUT_S * 1000
    assert seen["call"]["model"] == "m1" and seen["call"]["config"]["response_schema"] is Out


def test_claude_adapter_uses_structured_output(monkeypatch):
    seen = {}

    class FakeMessages:
        def parse(self, **kw):
            seen.update(kw)
            return type("M", (), {"stop_reason": "end_turn", "parsed_output": Out(value="c")})()

    class FakeAnthropic:
        def __init__(self, **kw):
            seen["client"] = kw
            self.messages = FakeMessages()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    c = Claude(Settings(anthropic_api_key="a"))
    assert c.generate("prompt", Out) == Out(value="c")
    assert seen["model"] == "claude-opus-5-5" and seen["output_format"] is Out
    assert seen["client"]["timeout"] == providers.CLAUDE_TIMEOUT_S and seen["client"]["max_retries"] == 0
    assert seen["messages"] == [{"role": "user", "content": "prompt"}]


def test_claude_refusal_is_an_error(monkeypatch):
    class FakeAnthropic:
        def __init__(self, **kw):
            self.messages = self

        def parse(self, **kw):
            return type("M", (), {"stop_reason": "refusal", "parsed_output": None})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    with pytest.raises(ProviderError) as e:
        Claude(Settings(anthropic_api_key="a")).generate("p", Out)
    assert not e.value.transient and "declined" in str(e.value)


def _openrouter(handler):
    s = Settings(openrouter_api_key="o-key", openrouter_model="vendor/model")
    return OpenRouter(s, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_openrouter_adapter_posts_json_schema_and_parses():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value": "o"}'}}]})

    assert _openrouter(handler).generate("p", Out) == Out(value="o")
    assert seen["auth"] == "Bearer o-key" and seen["body"]["model"] == "vendor/model"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["body"]["response_format"]["json_schema"]["schema"] == Out.model_json_schema()


def test_openrouter_503_is_transient_and_bad_json_is_not():
    with pytest.raises(ProviderError) as e:
        _openrouter(lambda req: httpx.Response(503, json={"error": {"message": "busy"}})).generate("p", Out)
    assert e.value.transient
    bad = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})  # noqa: E731
    with pytest.raises(ProviderError) as e:
        _openrouter(bad).generate("p", Out)
    assert not e.value.transient
