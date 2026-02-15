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

# Run with coverage
python -m pytest tests/unit/ --cov=src --cov-report=term-missing

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
  - sqlite_store.py: SQLite with FTS5 full-text search
  - vector_store.py: hnswlib vector indexing
- **ai/** - AI service wrappers
  - deepseek_client.py: Summary & tag extraction
  - openai_client.py / embedder.py: Text vectorization

### Key Patterns

1. **Processor Pattern**: All content processors inherit from `BaseProcessor`:
   - `can_handle(url)`: Class method to detect if processor handles this input
   - `process(url)`: Async method to process and return Entry

2. **Entry Dataclass**: Standard return type from processors with title, content, abstract, tags, metadata

3. **Dual Storage**: Markdown (human-readable, primary) + SQLite/hnswlib (index/cache)

## Important Conventions

- **No pollution**: Always use virtual environment (.venv/ or Conda)
- **Type hints**: All functions must have type annotations
- **Docstrings**: Public APIs require docstrings
- **Error handling**: Graceful degradation, no bare `except:`
- **Environment**: Project-level config in `.env` and `config/`, not system config

## Current Development Status

**Completed Milestones**:
- M1: Infrastructure (storage, config, SQLite, vectors)
- M2: AI services (DeepSeek, OpenAI, embedding)
- M3: Content processors (WeChat, Zhihu, generic webpage, chat)
- M3.5: AI chat processor & text fallback

**In Progress**: M4-M7 (search engine, workflow, CLI, docs)

## Key Files

- `docs/STARTER_PROMPT.md` - Full development plan
- `docs/personal-knowledge-vault-prd.md` - Core requirements
- `docs/架构设计.md` - Workflow-driven architecture
- `config/config.yaml` - Main configuration

## Testing Fixtures

Test fixtures are in `tests/fixtures/`. Each processor has corresponding tests in `tests/unit/test_processors_*.py`.
