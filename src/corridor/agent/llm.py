"""The only model entry point: offline by default, explicitly budgeted when live."""

import os
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from corridor.crypto.jcs import H, jcs_bytes
from corridor.gateway.privacy import PIIVault
from corridor.schemas.base import loads_exact
from corridor.schemas.models import JsonValue

SCHEMA_VERSION = "bank-llm-v1"
_DEFAULT_API_BASE = "https://api.openai.com/v1"
_SDK_LOCK = RLock()
_MODES = {"scripted", "strict", "fallback", "live", "record"}
_ENV_FIELDS = {
    "LLM_API_KEY",
    "LLM_API",
    "LLM_API_BASE",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_MODEL_CHEAP",
    "LLM_MODEL_WEAK",
    "LLM_REASONING_EFFORT",
    "LLM_MAX_CALLS",
    "LLM_MAX_USD",
    "LLM_ASSUMED_PRICE_IN",
    "LLM_ASSUMED_PRICE_OUT",
}
_RESPONSE_INPUT_PADDING = 1024
_USAGE_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
RESPONSE_DIAGNOSTIC_FIELDS = frozenset({
    "finish_reason",
    "response_status",
    "incomplete_details",
    "output_tokens",
    "output_length",
    "output_sha256",
    "output_json_valid",
    "output_candidates",
    "output_truncated",
    "rejected_candidates",
    "validation_errors",
})
_WEALTH_TOOL_NAMES = {
    "capture_goal",
    "start_risk_profile",
    "compare_products",
    "explain_fact",
    "why_not",
    "propose_allocation",
    "adjust",
    "execute_plan",
    "handoff_to_advisor",
}
_AGENTDOJO_TOOL_FIELDS = {
    "get_iban": {},
    "send_money": {
        "recipient": "string",
        "amount": "string",
        "subject": "string",
        "date": "string",
    },
    "schedule_transaction": {
        "recipient": "string",
        "amount": "string",
        "subject": "string",
        "date": "string",
        "recurring": "boolean",
    },
    "update_scheduled_transaction": {
        "id": "integer",
        "recipient": "string?",
        "amount": "string?",
        "subject": "string?",
        "date": "string?",
        "recurring": "boolean?",
    },
    "get_balance": {},
    "get_most_recent_transactions": {"n": "integer"},
    "get_scheduled_transactions": {},
    "read_file": {"file_path": "string"},
    "get_user_info": {},
    "update_password": {"password": "string"},
    "update_user_info": {
        "first_name": "string?",
        "last_name": "string?",
        "street": "string?",
        "city": "string?",
    },
    "finish": {"message": "string"},
}
_IGNORED_ENVELOPE_NUMBERS = {
    "temperature",
    "top_p",
    "created_at",
    "completed_at",
    "frequency_penalty",
    "presence_penalty",
}
_SAFE_ENVELOPE_FIELDS = _IGNORED_ENVELOPE_NUMBERS | {
    "output",
    "usage",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_tokens_details",
    "output_tokens_details",
    "reasoning_tokens",
    "cached_tokens",
    "reasoning",
    "summary",
    "content",
    "moderation",
    "category_scores",
    "categories",
    "results",
    "error",
    "metadata",
    "logprobs",
    "top_logprobs",
    "token",
    "text",
    "arguments",
    "score",
    "scores",
    "value",
    "status",
    "model",
    "type",
    "id",
}
_ERROR_TYPES = {
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ReadError",
    "WriteError",
    "RemoteProtocolError",
    "ProxyError",
    "SSLError",
    "SSLCertVerificationError",
    "TimeoutError",
    "ValueError",
    "TypeError",
    "LLMConfigurationError",
    "LLMOutputError",
    "BudgetExceeded",
}
_PROVIDER_ERROR_FIELDS = {
    "code": {
        "invalid_request_error",
        "unsupported_parameter",
        "unsupported_value",
        "invalid_json_schema",
        "invalid_value",
        "missing_required_parameter",
        "model_not_found",
        "permission_denied",
        "insufficient_quota",
        "rate_limit_exceeded",
        "invalid_api_key",
        "context_length_exceeded",
        "server_error",
        "timeout",
    },
    "type": {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "rate_limit_error",
        "server_error",
        "insufficient_quota",
    },
    "param": {
        "model",
        "input",
        "tools",
        "tools[0]",
        "tools[0].parameters",
        "tools[0].strict",
        "tools[0].name",
        "reasoning",
        "reasoning.effort",
        "max_output_tokens",
        "temperature",
        "top_p",
        "text",
        "text.format",
        "parallel_tool_calls",
        "tool_choice",
        "store",
        "stream",
    },
}
_WIRE_TOKEN = re.compile(
    r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    r"|true|false|null|[{}\[\]:,]"
)
_SENSITIVE = {
    "password",
    "passwd",
    "pin",
    "otp",
    "api_key",
    "private_key",
    "phone",
    "phone_number",
    "account_number",
    "full_name",
    "raw_text",
    "passport",
    "email",
}


