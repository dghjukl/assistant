"""
Unit tests for provider adapter message-format helpers and no-key guards.

These tests cover pure Python logic only — no HTTP calls are made.

Covers:
- GeminiAdapter._convert_messages: system → system_instruction, assistant → model role
- GeminiAdapter._convert_messages: inserts empty user turn when conversation starts with model
- GeminiAdapter._extract_content: extracts joined text parts
- AnthropicAdapter.complete: returns no_api_key when api_key is empty
- OpenAIAdapter.complete: returns no_api_key when api_key is empty
- GeminiAdapter.complete: returns no_api_key when api_key is empty
- HuggingFaceAdapter.complete: returns no_api_key when api_key is empty
- OpenRouterAdapter.complete: returns no_api_key when api_key is empty
- All adapters expose correct provider_id strings
- All adapters expose ProviderCapabilities with expected tier values
- All adapters expose with_model() that returns a new instance of the same class
- LocalAdapter capabilities: is_local=True, cost_tier=4
"""
from __future__ import annotations

import pytest

from runtime.providers.adapters.anthropic   import AnthropicAdapter
from runtime.providers.adapters.gemini      import GeminiAdapter, _convert_messages, _extract_content
from runtime.providers.adapters.huggingface import HuggingFaceAdapter
from runtime.providers.adapters.local       import LocalAdapter
from runtime.providers.adapters.openai      import OpenAIAdapter
from runtime.providers.adapters.openrouter  import OpenRouterAdapter


# ── Gemini message format helpers ─────────────────────────────────────────────


