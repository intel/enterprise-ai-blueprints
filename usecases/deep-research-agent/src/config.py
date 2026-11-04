# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # LLM
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "meta-llama/Llama-3.1-70B-Instruct-AWQ"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_top_p: float = 0.8
    llm_top_k: int = 20
    llm_min_p: float = 0.0
    llm_presence_penalty: float = 1.5
    llm_repetition_penalty: float = 1.0

    # Secondary LLM (optional, for lighter tasks)
    secondary_llm_base_url: str = ""
    secondary_llm_api_key: str = "not-needed"
    secondary_llm_model: str = ""
    secondary_llm_modules: str = ""
    # Per-slot temperature override for the secondary model. -1 = inherit the
    # primary `llm_temperature`. Set to 0 when the secondary is served with
    # speculative decoding: vLLM SD on CPU only produces valid constrained output
    # at temperature 0 (temp>0 garbles JSON-schema decoding — see tasks/ SD notes).
    secondary_llm_temperature: float = -1.0

    # Hardware labels (used in UI telemetry)
    llm_hardware: str = "GPU"
    secondary_llm_hardware: str = "Xeon"

    # Tavily
    tavily_api_key: str = Field(default="", description="Tavily search API key")
    tavily_search_depth: str = "advanced"
    tavily_max_results: int = 5

    # Serper (optional second search provider)
    serper_api_key: str = ""

    # Research pipeline
    max_sub_questions: int = 5
    max_iterations: int = 2
    min_sources_for_report: int = 3
    relevance_threshold: float = 0.4
    # Max search→evaluate→gap-analysis cycles. 1 = a single pass (no analyze_gaps /
    # no second web_search); set to 1 for a fast, deterministic demo.
    max_research_cycles: int = 1

    # Webpage summarization — compress each source before it reaches
    # evaluate_sources / write_report, cutting the LLM (Bedrock) token cost.
    # use_llm_summarizer overrides the extractive path when True.
    use_llm_summarizer: bool = False
    use_extractive_summarizer: bool = True
    extract_budget_map: int = 800  # Budget per source, counted in NLTK words (~2.5 LLM tokens/word),
                                    # so ~800 words ≈ ~2000 LLM tokens/source
    extract_selection_method: str = "centroid"  # centroid | mmr | lexrank | cosine_similarity_centrality | sentence_centrality
    extract_embedding_model: str = "all-MiniLM-L6-v2"
    extract_embedding_device: str = "cpu"  # embeddings run on the Xeon CPU
    extract_chunk_max_chars: int = 2000  # max chars per heading chunk before sub-split (chunk_summarizer)
    extract_query_weight: float = 0.5  # query-sim vs. page-centroid-sim blend (0..1, chunk_summarizer)

    # Report
    report_max_tokens: int = 8192

    # Agent
    agent_max_iterations: int = 15
    agent_max_tokens: int = 1024

    # Server
    host: str = "0.0.0.0"
    port: int = 8081

    # Auth
    api_keys: str = Field(
        default="",
        description="Comma-separated list of valid API keys. Empty = no auth required.",
    )

    # Database
    database_url: str = "sqlite+aiosqlite:///deep_research.db"

    # Search result cache
    search_cache_ttl_hours: int = Field(default=24, description="TTL for cached search results (hours)")
    search_cache_max_entries: int = Field(default=500, description="Max in-memory search cache entries")


settings = Settings()