class LLMConfigurationError(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ReplayMiss(RuntimeError):
    pass


class ReplayCorrupt(ValueError):
    pass


class LLMOutputError(ValueError):
    pass


class LLMOutputValidationError(LLMOutputError):
    """A returned model candidate failed a caller's declared output contract."""


def _candidate_identity(output: object) -> dict[str, str | None] | None:
    if not isinstance(output, dict):
        return None
    kind, role = output.get("kind"), output.get("target_role", output.get("role"))
    if not isinstance(kind, str) or (role is not None and not isinstance(role, str)):
        return None
    candidate = {"kind": kind, "role": role}
    try:
        default_context_guard(candidate)
        if any(
            value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", value)
            for value in candidate.values()
        ):
            return None
    except ValueError:
        return None
    return candidate


def record_model_output_rejection(client: Any, exc: Exception, candidate: object = None) -> None:
    """Retain failure kinds/locations and candidate identities, never values/messages."""
    if not client.calls:
        return
    trace = client.calls[-1]
    identity = _candidate_identity(candidate or getattr(exc, "candidate", None))
    credential = getattr(client, "_api_key", None)
    if identity is not None and credential and credential in jcs_bytes(identity).decode("utf-8"):
        identity = None
    if identity is not None:
        trace.setdefault("rejected_candidates", []).append(identity)
    if hasattr(exc, "errors"):

        def location(part: object) -> object:
            if type(part) is int:
                return part
            if (
                isinstance(part, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", part)
                and not (credential and credential in part)
            ):
                return part
            return "<redacted-field>"

        trace["validation_errors"] = [
            {"type": item["type"], "loc": [location(part) for part in item["loc"]]}
            for item in exc.errors(include_url=False, include_context=False, include_input=False)
        ]
    elif not isinstance(exc, LLMOutputError):
        trace["validation_errors"] = [{"type": type(exc).__name__, "loc": []}]


def model_output_error(
    client: Any, message: str, *, output: object = None
) -> LLMOutputValidationError:
    error = LLMOutputValidationError(message)
    record_model_output_rejection(client, error, output)
    return error


def validate_model_output(client: Any, schema: Any, output: object) -> Any:
    try:
        return schema.model_validate(output)
    except ValueError as exc:
        record_model_output_rejection(client, exc, output)
        raise model_output_error(
            client, "Model output does not satisfy the declared schema"
        ) from None


def is_output_truncated(observation: object, *, max_output_tokens: int = 512) -> bool:
    """Recognize one call's retained truncation evidence, including historical formats."""
    if not isinstance(observation, dict):
        return False
    shape = observation.get("response_shape")
    details = observation.get("incomplete_details")
    counts = [observation.get("output_tokens")]
    for field in ("usage", "recorded_usage"):
        usage = observation.get(field)
        if isinstance(usage, dict):
            counts.append(usage.get("output_tokens"))
    return (
        observation.get("output_truncated") is True
        or observation.get("finish_reason") == "length"
        or (isinstance(shape, dict) and shape.get("finish_reason") == "length")
        or (isinstance(details, dict) and details.get("reason") == "max_output_tokens")
        or any(type(count) is int and count == max_output_tokens for count in counts)
    )


def response_diagnostics(
    response: object, *, max_output_tokens: int, api_key: str | None = None
) -> dict[str, Any]:
    """Capture response metadata before envelope/schema rejection; no payload text persists."""
    if not isinstance(response, dict):
        return {}
    result: dict[str, Any] = {}
    payloads: list[str] = []
    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            reason = choice.get("finish_reason")
            result["finish_reason"] = (
                reason
                if reason in {"stop", "length", "tool_calls", "content_filter", "function_call"}
                else "unsupported"
            )
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("content"), str) and message["content"]:
                payloads.append(message["content"])
            calls = message.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    payloads.append(function["arguments"])
    status = response.get("status")
    if status is not None:
        result["response_status"] = (
            status
            if status in {"completed", "incomplete", "failed", "in_progress", "queued", "cancelled"}
            else "unsupported"
        )
    details = response.get("incomplete_details")
    if "incomplete_details" in response:
        reason = details.get("reason") if isinstance(details, dict) else None
        result["incomplete_details"] = (
            None
            if details is None
            else {
                "reason": reason
                if reason in {"max_output_tokens", "content_filter"}
                else "unsupported"
            }
        )
    entries = response.get("output")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("arguments"), str):
            payloads.append(entry["arguments"])
        contents = entry.get("content")
        for content in contents if isinstance(contents, list) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                payloads.append(content["text"])
    usage = response.get("usage")
    count = (
        usage.get("output_tokens", usage.get("completion_tokens"))
        if isinstance(usage, dict)
        else None
    )
    if type(count) is int and count >= 0:
        result["output_tokens"] = count
    candidates = []
    valid = bool(payloads)
    for payload in payloads:
        try:
            # Syntax-only observation: replace numeric tokens, never strings,
            # before decoding so a legal JSON decimal is not mislabeled as
            # malformed JSON. The actual model parser still rejects floats.
            syntax = _WIRE_TOKEN.sub(
                lambda match: (
                    "0"
                    if match.group()[0] in "-0123456789"
                    and any(char in match.group() for char in ".eE")
                    else match.group()
                ),
                payload,
            )
            decoded = loads_exact(syntax)
        except (ValueError, TypeError):
            valid = False
        else:
            identity = _candidate_identity(decoded)
            if identity is not None and not (
                api_key and api_key in jcs_bytes(identity).decode("utf-8")
            ):
                candidates.append(identity)
    raw = payloads[0].encode("utf-8") if len(payloads) == 1 else jcs_bytes(payloads)
    result.update(
        output_length=sum(map(len, payloads)),
        output_sha256=sha256(raw).hexdigest(),
        output_json_valid=valid,
        output_candidates=candidates,
        output_truncated=is_output_truncated(result, max_output_tokens=max_output_tokens),
    )
    return result


@dataclass(frozen=True)
class LLMResult:
    output: dict[str, JsonValue]
    source: str
    replay_key: str


def _hash(value: object) -> str:
    """Hash all request fields, including fields named sig inside untrusted prompts."""
    return sha256(jcs_bytes(value)).hexdigest()


def _usage_metadata(value: object) -> dict[str, int]:
    """Validate persisted provider counts without treating replay as new inference."""
    if (
        not isinstance(value, dict)
        or set(value) != set(_USAGE_FIELDS)
        or any(type(value[field]) is not int for field in _USAGE_FIELDS)
        or value["input_tokens"] < 1
        or value["output_tokens"] < 1
        or not 0 <= value["reasoning_tokens"] <= value["output_tokens"]
        or value["total_tokens"] != value["input_tokens"] + value["output_tokens"]
    ):
        raise ValueError("Invalid recorded usage metadata")
    return {field: value[field] for field in _USAGE_FIELDS}


def default_context_guard(value: object) -> None:
    """Reject recognizable raw secrets/PII; a caller vault guards known private names."""
    PIIVault().ensure_safe(value, rule="PII-OUT")
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE:
                raise ValueError("PII-OUT")
            default_context_guard(key)
            default_context_guard(item)
    elif isinstance(value, list):
        for item in value:
            default_context_guard(item)


