"""R2-11 transport probes; only synthetic development requests, never formal runs."""

import os
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from time import gmtime, strftime
from urllib.parse import urlsplit

from corridor.agent.llm import LLMClient
from corridor.bench.recording import network_disabled, replay_client
from corridor.crypto.jcs import H, jcs_bytes
from corridor.schemas.base import loads_exact

LOCK = Path("local-models/models-lock.json")
REQUIRED_LOCAL_CONTEXT = 32768
PROVIDER_BASES = {
    "openai": {"https://api.openai.com/v1"},
    "deepseek": {"https://api.deepseek.com", "https://api.deepseek.com/v1"},
    "zhipu": {"https://open.bigmodel.cn/api/paas/v4"},
    "dashscope": {"https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "qwen-plus": {"https://dashscope.aliyuncs.com/compatible-mode/v1"},
}
OFFICIAL_PRICE_URLS = {
    "deepseek": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
    "zhipu": "https://docs.bigmodel.cn/cn/guide/start/pricing",
}
OFFICIAL_ADAPTER_URLS = {
    "deepseek": [
        "https://api-docs.deepseek.com/zh-cn/guides/thinking_mode",
        "https://api-docs.deepseek.com/zh-cn/api/create-chat-completion",
    ],
    "zhipu": [
        "https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode",
        "https://docs.bigmodel.cn/cn/guide/capabilities/function-calling",
    ],
}


def domestic_worker_environment(environment=None):
    """Bind domestic transports to their explicit file, excluding inherited vendors."""
    return {
        key: value
        for key, value in (os.environ if environment is None else environment).items()
        if not key.upper().startswith(("OPENAI_", "LLM_"))
        and not key.upper().endswith(("_API_KEY", "_API_TOKEN", "_AUTH_TOKEN"))
    }


@contextmanager
def domestic_environment():
    """Isolate the synchronous probe, restoring the caller environment afterwards."""
    original = dict(os.environ)
    clean = domestic_worker_environment(original)
    removed = {key: value for key, value in original.items() if key not in clean}
    for key in removed:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def formal_probe_request():
    """Capture one existing development prompt before any synthetic bank dispatch."""
    from corridor.bench.blind_zh import load_cases, run_case

    class Captured(BaseException):
        pass

    class CaptureClient(LLMClient):
        def complete(self, **kwargs):
            self.captured = {
                key: deepcopy(value) for key, value in kwargs.items() if key != "scripted"
            }
            raise Captured

    client = CaptureClient(mode="scripted", config="B_prompt", env_file=None)
    cases = load_cases(Path(__file__).resolve().parents[3] / "tests/fixtures/blind-zh-sample")
    try:
        run_case(cases[0], config="B_prompt", client=client, variant="legit_twin")
    except Captured:
        return client.captured
    raise ValueError("formal_probe_development_prompt_unavailable")


def _formal_call(client):
    return client.complete(**formal_probe_request(), scripted=lambda: {})


@dataclass(frozen=True)
class ProviderCredentials:
    """A secret bound to one explicitly declared provider and exact endpoint."""

    provider: str
    api_base: str
    model: str
    api_key: str = field(repr=False)


def _credential_values(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    values = {}
    allowed = {"LLM_PROVIDER", "LLM_API_BASE", "LLM_MODEL", "LLM_API_KEY", "OPENAI_API_KEY"}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ValueError("credential_file_unreadable") from None
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator and key in allowed:
            if key in values:
                raise ValueError("credential_duplicate_field")
            values[key] = value.strip().strip("\"'")
    return values


def load_provider_credentials(
    path: Path | None, *, provider: str, api_base: str, model: str | None = None
) -> ProviderCredentials | None:
    """Fail closed before requests; never consult inherited process credentials."""
    values = _credential_values(path)
    if not values:
        return None
    key = values.get("LLM_API_KEY") or (
        values.get("OPENAI_API_KEY", "") if provider == "openai" else ""
    )
    if not key:
        return None
    if not values.get("LLM_PROVIDER") or not values.get("LLM_API_BASE"):
        raise ValueError("credential_binding_missing")
    if values["LLM_PROVIDER"] != provider:
        raise ValueError("credential_provider_mismatch")
    if values["LLM_API_BASE"] != api_base:
        raise ValueError("credential_api_base_mismatch")
    if api_base not in PROVIDER_BASES.get(provider, set()):
        raise ValueError("provider_api_base_forbidden")
    declared_model = values.get("LLM_MODEL", "")
    if model is not None and provider in {"deepseek", "zhipu"} and not declared_model:
        raise ValueError("credential_model_missing")
    if model is not None and declared_model and model != declared_model:
        raise ValueError("credential_model_mismatch")
    if declared_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", declared_model):
        raise ValueError("credential_model_invalid")
    if key in declared_model or key in api_base or key in provider:
        raise ValueError("credential_binding_invalid")
    return ProviderCredentials(provider, api_base, declared_model or model or "", key)


def dotenv_credential(path: Path) -> str:
    """Legacy file reader; new paid transports must use load_provider_credentials."""
    if not path.is_file():
        return ""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key in {"LLM_API_KEY", "OPENAI_API_KEY"}:
            values[key] = value.strip().strip("\"'")
    return values.get("LLM_API_KEY") or values.get("OPENAI_API_KEY", "")


class _PriceTables(HTMLParser):
    """Expand HTML table spans so model names stay bound to their price columns."""

    def __init__(self):
        super().__init__()
        self.tables = []
        self.rows = None
        self.row = None
        self.cell = None
        self.spans = {}
        self.row_number = 0
        self.column = 0
        self.span = (1, 1)

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows, self.spans, self.row_number = [], {}, 0
        elif tag == "tr" and self.rows is not None:
            self.row, self.column = {}, 0
            for (row, column), text in self.spans.items():
                if row == self.row_number:
                    self.row[column] = text
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            attributes = dict(attrs)
            self.span = (int(attributes.get("rowspan", "1")), int(attributes.get("colspan", "1")))
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            text = re.sub(r"\s+", " ", "".join(self.cell)).strip()
            while self.column in self.row:
                self.column += 1
            for row_offset in range(self.span[0]):
                for column_offset in range(self.span[1]):
                    column = self.column + column_offset
                    if row_offset == 0:
                        self.row[column] = text
                    else:
                        self.spans[self.row_number + row_offset, column] = text
            self.column += self.span[1]
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append([
                self.row.get(index, "") for index in range(max(self.row, default=-1) + 1)
            ])
            self.row_number += 1
            self.row = None
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows)
            self.rows = None


def _exact_price(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*元?\s*", value)
    if match is None:
        raise ValueError("official_price_format_unrecognized")
    return Fraction(match.group(1))


def _model_label(value):
    return re.sub(r"\(\d+\)$", "", value).strip().casefold()


def _extract_official_prices(provider, model, tables):
    rates = []
    excerpts = []
    for table in tables:
        if not table:
            continue
        if provider == "deepseek":
            column = next(
                (
                    index
                    for index, value in enumerate(table[0])
                    if _model_label(value) == model.casefold()
                ),
                None,
            )
            if column is None:
                continue
            inputs, outputs = [], []
            for row in table[1:]:
                if len(row) <= column:
                    continue
                label = " ".join(row[:column])
                if "百万" in label and "缓存未命中" in label:
                    inputs.append(_exact_price(row[column]))
                    excerpts.append(" | ".join(row))
                elif "百万" in label and "输出" in label:
                    outputs.append(_exact_price(row[column]))
                    excerpts.append(" | ".join(row))
            if inputs and outputs:
                rates.append((max(inputs), max(outputs)))
                excerpts.insert(0, " | ".join(table[0]))
        else:
            previous_count = len(rates)
            header = table[0]
            input_column = next(
                (
                    i
                    for i, value in enumerate(header)
                    if "输入单价" in value and "百万" in value and "元" in value
                ),
                None,
            )
            output_column = next(
                (
                    i
                    for i, value in enumerate(header)
                    if "输出单价" in value and "百万" in value and "元" in value
                ),
                None,
            )
            if input_column is None or output_column is None:
                continue
            for row in table[1:]:
                if (
                    row
                    and _model_label(row[0]) == model.casefold()
                    and len(row) > max(input_column, output_column)
                ):
                    rates.append((
                        _exact_price(row[input_column]),
                        _exact_price(row[output_column]),
                    ))
                    excerpts.append(" | ".join(row))
            if len(rates) > previous_count:
                excerpts.insert(0, " | ".join(header))
    if not rates:
        raise ValueError("official_model_price_unavailable")
    return {
        "price_in_per_million": str(max(pair[0] for pair in rates)),
        "price_out_per_million": str(max(pair[1] for pair in rates)),
        "published_rate_pairs": [
            {
                "input": f"{pair[0].numerator}/{pair[0].denominator}",
                "output": f"{pair[1].numerator}/{pair[1].denominator}",
            }
            for pair in rates
        ],
        "excerpt": "\n".join(excerpts),
    }


def freeze_official_prices(provider, model, *, output_dir):
    """Fetch unauthenticated official bytes and reject absent or ambiguous model prices."""
    import httpx

    url = OFFICIAL_PRICE_URLS[provider]
    evidence = {"url": url, "model": model, "status": "unavailable"}
    try:
        with httpx.Client(trust_env=False, follow_redirects=True, timeout=20) as client:
            response = client.get(url)
            response.raise_for_status()
        if urlsplit(str(response.url)).hostname != urlsplit(url).hostname:
            raise ValueError("official_price_redirect_forbidden")
        raw = response.content
        digest = sha256(raw).hexdigest()
        path = Path(output_dir) / provider / f"{digest}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        # Retrieval time is provenance metadata only; it never enters a model prompt.
        evidence.update(
            page_sha256=digest,
            fetched_at_utc=strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()),
            raw_page=str(path),
            final_url=str(response.url),
        )
        parser = _PriceTables()
        parser.feed(raw.decode("utf-8"))
        evidence["excerpt"] = "\n".join(
            " | ".join(row) for table in parser.tables[:1] for row in table
        )[:2400]
        extracted = _extract_official_prices(provider, model, parser.tables)
        for key in ("price_in_per_million", "price_out_per_million"):
            value = Fraction(extracted[key])
            extracted[key] = f"{value.numerator}/{value.denominator}"
        evidence.update(
            extracted,
            status="frozen",
            currency="CNY",
            rate_selection="maximum_published_model_tier_for_budget_estimation; uncached_input",
        )
    except Exception as exc:
        evidence["failure_reason"] = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("official_")
            else type(exc).__name__
        )
    return evidence


