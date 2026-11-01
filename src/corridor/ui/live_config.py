"""Live-model deployments and switches for the domestic banking UI (product layer).

Off by default. A real model is reachable only when three conditions hold at once: the
process switch ``CORRIDOR_LIVE=1``, a credential file for the chosen deployment under
``CORRIDOR_LIVE_ENV_DIR`` that is read by path, and the judges' access code. The
``DEPLOYMENTS`` table below is the single place that names a model together with the
price it is budgeted at; a credential file never changes either. Only five names are
ever read from a credential file (``CREDENTIAL_NAMES``): the key is handed to
``LLMClient`` by value and is never displayed, logged, copied or written anywhere.
The seven switches in ``PROCESS_NAMES`` are process environment variables: nothing here
loads a ``.env`` file. Nothing here touches the kernel, the gateway, the synthetic clock
or the audit chain, and nothing reads the host clock.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path

from corridor.agent.llm import LLMConfigurationError

SWITCH = "CORRIDOR_LIVE"
ENV_DIR = "CORRIDOR_LIVE_ENV_DIR"
ACCESS_CODE = "LIVE_ACCESS_CODE"
SESSION_CALLS = "LIVE_SESSION_CALLS"
BUDGET_USD = "LIVE_BUDGET_USD"
BUDGET_CNY = "LIVE_BUDGET_CNY"
JOURNAL_DIR = "LIVE_JOURNAL_DIR"
PROCESS_NAMES = (SWITCH, ENV_DIR, ACCESS_CODE, SESSION_CALLS, BUDGET_USD, BUDGET_CNY, JOURNAL_DIR)
CREDENTIAL_NAMES = frozenset({
    "LLM_API_KEY",
    "LIVE_ACCESS_CODE",
    "LLM_MODEL",
    "LLM_API",
    "LLM_API_BASE",
})
CURRENCIES = ("USD", "CNY")
DEFAULT_SESSION_CALLS = 40
# Cumulative per journal directory (there is no day: the repository reads no host clock).
DEFAULT_BUDGET_USD = Fraction(5)
DEFAULT_BUDGET_CNY = Fraction(30)


@dataclass(frozen=True)
class Deployment:
    """One selectable model: its fixed endpoint and the price the budget guard assumes.

    ``price_in`` / ``price_out`` are per million tokens in ``currency``; they are the
    conservative assumptions the budget guard reserves with, not an invoice.
    ``price_note`` is the catalog key of the sentence saying where the price comes from
    (UI copy lives in the bilingual catalogs, never in Python sources).
    """

    id: str
    label: str
    provider: str
    model: str
    api: str
    api_base: str
    currency: str
    price_in: str
    price_out: str
    price_note: str
    env_file: str
    max_output_tokens: int = 512
    reasoning_effort: str | None = None


DEPLOYMENTS: tuple[Deployment, ...] = (
    Deployment(
        id="openai-gpt-6.1-sol",
        label="OpenAI gpt-6.1-sol",
        provider="openai",
        model="gpt-6.1-sol",
        api="responses",
        api_base="https://api.openai.com/v1",
        currency="USD",
        price_in="10",
        price_out="40",
        price_note="live.price_note.openai-gpt-6.1-sol",
        env_file=".env",
        max_output_tokens=1024,
        reasoning_effort="low",
    ),
    Deployment(
        id="openai-gpt-5.4-mini",
        label="OpenAI gpt-5.4-mini",
        provider="openai",
        model="gpt-5.4-mini",
        api="responses",
        api_base="https://api.openai.com/v1",
        currency="USD",
        price_in="10",
        price_out="40",
        price_note="live.price_note.openai-gpt-5.4-mini",
        env_file=".env",
        max_output_tokens=512,
        reasoning_effort="low",
    ),
    Deployment(
        id="deepseek-flash",
        label="DeepSeek deepseek-flash",
        provider="deepseek",
        model="deepseek-flash",
        api="chat",
        api_base="https://api.deepseek.com/v1",
        currency="CNY",
        price_in="2",
        price_out="8",
        price_note="live.price_note.deepseek-flash",
        env_file=".env.deepseek",
    ),
    Deployment(
        id="zhipu-glm-5.3",
        label="Zhipu glm-5.3",
        provider="zhipu",
        model="glm-5.3",
        api="chat",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        currency="CNY",
        price_in="8",
        price_out="28",
        price_note="live.price_note.zhipu-glm-5.3",
        env_file=".env.zhipu",
    ),
    Deployment(
        id="zhipu-glm-5.3-flash",
        label="Zhipu glm-5.3-flash",
        provider="zhipu",
        model="glm-5.3-flash",
        api="chat",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        currency="CNY",
        price_in="0.8",
        price_out="2.8",
        price_note="live.price_note.zhipu-glm-5.3-flash",
        env_file=".env.zhipu",
    ),
    Deployment(
        id="zhipu-glm-4.7-flash",
        label="Zhipu glm-4.7-flash",
        provider="zhipu",
        model="glm-4.7-flash",
        api="chat",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        currency="CNY",
        price_in="1",
        price_out="1",
        price_note="live.price_note.zhipu-glm-4.7-flash",
        env_file=".env.zhipu",
    ),
)
# Keep the six-deployment experiment/UI baseline intact. The web site may offer more.
SITE_DEPLOYMENTS = DEPLOYMENTS + (
    Deployment(
        id="deepseek-v4-pro",
        label="DeepSeek deepseek-v4-pro",
        provider="deepseek",
        model="deepseek-v4-pro",
        api="chat",
        api_base="https://api.deepseek.com/v1",
        currency="CNY",
        price_in="9",
        price_out="27",
        price_note="live.price_note.deepseek-v4-pro",
        env_file=".env.deepseek",
    ),
)
SITE_DEPLOYMENTS += tuple(
    Deployment(
        id=f"zhipu-{model}",
        label=f"Zhipu {model}",
        provider="zhipu",
        model=model,
        api="chat",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        currency="CNY",
        price_in="12",
        price_out="36",
        price_note="live.price_note.zhipu-site",
        env_file=".env.zhipu",
    )
    for model in ("glm-5.2", "glm-5.1", "glm-5-turbo", "glm-4.7")
)
_BY_ID = {item.id: item for item in SITE_DEPLOYMENTS}


def deployment(deployment_id: str) -> Deployment:
    return _BY_ID[deployment_id]


@dataclass(frozen=True)
class Credential:
    """The five values a credential file may contribute; the secrets never repr."""

    api_key: str = field(repr=False)
    access_code: str = field(default="", repr=False)
    model: str = ""
    api: str = ""
    api_base: str = ""


# C3 (review of S7 round 3): the access code is only ever as strong as its entropy. The
# page keeps a single-use HMAC proof of it, which a short code would let anyone holding the
# proof recover offline, outside the wrong-code locks. A code shorter than this, or with
# too few different characters, makes live access unavailable; the operator generates it
# from a cryptographic random source (for example secrets.token_urlsafe(24)).
MIN_ACCESS_CODE_LENGTH = 20
MIN_ACCESS_CODE_SYMBOLS = 8


def access_code_strong(code: str) -> bool:
    """True when a configured access code is long and varied enough to be used (C3).

    This cannot prove randomness; it refuses the codes a person types from memory.
    """
    return (
        isinstance(code, str)
        and len(code) >= MIN_ACCESS_CODE_LENGTH
        and len(set(code)) >= MIN_ACCESS_CODE_SYMBOLS
    )


def read_credential(path: Path) -> Credential | None:
    """Read one credential file by path; only ``CREDENTIAL_NAMES`` are kept.

    Returns None when the file is missing, unreadable or has no ``LLM_API_KEY``.
    """
    try:
        data = Path(path).read_bytes()
    except (OSError, ValueError):
        return None
    return parse_credential(data)


def parse_credential(data: bytes) -> Credential | None:
    """The credential in one file's bytes (C3: the bytes a grant's fingerprint covers)."""
    try:
        lines = data.decode("utf-8").splitlines()
    except (UnicodeDecodeError, ValueError):
        return None
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() in CREDENTIAL_NAMES:
            values[name.strip()] = value.strip().strip("\"'")
    key = values.get("LLM_API_KEY", "")
    if not key:
        return None
    return Credential(
        api_key=key,
        access_code=values.get("LIVE_ACCESS_CODE", ""),
        model=values.get("LLM_MODEL", ""),
        api=values.get("LLM_API", ""),
        api_base=values.get("LLM_API_BASE", "").rstrip("/"),
    )


def environ_access_code(environ: Mapping[str, str] | None = None) -> str:
    """The access code set in the environment, read when a code is checked and never kept.

    No configuration object, session or snapshot holds it; the caller compares and drops it.
    """
    values: Mapping[str, str] = os.environ if environ is None else environ
    return str(values.get(ACCESS_CODE, "")).strip()


@dataclass(frozen=True)
class LiveConfig:
    """Process-level switches. No access code and no key: those are read where they are
    checked or used, and never kept here (S7 external review 3).

    ``budget_usd`` / ``budget_cny`` are cumulative caps for everything recorded under
    ``journal_dir`` (its spend ledger lives there); a new directory starts a new total.
    """

    enabled: bool = False
    env_dir: Path | None = None
    session_calls: int = DEFAULT_SESSION_CALLS
    budget_usd: Fraction = DEFAULT_BUDGET_USD
    budget_cny: Fraction = DEFAULT_BUDGET_CNY
    journal_dir: Path | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "LiveConfig":
        """Read the process environment; a blank value means "not set".

        With the switch off nothing but the switch is read: no directory, no cap and no
        code, so a blank or broken value can never change the scripted page. With it on,
        an invalid cap is a configuration error (the page says so and stays scripted).
        """
        values: Mapping[str, str] = os.environ if environ is None else environ

        def text(name: str) -> str:
            return str(values.get(name, "")).strip()

        if text(SWITCH) != "1":
            return cls()
        env_dir, journal = text(ENV_DIR), text(JOURNAL_DIR)
        config = cls(
            enabled=True,
            env_dir=Path(env_dir) if env_dir else None,
            journal_dir=Path(journal) if journal else None,
        )
        try:
            calls_text = text(SESSION_CALLS) or str(DEFAULT_SESSION_CALLS)
            if not calls_text.isdigit():
                raise ValueError
            session_calls = int(calls_text)
            budget_usd = Fraction(text(BUDGET_USD) or DEFAULT_BUDGET_USD)
            budget_cny = Fraction(text(BUDGET_CNY) or DEFAULT_BUDGET_CNY)
            if session_calls < 0 or budget_usd < 0 or budget_cny < 0:
                raise ValueError
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            raise LLMConfigurationError("Invalid live hosting configuration") from None
        return replace(
            config, session_calls=session_calls, budget_usd=budget_usd, budget_cny=budget_cny
        )

    def budget_cap(self, currency: str) -> Fraction:
        if currency == "USD":
            return self.budget_usd
        if currency == "CNY":
            return self.budget_cny
        raise ValueError("Unknown live budget currency")

    def credential_path(self, item: Deployment) -> Path | None:
        if self.env_dir is None:
            return None
        return self.env_dir / item.env_file
