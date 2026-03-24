"""
EOS — Provider Adapters
========================
Each adapter implements BaseProvider for one remote or local backend.

Available adapters
------------------
  LocalAdapter       — local llama-server (OpenAI-compatible REST)
  HuggingFaceAdapter — HuggingFace Inference API
  OpenAIAdapter      — OpenAI Chat Completions API
  AnthropicAdapter   — Anthropic Messages API
  GeminiAdapter      — Google Gemini GenerateContent API
  OpenRouterAdapter  — OpenRouter (open-weight model gateway)
"""
from runtime.providers.adapters.local        import LocalAdapter
from runtime.providers.adapters.huggingface  import HuggingFaceAdapter
from runtime.providers.adapters.openai       import OpenAIAdapter
from runtime.providers.adapters.anthropic    import AnthropicAdapter
from runtime.providers.adapters.gemini       import GeminiAdapter
from runtime.providers.adapters.openrouter   import OpenRouterAdapter

__all__ = [
    "LocalAdapter",
    "HuggingFaceAdapter",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OpenRouterAdapter",
]
