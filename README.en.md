[简体中文](README.md) | **English**

# Corridor: swap the model, keep the controls

A banking AI agent built for the Fintechathon 2026 domestic track. The language model understands the request and proposes an action; a deterministic kernel decides whether it is allowed; a simulated device approves it; the gateway executes it and writes an append-only audit trail. Swap the model and the controls stay the same.

Everything is synthetic: people, accounts, products, transactions and authentication. There is no real money, bank connection or messaging platform behind it.

## What it covers

Transfers, bill analysis, investments, cards, subscriptions and a multi-step birthday plan, in Chinese, through a web demo (the public site https://43-154-247-126.sslip.io/ , scripted planning by default, synthetic data, no availability guarantee) with Streamlit as the local fallback and a simulated IM channel. Multi-step tasks can be interrupted, reconciled when an outcome is unknown, compensated under a fresh authorization, or handed to a simulated human operator.

## Run it

```text
uv sync --frozen --python 3.11
uv run --frozen python -m corridor.web --host 127.0.0.1 --port 8772
uv run --frozen corridor demo --port 8501
```

The first command serves the web front end locally at `http://127.0.0.1:8772/`; the second starts the Streamlit fallback.

Python 3.11 and uv are required; keep the checkout path short on Windows. The first install downloads the dependencies pinned in `uv.lock` and needs network access. For an offline install, point `UV_CACHE_DIR` at an already populated wheel cache and run `uv sync --frozen --offline --python 3.11`. The scripted demo needs no model credentials. Set `CORRIDOR_LOCALE=en` for the English interface.

For the file interface, first create a UTF-8 request with no device approval (preview only):

```powershell
$task = '{"task_id":"DEMO-TRANSFER","task":"给Chen转200元","initial_state":"bank","simulated_device_events":[],"context":[]}'
[System.IO.File]::WriteAllText((Join-Path $PWD 'task.json'), $task, [System.Text.UTF8Encoding]::new($false))
uv run --frozen corridor run-task --file task.json --out result.json
```

## Quick verification

The submitted source archive includes `split.mp4` and `split.webm` under `evidence-min/flagship/`; the public repository does not include these recordings. Their SHA-256 values are listed in `evidence-min/EXTERNAL-ASSETS.sha256`. `verify_all.py` checks their hashes when present and reports `present; hash matches`; when absent, it reports that they are supplied with the submission materials, not in the repository. Neither case is a verification failure.

With dependencies installed, verify the minimal evidence package supplied in the source archive, then run scripted checks. New evidence goes in `evidence/reproduced/`. Disable Ruff's cache so it cannot add unlisted files to `evidence-min/`.

```text
uv run --frozen python -B evidence-min/verify_all.py
uv run --offline --frozen corridor judge --planner scripted --suite all --output evidence/reproduced/judge
uv run --offline --frozen corridor judge --planner scripted --suite wealth --output evidence/reproduced/wealth
uv run --offline --frozen corridor judge --planner scripted --suite zh --output evidence/reproduced/zh
uv run --offline --frozen corridor intent-eval --planner scripted --output evidence/reproduced/zh/intent
uv run --offline --frozen corridor witness --output evidence/reproduced/witness
uv run --offline --frozen ruff check --no-cache .
uv run --offline --frozen ruff format --check --no-cache .
uv run --offline --frozen mypy
uv run --offline --frozen lint-imports
```

The Chinese suite has one disclosed failure, ZH-BD-03, and exits 1 (32/33): the frozen `bank` fixture has no spouse birthday, so the request needs clarification. Replay preserves recorded failures and is neither a new model call nor a safety pass.

## Long regression

The full pytest suite takes longer and is separate from quick verification. The unified test entry also checks rule coverage.

```text
uv run --offline --frozen pytest -p no:cacheprovider
uv run --offline --frozen corridor test --rules-coverage
```

The Chinese README has the full description, the evaluation interface and the safety boundaries.