def _object(value: object) -> dict[str, JsonValue]:
    result = loads_exact(jcs_bytes(value))
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def _fingerprints(value: object, path: str = "$") -> dict[str, str]:
    result = {path: _hash(value)}
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            label = key if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", key) else _hash(key)[:12]
            result.update(_fingerprints(item, f"{path}.{label}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_fingerprints(item, f"{path}[{index}]"))
    return result


def _environment(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is not None and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() in _ENV_FIELDS:
                values[key.strip()] = value.strip().strip("\"'")
    inherited = {key: value for key, value in os.environ.items() if key in _ENV_FIELDS}
    if path is not None and path.exists():
        request_fields = _ENV_FIELDS - {"LLM_API_KEY", "LLM_MAX_CALLS", "LLM_MAX_USD"}
        if any(
            values.get(key) != value for key, value in inherited.items() if key in request_fields
        ):
            raise LLMConfigurationError(
                "Named env file conflicts with inherited request configuration"
            )
        return values
    return inherited


def _configured_base(value: str) -> str:
    """Fix one explicit origin/path; HTTP is available only for local protocol tests."""
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in ({"https", "http"} if local else {"https"})
            or any(character.isspace() for character in value)
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise LLMConfigurationError("Invalid fixed LLM_API_BASE endpoint") from None


def _chat_output(response: dict[str, Any], *, tools: bool) -> list[dict[str, Any]]:
    """Normalize one chat result to the same strict output parser as Responses."""
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError
    choice = choices[0]
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.get("role") != "assistant"
        or message.get("refusal") is not None
        or message.get("function_call") is not None
    ):
        raise ValueError
    if tools:
        calls = message.get("tool_calls")
        if (
            choice.get("finish_reason") != "tool_calls"
            or (message.get("content") is not None and not isinstance(message["content"], str))
            or not isinstance(calls, list)
            or len(calls) != 1
            or not isinstance(calls[0], dict)
            or calls[0].get("type") != "function"
            or not isinstance(calls[0].get("function"), dict)
        ):
            raise ValueError
        # Chat permits untrusted assistant commentary alongside a function call.
        # Drop it entirely: only the single typed call can reach execution/replay.
        # Text (including thinking tags) is never parsed as a substitute tool call.
        function = calls[0]["function"]
        return [
            {
                "type": "function_call",
                "status": "completed",
                "name": function.get("name"),
                "arguments": function.get("arguments"),
            }
        ]
    if (
        choice.get("finish_reason") != "stop"
        or message.get("tool_calls")
        or not isinstance(message.get("content"), str)
    ):
        raise ValueError
    return [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": message["content"]}],
        }
    ]


def local_prompt_token_probe(request: dict[str, Any]) -> dict[str, int]:
    """Measure a captured development request with Ollama's native token counter.

    This separate one-token diagnostic preserves the actual messages and tools,
    never executes the output, and never changes the service context setting.
    Callers must compare its count with the original chat usage before treating
    the native counter as a measurement of that request.
    """
    if request.get("reasoning_effort") != "none" or not isinstance(request.get("messages"), list):
        raise LLMConfigurationError("Local token diagnostics require captured nonthinking chat")
    body: dict[str, Any] = {
        "model": request["model"],
        "messages": deepcopy(request["messages"]),
        "stream": False,
        "think": False,
        "options": {"num_predict": 1},
    }
    if "tools" in request:
        body["tools"] = deepcopy(request["tools"])
    if request.get("response_format") == {"type": "json_object"}:
        body["format"] = "json"
    for field in ("seed", "temperature"):
        if field in request:
            body["options"][field] = request[field]
    import httpx

    with httpx.Client(trust_env=False, timeout=120) as client:
        response = client.post("http://127.0.0.1:11434/api/chat", content=jcs_bytes(body))
        response.raise_for_status()
    result = _response_envelope(response.content)
    if not isinstance(result, dict) or result.get("done") is not True:
        raise LLMOutputError("Local token diagnostics returned an incomplete envelope")
    counts = {key: result.get(key) for key in ("prompt_eval_count", "eval_count")}
    if any(type(value) is not int or value < 1 for value in counts.values()):
        raise LLMOutputError("Local token diagnostics returned invalid usage")
    return {key: int(value) for key, value in counts.items()}


def _strict_schema(schema: object) -> dict[str, Any]:
    """Close every tool object; arbitrary maps narrow to the empty object."""

    def close(value: object) -> Any:
        if isinstance(value, list):
            return [close(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "default":
                continue
            if key in {"properties", "$defs"} and isinstance(item, dict):
                result[key] = {name: close(child) for name, child in item.items()}
            else:
                result[key] = close(item)
        if result.get("type") == "object" or "properties" in result:
            properties = result.get("properties", {})
            if not isinstance(properties, dict):
                raise LLMConfigurationError("Invalid response tool schema")
            result.update(
                properties=properties, required=list(properties), additionalProperties=False
            )
        return result

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise LLMConfigurationError("Responses tool schema must be an object")
    closed = close(schema)
    if not isinstance(closed, dict):
        raise LLMConfigurationError("Invalid response tool schema")
    return closed


def _named_tools(
    prompt: dict[str, JsonValue], namespace: str = "wealth"
) -> list[dict[str, Any]] | None:
    """Constructor-scoped catalogues; model/prompt data cannot widen capability."""
    if "tools" not in prompt:
        return None
    offered = prompt["tools"]
    allowed = _WEALTH_TOOL_NAMES if namespace == "wealth" else set(_AGENTDOJO_TOOL_FIELDS)
    if (
        "response_schema" in prompt
        or not isinstance(offered, list)
        or not 1 <= len(offered) <= len(allowed)
    ):
        raise LLMConfigurationError("Named tools require one unambiguous tool catalogue")
    tools = []
    names = set()
    for tool in offered:
        if (
            not isinstance(tool, dict)
            or set(tool) != {"type", "name", "description", "parameters", "strict"}
            or tool.get("type") != "function"
            or tool.get("strict") is not True
            or not isinstance(tool.get("name"), str)
            or tool["name"] not in allowed
            or tool["name"] in names
            or not isinstance(tool.get("description"), str)
        ):
            raise LLMConfigurationError("Invalid named tool definition")
        names.add(tool["name"])
        parameters = _strict_schema(tool["parameters"])
        if namespace == "agentdojo-banking-v1.2.2":
            expected = _AGENTDOJO_TOOL_FIELDS[tool["name"]]
            if set(parameters["properties"]) != set(expected):
                raise LLMConfigurationError("External benchmark tool fields differ from pin")
            for field, kind in expected.items():
                schema = parameters["properties"][field]
                # Descriptive metadata is retained; executable schema shape is frozen.
                shape = {
                    key: value
                    for key, value in schema.items()
                    if key not in {"title", "description"}
                }
                target = (
                    {"anyOf": [{"type": kind[:-1]}, {"type": "null"}]}
                    if kind.endswith("?")
                    else {"type": kind}
                )
                if shape != target:
                    raise LLMConfigurationError("External benchmark tool type differs from pin")
        tools.append({**tool, "parameters": parameters})
    return tools


def _response_envelope(raw: bytes) -> Any:
    """Discard root sampling/timestamp metadata before the exact JSON decoder."""
    if len(raw) > 1_048_576:
        raise ValueError("Provider response exceeds the transport bound")
    source = raw.decode("utf-8")
    parts: list[str] = []
    stack: list[str] = []
    previous, root_key = "", ""
    offset = 0
    while offset < len(source):
        if source[offset] in " \t\r\n":
            parts.append(source[offset])
            offset += 1
            continue
        match = _WIRE_TOKEN.match(source, offset)
        if match is None:
            raise ValueError("Malformed provider JSON envelope")
        token = match.group()
        root = stack == ["{"]
        if root and previous in {"{", ","} and token.startswith('"'):
            root_key = loads_exact(token)
        if (
            root
            and previous == ":"
            and root_key in _IGNORED_ENVELOPE_NUMBERS
            and token[0] in "-0123456789"
        ):
            parts.append("null")
        else:
            parts.append(token)
        if token in {"{", "["}:
            stack.append(token)
        elif token in {"}", "]"}:
            if not stack or stack[-1] != ("{" if token == "}" else "["):
                raise ValueError("Malformed provider JSON envelope")
            stack.pop()
        previous = token
        offset = match.end()
    # Duplicate properties, malformed number tails, and all other floats still reject.
    return loads_exact("".join(parts))


def _safe_exception_diagnostics(error: Exception) -> dict[str, Any]:
    def label(item: BaseException) -> str:
        name = type(item).__name__
        return name if name in _ERROR_TYPES else "OtherError"

    result: dict[str, Any] = {"error_type": label(error)}
    causes = []
    cause = error.__cause__ or error.__context__
    for _ in range(4):
        if cause is None or cause is error:
            break
        causes.append(label(cause))
        cause = cause.__cause__ or cause.__context__
    if causes:
        result["error_cause_types"] = causes
    return result


def _envelope_diagnostics(raw: bytes, error: Exception) -> dict[str, Any]:
    """Describe decoder failures without exposing body values or unknown property names."""
    message = str(error)
    if isinstance(error, UnicodeDecodeError):
        reason = "utf8"
    elif message.startswith("JSON floating-point numbers are forbidden:"):
        reason = "float"
    elif message.startswith("Duplicate JSON property:"):
        reason = "duplicate-key"
    elif message == "Provider response exceeds the transport bound":
        reason = "response-too-large"
    elif isinstance(error, ValueError):
        reason = "json-syntax"
    else:
        reason = "other"
    result: dict[str, Any] = {"envelope_error_reason": reason}
    if reason != "float" or len(raw) > 1_048_576:
        return result
    paths: list[str] = []
    try:
        source = raw.decode("utf-8")
        stack: list[dict[str, str]] = []
        offset = 0
        while offset < len(source) and len(paths) < 12:
            if source[offset] in " \t\r\n":
                offset += 1
                continue
            match = _WIRE_TOKEN.match(source, offset)
            if match is None:
                break
            token = match.group()
            offset = match.end()
            frame = stack[-1] if stack else None
            if frame is not None and frame["kind"] == "object" and frame["phase"] == "key":
                if token.startswith('"'):
                    name = loads_exact(token)
                    frame["key"] = (
                        name
                        if name in _SAFE_ENVELOPE_FIELDS
                        else f"field_{sha256(name.encode('utf-8')).hexdigest()[:12]}"
                    )
                    frame["phase"] = "colon"
                    continue
            if token == ":" and frame is not None:
                frame["phase"] = "value"
                continue
            if token == "," and frame is not None:
                frame["phase"] = "key" if frame["kind"] == "object" else "value"
                continue
            if token in {"}", "]"}:
                if stack:
                    stack.pop()
                continue
            path = "$"
            if frame is not None:
                path = frame["path"] + (f".{frame['key']}" if frame["kind"] == "object" else "[*]")
                frame["phase"] = "after"
            if token in {"{", "["}:
                if len(stack) >= 32:
                    break
                stack.append({
                    "kind": "object" if token == "{" else "array",
                    "path": path,
                    "key": "",
                    "phase": "key" if token == "{" else "value",
                })
            elif (
                token[0] in "-0123456789"
                and any(character in token for character in ".eE")
                and path not in {f"$.{name}" for name in _IGNORED_ENVELOPE_NUMBERS}
                and path not in paths
            ):
                paths.append(path)
    except (ValueError, TypeError):
        pass
    if paths:
        result["envelope_float_paths"] = paths
    return result


def _safe_provider_diagnostics(raw: bytes) -> dict[str, str]:
    try:
        envelope = _response_envelope(raw)
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if not isinstance(error, dict):
            return {}
        return {
            f"provider_error_{key}": error[key]
            for key, allowed in _PROVIDER_ERROR_FIELDS.items()
            if isinstance(error.get(key), str) and error[key] in allowed
        }
    except (ValueError, TypeError):
        return {}


def _omit_null_defaults(value: object, schema: dict[str, Any]) -> object:
    """Restore omitted optional fields after strict tools made every field required."""

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = schema.get("$defs", {}).get(reference[8:])
            if isinstance(target, dict):
                return {**target, **{key: item for key, item in node.items() if key != "$ref"}}
        return node

    def prune(item: object, node: dict[str, Any], depth: int = 0) -> object:
        if depth > 128:
            raise LLMOutputError("Model output nesting exceeds the response bound")
        node = resolve(node)
        for branch in node.get("anyOf", []):
            if not isinstance(branch, dict):
                continue
            branch = resolve(branch)
            kind = branch.get("type")
            if (kind == "object" and isinstance(item, dict)) or (
                kind == "array" and isinstance(item, list)
            ):
                return prune(item, branch, depth + 1)
        if isinstance(item, dict):
            properties = node.get("properties", {})
            result = {}
            for key, child in item.items():
                child_schema = properties.get(key, node.get("additionalProperties", {}))
                if not isinstance(child_schema, dict):
                    child_schema = {}
                if child is None and "default" in child_schema and child_schema["default"] is None:
                    continue
                result[key] = prune(child, child_schema, depth + 1)
            return result
        if isinstance(item, list):
            child_schema = node.get("items", {})
            if not isinstance(child_schema, dict):
                child_schema = {}
            return [prune(child, child_schema, depth + 1) for child in item]
        return item

    return prune(value, schema)


class LLMClient:
    def __init__(
        self,
        mode: str = "scripted",
        replay_dir: Path = Path("data/replay/bank"),
        model: str = "scripted-bank-v1",
        config: str = "cca",
        user_policy: str = "careful",
        context_guard: Callable[[object], None] | None = None,
        *,
        api_key: str | None = None,
        max_calls: int | None = None,
        max_usd: Fraction | str | None = None,
        max_output_tokens: int = 512,
        env_file: Path | None = Path(".env"),
        api: str | None = None,
        api_base: str | None = None,
        provider: str | None = None,
        local_mock: bool = False,
        reasoning_effort: str | None = None,
        seed: int | None = None,
        temperature: int | None = None,
        deployment_profile: dict[str, JsonValue] | None = None,
        request_timeout_seconds: int = 20,
        tool_namespace: str = "wealth",
        model_tier: str = "main",
        assumed_price_in: Fraction | str | None = None,
        assumed_price_out: Fraction | str | None = None,
    ) -> None:
        if tool_namespace not in {"wealth", "agentdojo-banking-v1.2.2"}:
            raise LLMConfigurationError("Unknown tool namespace")
        self.tool_namespace = tool_namespace
        if mode not in _MODES:
            raise LLMConfigurationError("Unknown planner mode")
        self.mode, self.replay_dir = mode, Path(replay_dir)
        self.model, self.config, self.user_policy = model, config, user_policy
        self.context_guard = context_guard or default_context_guard
        self.calls: list[dict[str, Any]] = []
        self.observers: list[Callable[[str, object], None]] = []
        self.transport_observers: list[Callable[[object], None]] = []
        self.spent_usd = Fraction(0)
        self.api_calls = 0
        self._api_key = ""
        self._budget_exhausted = False
        self.max_calls = 0
        self.max_usd = Fraction(0)
        self.max_output_tokens = max_output_tokens
        if type(request_timeout_seconds) is not int or not 1 <= request_timeout_seconds <= 600:
            raise LLMConfigurationError("Request timeout must be bounded to 600 seconds")
        self.request_timeout_seconds = request_timeout_seconds
        if model_tier not in {"main", "cheap", "weak"}:
            raise LLMConfigurationError("Unknown model tier")
        env = _environment(env_file) if mode in {"live", "record"} else {}
        self.provider = provider or env.get("LLM_PROVIDER") or "openai"
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.provider):
            raise LLMConfigurationError("Invalid fixed LLM_PROVIDER")
        self.api_base = _configured_base(api_base or env.get("LLM_API_BASE") or _DEFAULT_API_BASE)
        self._custom_endpoint = self.provider != "openai" or self.api_base != _DEFAULT_API_BASE
        if type(local_mock) is not bool or (
            local_mock and urlsplit(self.api_base).hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise LLMConfigurationError("Local mock provenance requires a loopback API base")
        self.local_mock = local_mock
        self._record_source = "local-mock-recorded" if local_mock else "live-recorded"
        if mode in {"live", "record"}:
            self._api_key = api_key if api_key is not None else env.get("LLM_API_KEY", "")
            if not self._api_key:
                raise LLMConfigurationError("Live/record requires an API key credential")
            if model == "scripted-bank-v1":
                model_key = (
                    "LLM_MODEL" if model_tier == "main" else f"LLM_MODEL_{model_tier.upper()}"
                )
                self.model = env.get(model_key, model)
            raw_model = self.model.removeprefix(f"{self.provider}/").removeprefix("openai/")
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", raw_model)
                or self.model.endswith(("-latest", ":latest"))
                or self.model == "scripted-bank-v1"
            ):
                raise LLMConfigurationError("Live/record requires a fixed model")
            try:
                call_caps = ([max_calls] if max_calls is not None else []) + (
                    [int(env["LLM_MAX_CALLS"])] if "LLM_MAX_CALLS" in env else []
                )
                usd_caps = ([max_usd] if max_usd is not None else []) + (
                    [env["LLM_MAX_USD"]] if "LLM_MAX_USD" in env else []
                )
                if not call_caps or not usd_caps:
                    raise ValueError
                if any(type(cap) is not int or cap < 1 for cap in call_caps):
                    raise ValueError
                if any(isinstance(cap, (float, bool)) for cap in usd_caps):
                    raise ValueError
                fractions = [Fraction(cap) for cap in usd_caps]
                if any(cap <= 0 for cap in fractions):
                    raise ValueError
                self.max_calls, self.max_usd = min(call_caps), min(fractions)
            except (ValueError, TypeError, ZeroDivisionError):
                raise LLMConfigurationError(
                    "Live/record requires positive call and USD budgets"
                ) from None
        is_gpt6 = (
            self.model.removeprefix(f"{self.provider}/").removeprefix("openai/").startswith("gpt-6")
        )
        self.api = api or env.get("LLM_API") or ("responses" if is_gpt6 else "chat")
        if self.api == "chat.completions":
            self.api = "chat"
        if self.api not in {"chat", "responses"}:
            raise LLMConfigurationError("Unknown OpenAI API")
        if seed is not None and (type(seed) is not int or not 0 <= seed < 2**31):
            raise LLMConfigurationError("Invalid integer sampling seed")
        if seed is not None and self.api == "responses":
            raise LLMConfigurationError("Responses seed is unsupported; use independent repeats")
        if temperature is not None and (
            type(temperature) is not int or temperature not in {0, 1, 2}
        ):
            raise LLMConfigurationError("Temperature must be an exact supported integer")
        self.seed, self.temperature = seed, temperature
        self.deployment_profile = (
            _object(deployment_profile) if deployment_profile is not None else None
        )
        if self.provider in {"deepseek", "zhipu"}:
            self.deployment_profile = {
                **(self.deployment_profile or {}),
                "thinking": "disabled",
                "tool_choice_mode": (self.deployment_profile or {}).get(
                    "tool_choice_mode", "forced_function"
                ),
            }
            if self.deployment_profile["tool_choice_mode"] not in {"forced_function", "auto"}:
                raise LLMConfigurationError("Unsupported domestic tool choice mode")
        if self.deployment_profile is not None:
            self.context_guard(self.deployment_profile)
        self.reasoning_effort = (
            reasoning_effort
            or env.get("LLM_REASONING_EFFORT")
            or ("low" if self.api == "responses" else None)
        )
        if self.reasoning_effort not in {None, "none", "minimal", "low", "medium", "high", "xhigh"}:
            raise LLMConfigurationError("Unknown reasoning effort")
        if mode in {"live", "record"} and is_gpt6 and self.api != "responses":
            raise LLMConfigurationError("GPT-6 function tools require the Responses API")
        if type(max_output_tokens) is not int or (
            not 128 <= max_output_tokens <= 8192
            if self.api == "responses"
            else max_output_tokens != 512
        ):
            raise LLMConfigurationError("Invalid bounded generation profile")
        try:
            if (
                mode in {"live", "record"}
                and self._custom_endpoint
                and not (
                    (assumed_price_in is not None or "LLM_ASSUMED_PRICE_IN" in env)
                    and (assumed_price_out is not None or "LLM_ASSUMED_PRICE_OUT" in env)
                )
            ):
                raise ValueError
            self._assumed_pricing_configured = (
                assumed_price_in is not None
                or assumed_price_out is not None
                or "LLM_ASSUMED_PRICE_IN" in env
                or "LLM_ASSUMED_PRICE_OUT" in env
            )
            prices = (
                assumed_price_in
                if assumed_price_in is not None
                else env.get("LLM_ASSUMED_PRICE_IN", "10"),
                assumed_price_out
                if assumed_price_out is not None
                else env.get("LLM_ASSUMED_PRICE_OUT", "40"),
            )
            if any(isinstance(price, (float, bool)) for price in prices):
                raise ValueError
            self.assumed_price_in, self.assumed_price_out = (Fraction(price) for price in prices)
            if min(self.assumed_price_in, self.assumed_price_out) <= 0:
                raise ValueError
        except (ValueError, TypeError, ZeroDivisionError):
            raise LLMConfigurationError(
                "Assumed token prices must be positive exact values"
            ) from None

    def complete(
        self,
        *,
        task_id: str,
        step: str,
        prompt: dict[str, JsonValue],
        tool_state: dict[str, JsonValue],
        sample_idx: int = 0,
        scripted: Callable[[], dict[str, JsonValue]],
    ) -> LLMResult:
        if not task_id or not step or type(sample_idx) is not int or sample_idx < 0:
            raise ValueError("Invalid replay request identity")
        prompt, tool_state = _object(prompt), _object(tool_state)
        named_tools = _named_tools(prompt, self.tool_namespace)
        request: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "model": self.model,
            "api": self.api,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "prompt_hash": _hash(prompt),
            "tool_state_hash": _hash(tool_state),
            "task_id": task_id,
            "step": step,
            "sample_idx": sample_idx,
            "config": self.config,
            "user_policy": self.user_policy,
        }
        # Preserve historical OpenAI identities; custom transports cannot share their cache.
        if self.tool_namespace != "wealth":
            request["tool_namespace"] = self.tool_namespace
        if self._custom_endpoint:
            request.update(provider=self.provider, api_base_hash=_hash(self.api_base))
        if self.local_mock:
            request["transport"] = "local-mock"
        if (
            self.seed is not None
            or self.temperature is not None
            or self.deployment_profile is not None
        ):
            request["sampling"] = {
                "seed": self.seed,
                "temperature": self.temperature
                if self.temperature is not None
                else "provider-default",
            }
            request["deployment_profile"] = self.deployment_profile
        key = H(request)
        trace = {**request, "request_hash": key, "replay_key": key, "source": "context-rejected"}
        try:
            for value in (
                prompt,
                tool_state,
                {
                    "task_id": task_id,
                    "step": step,
                    "model": self.model,
                    "config": self.config,
                    "user_policy": self.user_policy,
                },
            ):
                self.context_guard(value)
        except Exception:
            for name in ("task_id", "step", "model", "config", "user_policy"):
                trace[name] = f"redacted:{_hash(request[name])}"
            self.calls.append(trace)
            raise ValueError("PII-CTX") from None
        self.calls.append(trace)
        self._observe("request", {"prompt": prompt, "tool_state": tool_state})
        path = self.replay_dir / key[:16] / f"{request['prompt_hash'][:16]}.json"
        if self.mode == "record" and path.exists():
            trace["source"] = "record-exists"
            raise ReplayCorrupt("Refusing to replace an existing live replay")
        if self.mode in {"strict", "fallback"}:
            if path.exists():
                try:
                    record = loads_exact(path.read_bytes())
                    if (
                        record["source"] != self._record_source
                        or record["request"] != request
                        or record["replay_key"] != key
                        or record["output_hash"] != _hash(record["output"])
                    ):
                        raise ValueError
                    if "usage" in record or "usage_hash" in record:
                        usage = _usage_metadata(record["usage"])
                        if (
                            record["usage_hash"] != _hash(usage)
                            or usage["output_tokens"] > self.max_output_tokens
                        ):
                            raise ValueError
                        trace["recorded_usage"] = usage
                    if "response_metadata" in record:
                        metadata = record["response_metadata"]
                        if not isinstance(metadata, dict) or record.get(
                            "response_metadata_hash"
                        ) != _hash(metadata):
                            raise ValueError
                        trace.update({
                            key: value
                            for key, value in metadata.items()
                            if key in RESPONSE_DIAGNOSTIC_FIELDS and key not in _USAGE_FIELDS
                        })
                    output = self._output(record["output"], named_tools)
                except (ValueError, KeyError, TypeError, OSError):
                    trace["source"] = "replay-corrupt"
                    raise ReplayCorrupt("Invalid real-response replay record") from None
                trace["source"] = "replay"
                return LLMResult(output, "replay", key)
            if self.mode == "strict":
                trace["source"] = "replay-miss"
                raise ReplayMiss(self._miss(request, prompt))
        if self.mode in {"scripted", "fallback"}:
            source = "scripted" if self.mode == "scripted" else "scripted-fallback"
            output = self._output(scripted(), named_tools)
        else:
            trace["source"] = "live-attempt"
            try:
                output = self._live(prompt, tool_state, trace)
            except TimeoutError:
                output, source = self._output(scripted(), named_tools), "scripted-timeout"
            else:
                source = (
                    self._record_source
                    if self.mode == "record"
                    else "local-mock"
                    if self.local_mock
                    else "live"
                )
                if self.mode == "record":
                    usage = _usage_metadata({field: trace[field] for field in _USAGE_FIELDS})
                    record = {
                        "source": source,
                        "request": request,
                        "replay_key": key,
                        "prompt_fingerprints": _fingerprints(prompt),
                        "output": output,
                        "output_hash": _hash(output),
                        "cost_usd": trace["cost_usd"],
                        "usage": usage,
                        "usage_hash": _hash(usage),
                    }
                    metadata = {
                        key: trace[key] for key in RESPONSE_DIAGNOSTIC_FIELDS if key in trace
                    }
                    record.update(
                        response_metadata=metadata, response_metadata_hash=_hash(metadata)
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = jcs_bytes(record)
                    if path.exists():
                        raise ReplayCorrupt("Refusing to replace an existing live replay")
                    with path.open("xb") as stream:
                        if stream.write(data) != len(data):
                            raise OSError("Replay write failed")
                        stream.flush()
        trace["source"] = source
        return LLMResult(deepcopy(output), source, key)

    def _observe(self, stage: str, value: object) -> None:
        """Offer in-memory inspection without persisting data or allowing mutation."""
        for observer in self.observers:
            observer(stage, deepcopy(value))

    def _output(
        self, output: object, named_tools: list[dict[str, Any]] | None = None
    ) -> dict[str, JsonValue]:
        try:
            result = _object(output)
            if named_tools is not None and (
                set(result) != {"tool", "arguments"}
                or not isinstance(result.get("tool"), str)
                or result["tool"] not in {tool["name"] for tool in named_tools}
                or not isinstance(result.get("arguments"), dict)
            ):
                raise LLMOutputError("Invalid named tool selection or arguments")
            if self._api_key and self._api_key in jcs_bytes(result).decode("utf-8"):
                raise ValueError
            self._observe("output", result)
            self.context_guard(result)
            return result
        except (ValueError, TypeError) as exc:
            record_model_output_rejection(self, exc, output)
            raise LLMOutputError("Invalid or unsafe model output (PII-OUT)") from None

    def _miss(self, request: dict[str, Any], prompt: dict[str, JsonValue]) -> str:
        current = _fingerprints(prompt)
        previous: dict[str, str] = {}
        for path in sorted(self.replay_dir.glob("*/*.json")):
            try:
                record = loads_exact(path.read_bytes())
                if all(record["request"][key] == request[key] for key in ("task_id", "step")):
                    candidate = record.get("prompt_fingerprints", {})
                    path_pattern = r"\$(?:\.[A-Za-z_][A-Za-z_0-9]{0,63}|\.[0-9a-f]{12}|\[[0-9]+\])*"
                    if isinstance(candidate, dict) and all(
                        isinstance(name, str)
                        and re.fullmatch(path_pattern, name)
                        and isinstance(digest, str)
                        and re.fullmatch(r"[0-9a-f]{64}", digest)
                        for name, digest in candidate.items()
                    ):
                        previous = candidate
                    break
            except (ValueError, KeyError, TypeError, OSError):
                continue
        changes = [
            f"{path}: {previous.get(path, 'missing')[:12]} -> {current.get(path, 'missing')[:12]}"
            for path in sorted(previous.keys() | current.keys())
            if previous.get(path) != current.get(path)
        ]
        difference = ", ".join(changes[:20]) or "unchanged prompt; request metadata differs"
        return (
            f"Replay miss task_id={request['task_id']} step={request['step']}; "
            f"prompt diff (hashes only): {difference}"
        )

    def _live(
        self, prompt: dict[str, JsonValue], state: dict[str, JsonValue], trace: dict[str, Any]
    ) -> dict[str, JsonValue]:
        if (
            self.api == "responses"
            or "response_schema" in prompt
            or "tools" in prompt
            or self._custom_endpoint
            or self.seed is not None
            or self.temperature is not None
            or self.reasoning_effort is not None
            or self.deployment_profile is not None
        ):
            return self._responses(prompt, state, trace)
        with _SDK_LOCK:
            if self._budget_exhausted or self.api_calls >= self.max_calls:
                trace["source"] = "budget-rejected"
                raise BudgetExceeded("LLM call budget exhausted")
            # Importing LiteLLM otherwise refreshes its remote pricing map.
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
            os.environ["EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER"] = "false"
            import litellm

            raw_model = self.model.removeprefix("openai/")
            info = litellm.model_cost.get(self.model, litellm.model_cost.get(raw_model, {}))
            try:
                input_limit = info["max_input_tokens"]
                output_limit = info["max_output_tokens"]
                if info.get("mode") != "chat":
                    raise ValueError
                if (
                    type(input_limit) is not int
                    or input_limit < 1
                    or self.max_output_tokens > output_limit
                ):
                    raise ValueError
                input_rate = max(
                    Fraction(str(value))
                    for key, value in info.items()
                    if key.startswith("input_cost_per_token") and value is not None
                )
                output_rate = max(
                    Fraction(str(value))
                    for key, value in info.items()
                    if key.startswith("output_cost_per_token") and value is not None
                )
                if input_rate < 0 or output_rate < 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError, ZeroDivisionError):
                trace["source"] = "pricing-rejected"
                raise LLMConfigurationError("Missing bounded local model pricing") from None
            if self._assumed_pricing_configured:
                input_rate = max(input_rate, self.assumed_price_in / 1_000_000)
                output_rate = max(output_rate, self.assumed_price_out / 1_000_000)
            trace["pricing_source"] = (
                "assumed_or_local_max" if self._assumed_pricing_configured else "local-model-map"
            )
            messages = [
                {
                    "role": "system",
                    "content": "Return exactly one JSON object. Use only supplied typed handles.",
                },
                {
                    "role": "user",
                    "content": jcs_bytes({"prompt": prompt, "tool_state": state}).decode("utf-8"),
                },
            ]
            # A byte bound avoids tokenizer downloads or approximate sampling.
            frozen_messages = jcs_bytes(messages)
            input_bound = len(frozen_messages) + 128
            if input_bound > input_limit:
                raise LLMConfigurationError("Request exceeds conservative model input bound")
            reserve = input_bound * input_rate + self.max_output_tokens * output_rate
            if self.spent_usd + reserve > self.max_usd:
                trace["source"] = "budget-rejected"
                raise BudgetExceeded("LLM USD budget cannot cover the bounded request cost")
            import httpx
            from openai import OpenAI

            expected_url = self.api_base + "/chat/completions"
            output_cap = self.max_output_tokens
            credential = self._api_key

            class SingleRequestTransport(httpx.BaseTransport):
                def __init__(self) -> None:
                    self.inner = httpx.HTTPTransport(retries=0, trust_env=False)
                    self.sent = False
                    self.responded = False
                    self.raw: object = None

                def handle_request(self, request: httpx.Request) -> httpx.Response:
                    if self.sent:
                        raise BudgetExceeded("Provider retry blocked before a second HTTP request")
                    body = loads_exact(request.content)
                    if (
                        request.method != "POST"
                        or str(request.url) != expected_url
                        or not isinstance(body, dict)
                        or body.get("model") != raw_model
                        or jcs_bytes(body.get("messages")) != frozen_messages
                        or body.get("max_completion_tokens", body.get("max_tokens")) != 512
                        or ("max_completion_tokens" in body and "max_tokens" in body)
                        or body.get("n", 1) != 1
                        or body.get("stream", False) is not False
                        or body.get("response_format") != {"type": "json_object"}
                        or set(body)
                        - {
                            "model",
                            "messages",
                            "max_completion_tokens",
                            "max_tokens",
                            "n",
                            "stream",
                            "response_format",
                        }
                    ):
                        raise LLMConfigurationError("Provider request changed fixed model or scope")
                    self.sent = True
                    response = self.inner.handle_request(request)
                    self.responded = 200 <= response.status_code < 300
                    if self.responded:
                        self.raw = _response_envelope(response.read())
                        trace.update(
                            response_diagnostics(
                                self.raw, max_output_tokens=output_cap, api_key=credential
                            )
                        )
                    return response

                def close(self) -> None:
                    self.inner.close()

            self.api_calls += 1
            self.spent_usd += reserve
            trace["cost_usd"] = reserve
            previous = {
                name: getattr(litellm, name, None)
                for name in (
                    "num_retries",
                    "model_fallbacks",
                    "drop_params",
                    "route_all_chat_openai_to_responses",
                    "model_alias_map",
                )
            }
            litellm.num_retries, litellm.model_fallbacks, litellm.drop_params = 0, None, False
            litellm.route_all_chat_openai_to_responses = False
            litellm.model_alias_map = {}
            try:
                transport = SingleRequestTransport()
                with httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=self.request_timeout_seconds,
                ) as http_client:
                    client = OpenAI(
                        api_key=self._api_key,
                        max_retries=0,
                        http_client=http_client,
                        base_url=self.api_base,
                        timeout=self.request_timeout_seconds,
                    )
                    litellm.completion(
                        model=f"openai/{raw_model}",
                        messages=deepcopy(messages),
                        client=client,
                        api_key=self._api_key,
                        max_completion_tokens=self.max_output_tokens,
                        n=1,
                        stream=False,
                        response_format={"type": "json_object"},
                        timeout=self.request_timeout_seconds,
                        num_retries=0,
                        max_retries=0,
                        retry_policy=None,
                        context_window_fallback_dict={},
                        _skip_responses_api_bridge=True,
                        caching=False,
                        api_base=self.api_base,
                    )
                    if not transport.responded:
                        raise LLMOutputError(
                            "No actual provider request; refusing cached SDK output"
                        )
            except Exception as exc:
                trace["source"] = "live-error"
                if "output_candidates" in trace:
                    trace["rejected_candidates"] = deepcopy(trace["output_candidates"])
                if isinstance(exc, (TimeoutError, getattr(litellm, "Timeout", TimeoutError))):
                    raise TimeoutError("Model request timed out") from None
                raise LLMOutputError("Model request failed; no replay recorded") from None
            finally:
                for name, value in previous.items():
                    setattr(litellm, name, value)
            try:
                response = transport.raw
                if not isinstance(response, dict):
                    raise ValueError
                usage = response.get("usage")
                if not isinstance(usage, dict):
                    raise ValueError
                used_in, used_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
                details = usage.get("completion_tokens_details") or {}
                if not isinstance(details, dict):
                    raise ValueError
                reasoning = details.get("reasoning_tokens", 0)
                if (
                    type(used_in) is not int
                    or type(used_out) is not int
                    or type(reasoning) is not int
                ):
                    raise ValueError
                total = usage.get("total_tokens", used_in + used_out)
                if (
                    used_in < 1
                    or used_out < 1
                    or type(total) is not int
                    or total != used_in + used_out
                    or not 0 <= reasoning <= used_out
                ):
                    raise ValueError
                actual = used_in * input_rate + used_out * output_rate
                trace.update(
                    input_tokens=used_in,
                    output_tokens=used_out,
                    reasoning_tokens=reasoning,
                    total_tokens=total,
                )
                # Provider usage is billable even when the output is unusable.
                # Settle known usage before parsing; retain the bound only when
                # the provider did not return valid, exact usage measurements.
                self.spent_usd += actual - reserve
                trace["cost_usd"] = actual
                if (
                    used_in > input_bound
                    or used_out > self.max_output_tokens
                    or self.spent_usd > self.max_usd
                ):
                    self._budget_exhausted = True
                    trace["source"] = "budget-rejected"
                    raise BudgetExceeded("Provider usage exceeded the reserved request budget")
                choices = response["choices"]
                if len(choices) != 1:
                    raise ValueError
                choice = choices[0]
                message = choice["message"] if isinstance(choice, dict) else choice.message
                content = message["content"] if isinstance(message, dict) else message.content
                if not isinstance(content, str):
                    raise ValueError
                output = self._output(loads_exact(content))
                return output
            except LLMOutputError:
                trace["source"] = "output-rejected"
                trace["rejected_candidates"] = deepcopy(trace.get("output_candidates", []))
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                trace["source"] = "output-rejected"
                trace["rejected_candidates"] = deepcopy(trace.get("output_candidates", []))
                raise LLMOutputError("Invalid model response; no replay recorded") from None

    def _responses(
        self, prompt: dict[str, JsonValue], state: dict[str, JsonValue], trace: dict[str, Any]
    ) -> dict[str, JsonValue]:
        """One structured request; chat and Responses share budgets and output validation."""
        with _SDK_LOCK:
            if self._budget_exhausted or self.api_calls >= self.max_calls:
                trace["source"] = "budget-rejected"
                raise BudgetExceeded("LLM call or USD budget exhausted")
            raw_model = self.model.removeprefix(f"{self.provider}/").removeprefix("openai/")
            chat = self.api == "chat"
            schema = prompt.get("response_schema")
            named_tools = _named_tools(prompt, self.tool_namespace)
            if schema is not None and not isinstance(schema, dict):
                raise LLMConfigurationError("Responses tool schema must be an object")
            body: dict[str, Any] = {
                "model": raw_model,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "Choose exactly one supplied function. Use only supplied typed handles."
                            if named_tools is not None
                            else "Return exactly one JSON object. Use only supplied typed handles."
                        ),
                    },
                    {
                        "role": "user",
                        "content": jcs_bytes({"prompt": prompt, "tool_state": state}).decode(
                            "utf-8"
                        ),
                    },
                ],
                "max_output_tokens": self.max_output_tokens,
                "store": False,
                "stream": False,
            }
            if self.reasoning_effort is not None:
                body["reasoning"] = {"effort": self.reasoning_effort}
            if self.temperature is not None:
                body["temperature"] = self.temperature
            if named_tools is not None:
                body.update(tools=named_tools, tool_choice="required", parallel_tool_calls=False)
            elif schema is not None:
                body.update(
                    tools=[
                        {
                            "type": "function",
                            "name": "emit_result",
                            "description": (
                                "Return the requested typed result using only supplied handles."
                            ),
                            "parameters": _strict_schema(schema),
                            "strict": True,
                        }
                    ],
                    tool_choice={"type": "function", "name": "emit_result"},
                    parallel_tool_calls=False,
                )
            else:
                body["text"] = {"format": {"type": "json_object"}}
            if chat:
                body["messages"] = body.pop("input")
                body["max_tokens"] = body.pop("max_output_tokens")
                body["n"] = 1
                body.pop("store")
                body.pop("reasoning", None)
                if self.reasoning_effort is not None:
                    body["reasoning_effort"] = self.reasoning_effort
                if self.seed is not None:
                    body["seed"] = self.seed
                if "tools" in body:
                    # Compatible providers receive the same closed parameters, without
                    # assuming support for OpenAI's optional strict-mode wire flag.
                    body["tools"] = [
                        {
                            "type": "function",
                            "function": {
                                key: value
                                for key, value in tool.items()
                                if key not in {"type", "strict"}
                            },
                        }
                        for tool in body["tools"]
                    ]
                    if isinstance(body["tool_choice"], dict):
                        body["tool_choice"] = {
                            "type": "function",
                            "function": {"name": "emit_result"},
                        }
                else:
                    body["response_format"] = body.pop("text")["format"]
                if self.provider in {"deepseek", "zhipu"}:
                    body["thinking"] = {"type": "disabled"}
                    if (
                        "tools" in body
                        and (self.deployment_profile or {}).get("tool_choice_mode") == "auto"
                    ):
                        body["tool_choice"] = "auto"
            # UTF-8 bytes bound text tokens; padding covers tool/message framing.
            frozen_body = jcs_bytes(body)
            input_bound = len(frozen_body) + _RESPONSE_INPUT_PADDING
            input_rate = self.assumed_price_in / 1_000_000
            output_rate = self.assumed_price_out / 1_000_000
            reserve = input_bound * input_rate + self.max_output_tokens * output_rate
            if self.spent_usd + reserve > self.max_usd:
                trace["source"] = "budget-rejected"
                raise BudgetExceeded("LLM USD budget cannot cover the bounded request cost")
            import httpx
            from openai import OpenAI

            expected_url = self.api_base + ("/chat/completions" if chat else "/responses")
            output_cap = self.max_output_tokens
            credential = self._api_key
            transport_observers = self.transport_observers

            class SingleResponseTransport(httpx.BaseTransport):
                def __init__(self) -> None:
                    self.inner = httpx.HTTPTransport(retries=0, trust_env=False)
                    self.sent = False
                    self.responded = False
                    self.raw: object = None

                def handle_request(self, request: httpx.Request) -> httpx.Response:
                    if self.sent:
                        raise BudgetExceeded("Provider retry blocked before a second HTTP request")
                    if (
                        request.method != "POST"
                        or str(request.url) != expected_url
                        or jcs_bytes(loads_exact(request.content)) != frozen_body
                    ):
                        raise LLMConfigurationError("Provider request changed fixed model or scope")
                    self.sent = True
                    for observer in transport_observers:
                        observer(deepcopy(loads_exact(request.content)))
                    trace["outbound_request_hash"] = _hash(loads_exact(request.content))
                    response = self.inner.handle_request(request)
                    self.responded = 200 <= response.status_code < 300
                    if type(response.status_code) is int and 100 <= response.status_code <= 599:
                        trace["http_status"] = response.status_code
                    if self.responded:
                        # Preserve wire types: the SDK may coerce boolean/string usage to ints.
                        payload = response.read()
                        try:
                            self.raw = _response_envelope(payload)
                            trace.update(
                                response_diagnostics(
                                    self.raw, max_output_tokens=output_cap, api_key=credential
                                )
                            )
                        except (ValueError, TypeError) as exc:
                            trace["error_stage"] = "response-envelope"
                            trace.update(_envelope_diagnostics(payload, exc))
                            raise
                    else:
                        trace.update(_safe_provider_diagnostics(response.read()))
                    return response

                def close(self) -> None:
                    self.inner.close()

            self.api_calls += 1
            self.spent_usd += reserve
            trace.update(cost_usd=reserve, pricing_source="assumed")
            try:
                transport = SingleResponseTransport()
                with httpx.Client(
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=self.request_timeout_seconds,
                ) as http_client:
                    client = OpenAI(
                        api_key=self._api_key,
                        max_retries=0,
                        http_client=http_client,
                        base_url=self.api_base,
                        timeout=self.request_timeout_seconds,
                    )
                    if chat:
                        if self.provider in {"deepseek", "zhipu"}:
                            adapted = deepcopy(body)
                            thinking = adapted.pop("thinking")
                            client.chat.completions.create(
                                **adapted, extra_body={"thinking": thinking}
                            )
                        else:
                            client.chat.completions.create(**deepcopy(body))
                    else:
                        client.responses.create(**deepcopy(body))
                    if not transport.responded:
                        raise LLMOutputError(
                            "No actual provider request; refusing cached SDK output"
                        )
            except Exception as exc:
                trace["source"] = "live-error"
                if "output_candidates" in trace:
                    trace["rejected_candidates"] = deepcopy(trace["output_candidates"])
                trace.update(_safe_exception_diagnostics(exc))
                # Responses live mode never turns a failed paid request into scripted success.
                raise LLMOutputError("Model request failed; no replay recorded") from None
            try:
                response = transport.raw
                if not isinstance(response, dict):
                    raise ValueError
                returned_model = response.get("model")
                if (
                    isinstance(returned_model, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", returned_model)
                    and not (self._api_key and self._api_key in returned_model)
                ):
                    trace["returned_model"] = returned_model
                if chat and isinstance(response.get("choices"), list):
                    choices = response["choices"]
                    if len(choices) == 1 and isinstance(choices[0], dict):
                        message = choices[0].get("message")
                        if isinstance(message, dict):
                            trace["response_shape"] = {
                                "finish_reason": choices[0].get("finish_reason")
                                if choices[0].get("finish_reason")
                                in {
                                    "stop",
                                    "length",
                                    "tool_calls",
                                    "content_filter",
                                    "function_call",
                                }
                                else "unsupported",
                                "content_length": len(message.get("content") or ""),
                                "tool_calls": len(message.get("tool_calls") or []),
                            }
                usage = response.get("usage")
                if not isinstance(usage, dict):
                    raise ValueError
                if chat:
                    usage = {
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                        "output_tokens_details": usage.get("completion_tokens_details"),
                    }
                used_in, used_out = usage.get("input_tokens"), usage.get("output_tokens")
                total = usage.get("total_tokens")
                details = usage.get("output_tokens_details") or {}
                if not isinstance(details, dict):
                    raise ValueError
                reasoning = details.get("reasoning_tokens", 0)
                if (
                    type(used_in) is not int
                    or type(used_out) is not int
                    or type(total) is not int
                    or type(reasoning) is not int
                ):
                    raise ValueError
                if (
                    used_in < 1
                    or used_out < 1
                    or total != used_in + used_out
                    or not 0 <= reasoning <= used_out
                ):
                    raise ValueError
                actual = used_in * input_rate + used_out * output_rate
                trace.update(
                    input_tokens=used_in,
                    output_tokens=used_out,
                    reasoning_tokens=reasoning,
                    total_tokens=total,
                )
                # Valid provider usage settles the charge before output parsing.
                self.spent_usd += actual - reserve
                trace["cost_usd"] = actual
                if (
                    used_in > input_bound
                    or used_out > self.max_output_tokens
                    or self.spent_usd > self.max_usd
                ):
                    self._budget_exhausted = True
                    trace["source"] = "budget-rejected"
                    raise BudgetExceeded("Provider usage exceeded the reserved request budget")
                if chat:
                    entries = _chat_output(
                        response, tools=named_tools is not None or schema is not None
                    )
                else:
                    entries = response.get("output")
                if (not chat and response.get("status") != "completed") or response.get(
                    "error"
                ) is not None:
                    raise ValueError
                if not isinstance(entries, list) or not all(
                    isinstance(entry, dict) for entry in entries
                ):
                    raise ValueError
                substantive = [entry for entry in entries if entry.get("type") != "reasoning"]
                if len(substantive) != 1:
                    raise ValueError
                entry = substantive[0]
                if named_tools is not None:
                    if (
                        entry.get("type") != "function_call"
                        or entry.get("status", "completed") != "completed"
                        or not isinstance(entry.get("arguments"), str)
                    ):
                        raise ValueError
                    output = self._output(
                        {"tool": entry.get("name"), "arguments": loads_exact(entry["arguments"])},
                        named_tools,
                    )
                elif schema is not None:
                    if (
                        entry.get("type") != "function_call"
                        or entry.get("name") != "emit_result"
                        or entry.get("status", "completed") != "completed"
                        or not isinstance(entry.get("arguments"), str)
                    ):
                        raise ValueError
                    decoded = loads_exact(entry["arguments"])
                    output = self._output(_omit_null_defaults(decoded, schema))
                else:
                    content = entry.get("content")
                    if (
                        entry.get("type") != "message"
                        or entry.get("role", "assistant") != "assistant"
                        or not isinstance(content, list)
                        or len(content) != 1
                        or not isinstance(content[0], dict)
                        or content[0].get("type") != "output_text"
                        or not isinstance(content[0].get("text"), str)
                    ):
                        raise ValueError
                    output = self._output(loads_exact(content[0]["text"]))
                return output
            except LLMOutputError:
                trace["source"] = "output-rejected"
                trace["rejected_candidates"] = deepcopy(trace.get("output_candidates", []))
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                trace["source"] = "output-rejected"
                trace["rejected_candidates"] = deepcopy(trace.get("output_candidates", []))
                raise LLMOutputError("Invalid model response; no replay recorded") from None
