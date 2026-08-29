# AI Revenue Recovery Platform

An AI-powered payment failure recovery system built with FastAPI, Gemini, and SQLModel.

## Quick Start

```bash
cp .env.example .env
# Fill in GEMINI_API_KEY and DATABASE_URL in .env
pip install -e ".[dev]"
pytest
uvicorn recovery_platform.main:app --reload
```

## Project Structure

```
razorpay-recovery-system/
├── config.yaml              # Baseline recovery policy
├── pyproject.toml           # Dependencies & tooling config
├── .env.example             # Environment variable template
├── recovery_platform/       # Main application package
│   └── config.py            # Unified config loader (Pydantic Settings)
└── tests/
    └── test_config.py       # Phase 1 smoke tests
```