def freeze_adapter_documents(provider, *, output_dir):
    """Archive the exact unauthenticated official adapter references and their hashes."""
    import httpx

    pages = []
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=20) as client:
        for url in OFFICIAL_ADAPTER_URLS[provider]:
            response = client.get(url)
            response.raise_for_status()
            if urlsplit(str(response.url)).hostname != urlsplit(url).hostname:
                raise ValueError("official_adapter_redirect_forbidden")
            raw = response.content
            digest = sha256(raw).hexdigest()
            path = Path(output_dir) / provider / f"{digest}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            pages.append({
                "url": url,
                "final_url": str(response.url),
                "page_sha256": digest,
                "fetched_at_utc": strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()),
                "raw_page": str(path),
            })
    return pages


def _redact_provider_message(value, credential):
    text = str(value)[:4096]
    if credential:
        text = text.replace(credential, "[REDACTED]")
        # Vendor messages sometimes echo only a prefix/suffix or one key component.
        fragments = {credential[index : index + 4] for index in range(max(0, len(credential) - 3))}
        for fragment in sorted(fragments):
            text = text.replace(fragment, "[REDACTED]")
    return re.sub(r"(?i)(?:bearer\s+|sk-)[A-Za-z0-9_.-]+", "[REDACTED]", text)


@contextmanager
def _capture_provider_error(api_base, credential, captured):
    """Observe the existing LLM transport without issuing an extra paid request."""
    import httpx

    original = httpx.HTTPTransport.handle_request

    def send(transport, request):
        response = original(transport, request)
        if str(request.url) == api_base + "/chat/completions" and response.status_code >= 400:
            try:
                error = loads_exact(response.read()).get("error", {})
                if isinstance(error, dict):
                    captured.update(http_status=response.status_code)
                    for key in ("code", "message", "type"):
                        if isinstance(error.get(key), (str, int)):
                            captured[key] = _redact_provider_message(error[key], credential)
            except (ValueError, TypeError, AttributeError):
                captured.update(http_status=response.status_code)
        return response

    httpx.HTTPTransport.handle_request = send
    try:
        yield
    finally:
        httpx.HTTPTransport.handle_request = original


