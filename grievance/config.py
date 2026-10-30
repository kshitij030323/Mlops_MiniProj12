"""Runtime configuration. Everything environment-specific comes from env vars (D4).

No credentials, endpoints or model identifiers have defaults here; see .env.example.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set (see .env.example)")
    return val


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str = field(default_factory=lambda: os.getenv("SERVICE_NAME", "grievance"))
    redis_url: str = field(default_factory=lambda: _req("REDIS_URL"))
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "/data"))
    index_dir: str = field(default_factory=lambda: os.getenv("INDEX_DIR", "/data/index"))
    corpus_dir: str = field(default_factory=lambda: os.getenv("CORPUS_DIR", "/corpus/raw"))
    embedding_model: str = field(default_factory=lambda: _req("EMBEDDING_MODEL"))
    chunking: str = field(default_factory=lambda: os.getenv("CHUNKING_STRATEGY", "clause"))

    # LLM provider: any OpenAI-compatible chat completions endpoint (real provider or the stub)
    llm_base_url: str = field(default_factory=lambda: _req("LLM_BASE_URL"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _req("LLM_MODEL"))
    # Optional provider hint (e.g. "none" for Gemini thinking models); omitted from the request when empty
    llm_reasoning_effort: str = field(default_factory=lambda: os.getenv("LLM_REASONING_EFFORT", ""))
    llm_timeout_s: float = field(default_factory=lambda: _f("LLM_TIMEOUT_S", 20))
    llm_max_retries: int = field(default_factory=lambda: _i("LLM_MAX_RETRIES", 2))
    llm_backoff_base_s: float = field(default_factory=lambda: _f("LLM_BACKOFF_BASE_S", 0.5))
    breaker_fail_threshold: int = field(default_factory=lambda: _i("BREAKER_FAIL_THRESHOLD", 3))
    breaker_reset_s: float = field(default_factory=lambda: _f("BREAKER_RESET_S", 30))
    price_prompt_per_1k: float = field(default_factory=lambda: _f("PRICE_PROMPT_PER_1K_USD", 0.0))
    price_completion_per_1k: float = field(default_factory=lambda: _f("PRICE_COMPLETION_PER_1K_USD", 0.0))

    # Agent loop bounds (A1)
    max_steps: int = field(default_factory=lambda: _i("AGENT_MAX_STEPS", 30))
    max_tool_calls: int = field(default_factory=lambda: _i("AGENT_MAX_TOOL_CALLS", 10))
    max_wall_clock_s: float = field(default_factory=lambda: _f("AGENT_MAX_WALL_CLOCK_S", 90))
    max_verify_attempts: int = field(default_factory=lambda: _i("AGENT_MAX_VERIFY_ATTEMPTS", 3))
    tool_timeout_s: float = field(default_factory=lambda: _f("TOOL_TIMEOUT_S", 5))
    retrieval_k: int = field(default_factory=lambda: _i("RETRIEVAL_K", 4))

    verifier_url: str = field(default_factory=lambda: _req("VERIFIER_URL"))
    verifier_timeout_s: float = field(default_factory=lambda: _f("VERIFIER_TIMEOUT_S", 15))

    prompt_variant: str = field(default_factory=lambda: os.getenv("PROMPT_VARIANT", "v2"))
    worker_concurrency: int = field(default_factory=lambda: _i("WORKER_CONCURRENCY", 4))
    run_cache_ttl_s: int = field(default_factory=lambda: _i("RUN_CACHE_TTL_S", 3600))
    retrieval_cache_ttl_s: int = field(default_factory=lambda: _i("RETRIEVAL_CACHE_TTL_S", 3600))
    rate_limit_per_min: int = field(default_factory=lambda: _i("RATE_LIMIT_PER_MIN", 30))
    run_deadline_s: float = field(default_factory=lambda: _f("RUN_DEADLINE_S", 180))
    live_eval_sample_rate: float = field(default_factory=lambda: _f("LIVE_EVAL_SAMPLE_RATE", 0.2))
    demo_faults_enabled: bool = field(default_factory=lambda: _b("DEMO_FAULTS_ENABLED", False))
    otel_endpoint: str = field(default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""))


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
