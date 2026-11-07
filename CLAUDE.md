# auditable-regulatory-rag

RAG assistant over FDA regulations and guidance documents.
Answers must cite sources and say "I don't know" when the corpus has no answer.

## Structure
- data/corpus.csv: list of source documents (source of truth)
- data/raw/: downloaded files (not versioned)
- data/sections/: extracted text sections (JSONL)
- eval/: questions, prompts and evaluation scripts
- scripts/: download and extraction scripts

## Rules
- Python 3, no LangChain or LlamaIndex.
- Never put API keys in code: use .env (not versioned).
- Every LLM call must be logged (prompt version, model, tokens, cost).
- Dates in YYYY-MM-DD or YYYY-MM (partial dates allowed).
- Ask before adding a new dependency.