def classify_parameters(body: dict, *, api: str) -> dict:
    reasoning = body.get("reasoning", {}).get("effort", body.get("reasoning_effort"))
    return {
        "seed": {
            "status": "supported" if "seed" in body else "unsupported",
            "sent": "seed" in body,
            "value": body.get("seed"),
            "reason": "wire field observed"
            if "seed" in body
            else "Responses has no seed field; independent repeats"
            if api == "responses"
            else "No seed observed; support not established",
        },
        "reasoning_effort": {
            "status": "supported" if reasoning is not None else "unsupported",
            "sent": reasoning is not None,
            "value": reasoning,
            "reason": "wire field observed; acceptance checked separately",
        },
        "temperature": {
            "status": "supported" if "temperature" in body else "unsupported",
            "sent": "temperature" in body,
            "value": body.get("temperature", "provider-default"),
            "reason": "Default sampling intentionally omits an explicit temperature override",
        },
        "context": {
            "status": "unsupported",
            "sent": False,
            "value": "deployment setting",
            "reason": "OpenAI-compatible APIs expose no per-request context-window control; "
            "observed deployment context is recorded in identity, not claimed as sent",
        },
    }


def local_deployments(lock_path: Path = LOCK) -> list[dict]:
    """Read-only verification of pinned manifests and the running server digest."""
    import httpx

    lock = loads_exact(lock_path.read_bytes())
    with httpx.Client(trust_env=False, timeout=5) as http:
        tags = http.get("http://127.0.0.1:11434/api/tags")
        tags.raise_for_status()
        version = http.get("http://127.0.0.1:11434/api/version")
        version.raise_for_status()
    listed = {row["name"]: row for row in loads_exact(tags.content)["models"]}
    engine = loads_exact(version.content)["version"]
    if engine != "0.40.5":
        raise ValueError("Locked engine version differs")
    profiles = []
    start_script = lock_path.parent / "start.ps1"
    settings = start_script.read_text(encoding="utf-8")
    configured_context = re.search(r"OLLAMA_CONTEXT_LENGTH\s*=\s*'(\d+)'", settings)
    if configured_context is None:
        raise ValueError("Cannot freeze the local server context setting")
    for entry in lock:
        manifest = Path(entry["manifest_path"])
        if sha256(manifest.read_bytes()).hexdigest() != entry["manifest_sha256"]:
            raise ValueError("Local model manifest differs from lock")
        actual = listed.get(entry["model"], {})
        if actual.get("digest") != entry["registry_manifest_sha256"]:
            raise ValueError("Serving model digest differs from lock")
        profiles.append({
            "model": entry["model"],
            "engine": engine,
            "weights_digest": entry["registry_manifest_sha256"],
            "manifest_sha256": entry["manifest_sha256"],
            "context_window": int(configured_context.group(1)),
            "model_max_context": actual.get("details", {}).get("context_length", "unknown"),
            "server_settings_hash": sha256(start_script.read_bytes()).hexdigest(),
            "context_control": "unsupported_per_request",
            "temperature": "provider-default",
            "reasoning_effort": "none",
            "max_output_tokens": 512,
            "tool_budget": 1,
            "lock_hash": sha256(lock_path.read_bytes()).hexdigest(),
        })
    return profiles