class TestGeminiConvertMessages:
    def test_system_extracted_as_instruction(self):
        msgs = [
            {"role": "system",    "content": "You are helpful."},
            {"role": "user",      "content": "Hello"},
        ]
        sys_instr, contents = _convert_messages(msgs)
        assert sys_instr == "You are helpful."
        assert len(contents) == 1
        assert contents[0]["role"] == "user"

    def test_assistant_mapped_to_model(self):
        msgs = [
            {"role": "user",      "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
        _, contents = _convert_messages(msgs)
        roles = [c["role"] for c in contents]
        assert "model" in roles
        assert "assistant" not in roles

    def test_user_role_preserved(self):
        msgs = [{"role": "user", "content": "Hi"}]
        _, contents = _convert_messages(msgs)
        assert contents[0]["role"] == "user"

    def test_text_wrapped_in_parts(self):
        msgs = [{"role": "user", "content": "Hello world"}]
        _, contents = _convert_messages(msgs)
        assert contents[0]["parts"] == [{"text": "Hello world"}]

    def test_inserts_empty_user_turn_when_starts_with_model(self):
        msgs = [{"role": "assistant", "content": "Starting assistant turn"}]
        _, contents = _convert_messages(msgs)
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"] == [{"text": ""}]

    def test_no_system_message_gives_empty_instruction(self):
        msgs = [{"role": "user", "content": "Hi"}]
        sys_instr, _ = _convert_messages(msgs)
        assert sys_instr == ""

    def test_empty_input_inserts_user_turn(self):
        _, contents = _convert_messages([])
        assert contents[0]["role"] == "user"


class TestGeminiExtractContent:
    def test_extracts_text_parts(self):
        data = {
            "candidates": [
                {"content": {"parts": [{"text": "Hello"}, {"text": " world"}]}}
            ]
        }
        assert _extract_content(data) == "Hello world"

    def test_ignores_non_text_parts(self):
        data = {
            "candidates": [
                {"content": {"parts": [{"inline_data": "..."}, {"text": "text"}]}}
            ]
        }
        assert _extract_content(data) == "text"

    def test_empty_candidates_returns_empty(self):
        assert _extract_content({"candidates": []}) == ""

    def test_malformed_data_returns_empty(self):
        assert _extract_content({}) == ""
        assert _extract_content({"candidates": [{}]}) == ""


# ── No-key guard: all remote adapters must return error, not raise ─────────────


class TestNoApiKeyGuard:
    @pytest.mark.parametrize("adapter", [
        HuggingFaceAdapter(),
        OpenAIAdapter(),
        AnthropicAdapter(),
        GeminiAdapter(),
        OpenRouterAdapter(),
    ])
    def test_empty_key_returns_no_api_key_result(self, adapter):
        result = adapter.complete(
            [{"role": "user", "content": "hi"}],
            api_key="",
        )
        assert not result.ok
        assert result.error_code == "no_api_key"
        assert result.provider == adapter.provider_id

    @pytest.mark.parametrize("adapter", [
        HuggingFaceAdapter(),
        OpenAIAdapter(),
        AnthropicAdapter(),
        GeminiAdapter(),
        OpenRouterAdapter(),
    ])
    def test_none_key_returns_no_api_key_result(self, adapter):
        # Passing None (coerced to falsy) should also fail gracefully
        result = adapter.complete(
            [{"role": "user", "content": "hi"}],
            api_key=None,  # type: ignore[arg-type]
        )
        assert not result.ok
        # Either no_api_key or some other graceful error — must never raise
        assert result.error_code


# ── Provider IDs ──────────────────────────────────────────────────────────────


class TestProviderIds:
    def test_all_provider_ids(self):
        expected = {
            HuggingFaceAdapter(): "huggingface",
            OpenAIAdapter():      "openai",
            AnthropicAdapter():   "anthropic",
            GeminiAdapter():      "gemini",
            OpenRouterAdapter():  "openrouter",
            LocalAdapter():       "local",
        }
        for adapter, pid in expected.items():
            assert adapter.provider_id == pid


# ── Capabilities ──────────────────────────────────────────────────────────────


class TestAdapterCapabilities:
    def test_local_is_local_and_free(self):
        caps = LocalAdapter().capabilities
        assert caps.is_local is True
        assert caps.cost_tier == 4          # free

    def test_local_is_external_false_on_result(self):
        # LocalAdapter.complete can't be tested without an HTTP server,
        # but we can verify capabilities is_local.
        caps = LocalAdapter().capabilities
        assert caps.is_local is True

    def test_huggingface_quality_and_cost(self):
        caps = HuggingFaceAdapter().capabilities
        assert caps.quality_tier == 3      # budget
        assert caps.cost_tier == 3         # cheap

    def test_openai_quality_tier_premium(self):
        caps = OpenAIAdapter().capabilities
        assert caps.quality_tier == 1

    def test_anthropic_quality_tier_premium(self):
        caps = AnthropicAdapter().capabilities
        assert caps.quality_tier == 1

    def test_gemini_quality_tier_premium_cost_cheap(self):
        caps = GeminiAdapter().capabilities
        assert caps.quality_tier == 1
        assert caps.cost_tier == 3

    def test_openrouter_default_model_is_free_tier(self):
        caps = OpenRouterAdapter().capabilities
        assert ":free" in caps.default_model  # free tier model by default


# ── with_model() ──────────────────────────────────────────────────────────────


class TestWithModel:
    @pytest.mark.parametrize("adapter_cls,model", [
        (HuggingFaceAdapter, "mistralai/Mistral-7B-Instruct-v0.2"),
        (OpenAIAdapter,      "gpt-4o"),
        (AnthropicAdapter,   "claude-sonnet-4-6"),
        (GeminiAdapter,      "gemini-1.5-pro"),
        (OpenRouterAdapter,  "deepseek/deepseek-r1:free"),
    ])
    def test_with_model_returns_new_instance_with_model(self, adapter_cls, model):
        original = adapter_cls()
        new_adapter = original.with_model(model)
        assert new_adapter is not original
        assert isinstance(new_adapter, adapter_cls)
        assert new_adapter.model_id == model

    def test_with_model_preserves_timeout(self):
        adapter = OpenAIAdapter(timeout_sec=45.0)
        new_adapter = adapter.with_model("gpt-4o")
        assert new_adapter.timeout_sec == 45.0
