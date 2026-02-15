# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-First personal knowledge management system with workflow-driven architecture. Supports intelligent content archiving (webpages, chat records, news) and hybrid search (BM25 + vector).

**Tech Stack**: Python 3.11+, SQLite + FTS5, hnswlib, DeepSeek API, OpenAI Embedding

## Environment Setup

**Required**: Conda with Python 3.11 (avoid Python 3.13 compatibility issues)

```powershell
# Install via Conda (recommended)
.\scripts\setup-conda.ps1
conda activate pkv-py311

# Configure API Keys
notepad .env
# Add: DEEPSEEK_API_KEY, OPENAI_API_KEY

# Verify installation
.\scripts\test-conda.ps1
```

## Common Commands

```bash
# Run all unit tests
python -m pytest tests/unit/ -v

# Run specific test file
python -m pytest tests/unit/test_processors_ai_chat.py -v

# Run integration tests (requires API keys)
python -m pytest tests/integration/ -v

# Run with coverage
python -m pytest tests/unit/ --cov=src --cov-report=term-missing

# Run retrieval tests only
python -m pytest tests/unit/test_*retrieval*.py tests/integration/test_retrieval_integration.py -v

# Verify setup
python src/utils/verify_setup.py
```

## Code Architecture

### Source Structure (`src/`)

- **processors/** - Content processors with BaseProcessor abstraction
  - Registry in `__init__.py` via `get_processor()` factory
  - Processors: WechatProcessor, ZhihuProcessor, ChatProcessor, AIChatProcessor, TextFallbackProcessor, GenericProcessor
- **storage/** - Data persistence layer
  - markdown_store.py: YAML Front Matter management
  - sqlite_store.py: SQLite with FTS5 full-text search (uses `knowledge_id` as primary key)
  - vector_store.py: hnswlib vector indexing with bidirectional ID mapping
- **retrieval/** - Search engine (M4)
  - bm25_retriever.py: FTS5 keyword search with jieba Chinese tokenization
  - vector_retriever.py: Semantic search using hnswlib + OpenAI embeddings
  - hybrid_retriever.py: RRF (Reciprocal Rank Fusion) algorithm for combined results
  - query_router.py: Smart routing based on query token count (<10 tokens → BM25, ≥10 → vector)
- **ai/** - AI service wrappers
  - deepseek_client.py: Summary & tag extraction
  - openai_client.py / embedder.py: Text vectorization

### Key Patterns

1. **Processor Pattern**: All content processors inherit from `BaseProcessor`:
   - `can_handle(url)`: Class method to detect if processor handles this input
   - `process(url)`: Async method to process and return Entry

2. **Entry Dataclass**: Standard return type from processors with title, content, abstract, tags, metadata

3. **Dual Storage**: Markdown (human-readable, primary) + SQLite/hnswlib (index/cache)

4. **Retrieval Strategy**: Query router automatically selects optimal search method:
   - Short queries (<10 tokens): BM25 for precise keyword matching
   - Long queries (≥10 tokens): Vector search for semantic understanding
   - Hybrid mode available via `HybridRetriever` with RRF algorithm (k=60)

5. **Database Schema**: Uses explicit domain-specific column names:
   - `knowledge_id` (not generic `id`) for knowledge_items primary key
   - `tag_id`, `chunk_id`, `timestamp_id` for respective tables
   - All foreign keys reference `knowledge_id`

## Important Conventions

- **No pollution**: Always use virtual environment (.venv/ or Conda)
- **Type hints**: All functions must have type annotations
- **Docstrings**: Public APIs require docstrings
- **Error handling**: Graceful degradation, no bare `except:`
- **Environment**: Project-level config in `.env` and `config/`, not system config
- **Chinese text handling**: Always use `TextProcessor.tokenize_chinese()` for FTS5 queries (manual jieba tokenization + space joining)
- **Column naming**: Use explicit domain names (`knowledge_id`, `tag_id`) not generic `id` - see `docs/issues/SCHEMA_MIGRATION_PLAN.md`

## Current Development Status

**Completed Milestones**:
- M1: Infrastructure (storage, config, SQLite, vectors)
- M2: AI services (DeepSeek, OpenAI, embedding)
- M3: Content processors (WeChat, Zhihu, generic webpage, chat)
- M3.5: AI chat processor & text fallback
- M4: Retrieval engine (BM25, vector, hybrid search)

**In Progress**: M5-M7 (workflow engine, CLI, docs)

## Key Files

- `docs/STARTER_PROMPT.md` - Full development plan
- `docs/personal-knowledge-vault-prd.md` - Core requirements
- `docs/架构设计.md` - Workflow-driven architecture
- `config/config.yaml` - Main configuration

## Testing Fixtures

Test fixtures are in `tests/fixtures/`. Each processor has corresponding tests in `tests/unit/test_processors_*.py`.