def _call(client: LLMClient):
    return client.complete(
        task_id="D3-DEVELOPMENT-PROBE",
        step="request-parameters",
        prompt={
            "instruction": 'Reply with exactly this JSON object: {"handle":"PROBE_OK"}.',
        },
        tool_state={"handle": "PROBE_OK"},
        scripted=lambda: {"handle": "PROBE_OK"},
    )


def _domestic_probe(provider, *, document, output, env_file, tool_choice_mode):
    previous = document["providers"].get(provider)
    error = None
    bound = None
    pricing = None
    row = {
        "model": None,
        "status": "not_run",
        "parameters": classify_parameters({}, api="chat"),
        "actual_request": None,
        "actual_outbound_calls": 0,
        "strict_replay": False,
    }
    try:
        values = _credential_values(Path(env_file))
        bound = load_provider_credentials(
            Path(env_file), provider=provider, api_base=values.get("LLM_API_BASE", "")
        )
        if bound is None:
            error = "missing_credentials"
        elif not bound.model:
            error = "credential_model_missing"
        else:
            row["model"] = bound.model
    except ValueError as exc:
        error = str(exc)
    if (
        bound
        and previous
        and previous.get("gate_passed")
        and previous.get("api_base") == bound.api_base
        and any(
            item.get("model") == bound.model
            and item.get("formal_shape")
            and item.get("deployment_profile", {}).get("thinking") == "disabled"
            and item.get("deployment_profile", {}).get("tool_choice_mode") == tool_choice_mode
            for item in previous.get("models", [])
        )
    ):
        return previous
    if previous:
        document.setdefault("retained_attempts", {}).setdefault(provider, []).append(previous)
    paid_attempts = sum(
        item.get("actual_outbound_calls", 0)
        for prior in document.get("retained_attempts", {}).get(provider, [])
        for item in prior.get("models", [])
    )
    reservations = document.setdefault("paid_call_reservations", {})
    reserved = max(paid_attempts, reservations.get(provider, 0))
    if reserved >= 3:
        error = "paid_probe_three_call_limit"
    if bound and not error:
        pricing = freeze_official_prices(
            provider, bound.model, output_dir=output.parent / "pricing"
        )
        if pricing["status"] != "frozen":
            error = pricing["failure_reason"]
    if bound:
        profile = {
            "model": bound.model,
            "engine": f"{provider} OpenAI-compatible chat",
            "context_window": "provider-managed",
            "temperature": "provider-default",
            "reasoning_effort": None,
            "max_output_tokens": 512,
            "tool_budget": 1,
            "thinking": "disabled",
            "tool_choice_mode": tool_choice_mode,
        }
        row["deployment_profile"] = profile
    if error:
        row["failure_reason"] = error
    elif bound and pricing:
        captured, provider_error = [], {}
        try:
            row["adapter_documents"] = freeze_adapter_documents(
                provider, output_dir=output.parent / "adapter-docs"
            )
        except Exception as exc:
            row["adapter_documents"] = []
            row["adapter_document_error"] = type(exc).__name__
        client = LLMClient(
            mode="record",
            model=bound.model,
            config="D3-probe",
            replay_dir=output.parent / "replay" / provider / str(reserved),
            api="chat",
            provider=provider,
            api_base=bound.api_base,
            api_key=bound.api_key,
            max_calls=1,
            max_usd="15",
            env_file=None,
            assumed_price_in=pricing["price_in_per_million"],
            assumed_price_out=pricing["price_out_per_million"],
            reasoning_effort=None,
            seed=None,
            max_output_tokens=512,
            deployment_profile=profile,
            request_timeout_seconds=120,
        )
        client.transport_observers.append(captured.append)
        # Reserve durably before transport so a crash cannot buy another fourth request.
        reservations[provider] = reserved + 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(jcs_bytes(document))
        try:
            with _capture_provider_error(bound.api_base, bound.api_key, provider_error):
                result = _formal_call(client)
            row["status"] = "valid_completed"
            with network_disabled():
                replay = _formal_call(replay_client(client))
            row["strict_replay"] = (
                replay.source == "replay"
                and replay.output == result.output
                and isinstance(result.output, dict)
                and {"kind", "target_role", "assistant_text", "fact_refs"} == result.output.keys()
                and replay.replay_key == result.replay_key
            )
            if not row["strict_replay"]:
                row["status"] = "probe_verification_failed"
                row["failure_reason"] = "strict_probe_replay_mismatch"
        except Exception as exc:
            row["status"] = "runtime_error"
            row["failure_reason"] = type(exc).__name__
        if provider_error:
            row["provider_error"] = provider_error
        if captured:
            body = captured[0]
            request = formal_probe_request()
            row["formal_shape"] = {
                "case_id": request["task_id"],
                "config": "B_prompt",
                "source": "tests/fixtures/blind-zh-sample",
                "prompt_hash": H(request["prompt"]),
                "tool_state_hash": H(request["tool_state"]),
                "request_bytes": len(jcs_bytes(body)),
                "single_emit_result_tool": len(body.get("tools", [])) == 1
                and body["tools"][0]["function"]["name"] == "emit_result",
                "parallel_tool_calls": body.get("parallel_tool_calls"),
            }
            row["parameters"] = classify_parameters(body, api="chat")
            row["actual_request"] = {
                key: value
                for key, value in body.items()
                if key not in {"messages", "input", "tools"}
            }
            row["actual_request"]["content_hash"] = H(body)
        if client.calls:
            row["request_identity"] = {
                key: value for key, value in client.calls[0].items() if key != "cost_usd"
            }
        row["actual_outbound_calls"] = len(captured)
        row["returned_model"] = client.calls[0].get("returned_model") if client.calls else None
        row["cost_cny"] = str(client.spent_usd)
        row["cost_basis"] = (
            "provider token usage times frozen conservative uncached input/output rates; "
            "budget estimate, not a provider invoice"
        )
        trace = client.calls[0] if client.calls else {}
        shape = trace.get("response_shape", {})
        row["formal_gate_checks"] = {
            "single_tool_call": shape.get("tool_calls") == 1,
            "finish_reason": shape.get("finish_reason") == "tool_calls"
            or (tool_choice_mode == "auto" and shape.get("tool_calls") == 1),
            "output_tokens_at_most_300": 0 < trace.get("output_tokens", 0) <= 300,
            "reasoning_tokens_zero": trace.get("reasoning_tokens") == 0,
            "strict_replay": row["strict_replay"],
            "official_adapter_documents": len(row.get("adapter_documents", [])) == 2,
            "thinking_disabled": bool(captured)
            and captured[0].get("thinking") == {"type": "disabled"},
            "parallel_tool_calls_disabled": bool(captured)
            and captured[0].get("parallel_tool_calls") is False,
            "single_emit_result_tool": row.get("formal_shape", {}).get(
                "single_emit_result_tool", False
            ),
        }
    result = {
        "provider": provider,
        "api_base": bound.api_base if bound else None,
        "gate_passed": row["status"] == "valid_completed"
        and row["strict_replay"]
        and bool(row.get("formal_shape"))
        and all(row.get("formal_gate_checks", {"missing": False}).values())
        and bool(pricing and pricing["status"] == "frozen"),
        "models": [row],
        "failure_reason": error or row.get("failure_reason"),
        "pricing_currency": "CNY" if pricing and pricing["status"] == "frozen" else None,
        "pricing_evidence": pricing,
        "tool_choice_support": {
            "mode": tool_choice_mode,
            "observed_wire_accepted": row["status"] == "valid_completed",
            "documentation": (
                "Official documentation describes auto only; this model/endpoint accepted "
                "the explicitly named emit_result function with thinking disabled."
                if provider == "zhipu" and tool_choice_mode == "forced_function"
                else "Official API documentation and the recorded wire probe are retained."
            ),
        },
        "interpretation": (
            "Wire acceptance does not prove seed support, determinism or engine internals. "
            "Prices conservatively use the maximum published tier for uncached input/output."
        ),
    }
    if pricing and pricing["status"] == "frozen":
        result.update({
            key: pricing[key] for key in ("price_in_per_million", "price_out_per_million")
        })
    document["providers"][provider] = result
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(jcs_bytes(document))
    return result


def probe_requests(
    provider: str,
    *,
    output=Path("evidence/probe/requests.json"),
    env_file=Path(".env"),
    lock_path=LOCK,
    tool_choice_mode="forced_function",
) -> dict:
    if provider not in {"openai", "local", "deepseek", "zhipu"}:
        raise ValueError("Provider must be openai, local, deepseek or zhipu")
    output = Path(output)
    document = (
        loads_exact(output.read_bytes())
        if output.exists()
        else {
            "schema_version": "request-probe-v1",
            "formal_live_experiments": False,
            "providers": {},
        }
    )
    if provider in {"deepseek", "zhipu"}:
        if tool_choice_mode not in {"forced_function", "auto"}:
            raise ValueError("Unsupported domestic tool choice mode")
        with domestic_environment():
            return _domestic_probe(
                provider,
                document=document,
                output=output,
                env_file=env_file,
                tool_choice_mode=tool_choice_mode,
            )
    if provider in document["providers"]:
        previous = document["providers"][provider]
        document.setdefault("retained_attempts", {}).setdefault(provider, []).append(previous)
    rows = []
    error = None
    credential = "ollama"
    if provider == "openai":
        try:
            bound = load_provider_credentials(
                Path(env_file), provider="openai", api_base="https://api.openai.com/v1"
            )
            credential = bound.api_key if bound else ""
        except ValueError as exc:
            credential, error = "", str(exc)
    try:
        profiles = (
            local_deployments(Path(lock_path))
            if provider == "local"
            else [
                {
                    "model": "gpt-6.1-sol",
                    "engine": "OpenAI Responses",
                    "context_window": "provider-managed",
                    "temperature": "provider-default",
                    "reasoning_effort": "low",
                    "max_output_tokens": 128,
                    "tool_budget": 1,
                }
            ]
        )
    except Exception as exc:
        profiles, error = [], type(exc).__name__
    if not credential and not error:
        error = "missing_credentials"
    paid_attempts = sum(
        row.get("actual_outbound_calls", 0)
        for prior in document.get("retained_attempts", {}).get("openai", [])
        for row in prior.get("models", [])
    )
    if provider == "openai" and paid_attempts >= 3:
        error = "paid_probe_three_call_limit"
    for index, profile in enumerate(profiles):
        previous_rows = document.get("providers", {}).get(provider, {}).get("models", [])
        completed = next(
            (
                row
                for row in previous_rows
                if row["model"] == profile["model"]
                and row["status"] == "valid_completed"
                and row.get("deployment_profile") == profile
            ),
            None,
        )
        if completed:
            rows.append(completed)
            continue
        api = "chat" if provider == "local" else "responses"
        row = {
            "model": profile["model"],
            "deployment_profile": profile,
            "status": "not_run",
            "parameters": classify_parameters({}, api=api),
            "actual_request": None,
            "strict_replay": False,
        }
        rows.append(row)
        if error:
            row["failure_reason"] = error
            continue
        captured = []
        client = LLMClient(
            mode="record",
            model=profile["model"],
            config="D3-probe",
            replay_dir=output.parent / "replay" / provider / str(index),
            api=api,
            provider="ollama" if provider == "local" else "openai",
            api_base="http://127.0.0.1:11434/v1"
            if provider == "local"
            else "https://api.openai.com/v1",
            api_key=credential,
            max_calls=1,
            max_usd="1",
            env_file=None,
            assumed_price_in="10",
            assumed_price_out="40",
            reasoning_effort=profile["reasoning_effort"],
            seed=1 if provider == "local" else None,
            max_output_tokens=profile["max_output_tokens"],
            deployment_profile=profile,
            request_timeout_seconds=120 if provider == "local" else 20,
        )
        client.transport_observers.append(captured.append)
        try:
            result = _call(client)
            row["status"] = "valid_completed"
            with network_disabled():
                replay = _call(replay_client(client))
            row["strict_replay"] = replay.output == result.output
        except Exception as exc:
            # Do not persist exception messages, credential-bearing request objects or headers.
            row["status"] = "runtime_error"
            row["failure_reason"] = type(exc).__name__
        if captured:
            body = captured[0]
            row["parameters"] = classify_parameters(body, api=api)
            row["actual_request"] = {
                key: value
                for key, value in body.items()
                if key not in {"messages", "input", "tools"}
            }
            row["actual_request"]["content_hash"] = H(body)
        if client.calls:
            row["request_identity"] = {
                key: value for key, value in client.calls[0].items() if key not in {"cost_usd"}
            }
        row["actual_outbound_calls"] = len(captured)
        row["returned_model"] = client.calls[0].get("returned_model") if client.calls else None
        if provider == "local" and captured:
            import httpx

            try:
                with httpx.Client(trust_env=False, timeout=5) as http:
                    response = http.get("http://127.0.0.1:11434/api/ps")
                    response.raise_for_status()
                active = next(
                    (
                        item
                        for item in loads_exact(response.content)["models"]
                        if item["name"] == profile["model"]
                    ),
                    {},
                )
                row["observed_context_window"] = active.get("context_length")
                row["context_identity_matches_engine"] = (
                    row["observed_context_window"] == profile["context_window"]
                )
            except Exception as exc:
                row["context_identity_matches_engine"] = False
                row["failure_reason"] = type(exc).__name__
    result = {
        "gate_passed": bool(rows)
        and all(
            row["status"] == "valid_completed"
            and row["strict_replay"]
            and (provider != "local" or row.get("context_identity_matches_engine", False))
            for row in rows
        ),
        "models": rows,
        "failure_reason": error,
        "interpretation": "Wire acceptance does not prove determinism or engine internals.",
    }
    document["providers"][provider] = result
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(jcs_bytes(document))
    return result
