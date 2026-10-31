"""Resource stops and strict replay of complete observed episode prefixes."""

from copy import deepcopy
from pathlib import Path
from time import monotonic_ns

from corridor.agent.llm import (
    RESPONSE_DIAGNOSTIC_FIELDS,
    BudgetExceeded,
    LLMOutputError,
    ReplayMiss,
)
from corridor.crypto.jcs import H, jcs_bytes
from corridor.schemas.base import loads_exact

EPISODE_LIMITS = {"user_turns": 8, "tool_calls": 16, "seconds": 600}


def safe_traceback(exc):
    """Keep every frame and chained cause without local values or exception text.

    Messages, source lines and notes can contain credentials or frozen case data.
    Repository frames retain relative code paths; all other paths are redacted.
    No source file is opened while formatting these diagnostics.
    """
    root = str(Path(__file__).resolve().parents[3]).replace("\\", "/") + "/"
    seen = set()

    def render(error):
        if id(error) in seen:
            return "[exception cycle omitted]\n"
        seen.add(id(error))
        parts = []
        if error.__cause__ is not None:
            parts.append(render(error.__cause__))
            parts.append(
                "\nThe above exception was the direct cause of the following exception:\n\n"
            )
        elif error.__context__ is not None and not error.__suppress_context__:
            parts.append(render(error.__context__))
            parts.append(
                "\nDuring handling of the above exception, another exception occurred:\n\n"
            )
        parts.append("Traceback (most recent call last):\n")
        frame = error.__traceback__
        if frame is None:
            parts.append("  [no Python traceback available]\n")
        while frame is not None:
            code = frame.tb_frame.f_code
            path = code.co_filename.replace("\\", "/")
            path = (
                "<repo>/" + path[len(root) :]
                if path.casefold().startswith(root.casefold())
                else "<external>"
            )
            parts.append(f'  File "{path}", line {frame.tb_lineno}, in {code.co_name}\n')
            frame = frame.tb_next
        parts.append(type(error).__name__ + ": [message redacted]\n")
        if isinstance(error, BaseExceptionGroup):
            for index, child in enumerate(error.exceptions, 1):
                parts.append(f"\nException group member {index}:\n" + render(child))
        return "".join(parts)

    return render(exc)


class EpisodeLimit(RuntimeError):
    def __init__(self, limit):
        super().__init__("Fixed episode resource limit reached: " + limit)
        self.limit = limit


class EpisodeReplayMismatch(RuntimeError):
    """A missing or changed observation must never pass as a recorded stop."""


class RecordedEpisodeStop(RuntimeError):
    def __init__(self, terminal):
        super().__init__("Reproduced recorded episode termination")
        self.terminal = deepcopy(terminal)


def error_class(exc, trace=None):
    if isinstance(exc, (EpisodeLimit, BudgetExceeded)):
        return "local_resource"
    trace = trace or {}
    names = {type(exc).__name__, trace.get("error_type"), *trace.get("error_cause_types", [])}
    if "BudgetExceeded" in names:
        return "local_resource"
    network_names = {
        "ConnectionError",
        "APIConnectionError",
        "APITimeoutError",
        "TimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RateLimitError",
    }
    if (
        names & network_names
        or trace.get("http_status") == 429
        or trace.get("provider_error_code") in {"insufficient_quota", "rate_limit_exceeded"}
    ):
        return "network_quota"
    if (
        names & {"AuthenticationError", "PermissionDeniedError"}
        or trace.get("http_status") in {401, 403}
        or trace.get("provider_error_code") in {"invalid_api_key", "permission_denied"}
    ):
        # A rejected provider credential/permission is not a returned model candidate.
        return "apparatus_defect"
    if isinstance(exc, LLMOutputError):
        return "model_output_format"
    return "apparatus_defect"


class EpisodeControl:
    """Bench-only instrumentation; successful model observations still use LLM strict replay.

    Checkpoints bind a prefix to its exact resource stop. A failed model request is
    rebuilt by LLMClient in strict mode, including context guards and observers;
    only its verified replay miss can stand for the recorded failed request.
    """

    def __init__(
        self,
        client,
        metrics,
        replay_observation=None,
        limits=None,
        clock=monotonic_ns,
        evidence_dir=None,
    ):
        self.client, self.metrics, self.clock = client, metrics, clock
        self.start = clock()
        self.limits = dict(limits or EPISODE_LIMITS)
        self.expected = None
        if replay_observation is not None:
            if client.mode != "strict":
                raise EpisodeReplayMismatch("Recorded prefixes require strict mode")
            self.expected = deepcopy(replay_observation.get("replay_control"))
            if (
                not isinstance(self.expected, dict)
                or self.expected.get("schema") != "episode-control-v1"
            ):
                raise EpisodeReplayMismatch("Missing recorded episode control")
            if self.expected.get("limits") != self.limits:
                raise EpisodeReplayMismatch("Recorded resource limits changed")
        self.data = {
            "schema": "episode-control-v1",
            "limits": self.limits,
            "checkpoints": [],
            "terminal": None,
            "model_failures": [],
        }
        self.path = Path(evidence_dir) / "control.json" if evidence_dir is not None else None
        self.original = None
        self.active_model = False
        self.pending_model = None
        self.output_observations = []
        self.call_start = len(client.calls)

    def _persist(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_bytes(jcs_bytes({**self.data, "in_flight_model": self.pending_model}))
            temporary.replace(self.path)

    def checkpoint(self, label, **details):
        point = {"label": label, **details}
        index = len(self.data["checkpoints"])
        if self.expected is not None:
            points = self.expected["checkpoints"]
            if index >= len(points) or points[index] != point:
                raise EpisodeReplayMismatch("Recorded episode checkpoint differs")
        self.data["checkpoints"].append(point)
        self._persist()
        terminal = self.expected.get("terminal") if self.expected is not None else None
        if terminal and terminal["position"] == index + 1 and terminal["stage"] != "model":
            raise RecordedEpisodeStop(terminal)

    def check_limits(self, label, *, user_turn=False, tool_call=False):
        self.checkpoint(label)
        if user_turn and self.metrics.get("user_turns", 0) >= self.limits["user_turns"]:
            raise EpisodeLimit("user_turns")
        if tool_call and self.metrics.get("tool_calls", 0) >= self.limits["tool_calls"]:
            raise EpisodeLimit("tool_calls")
        self._check_time()

    def _check_time(self):
        # Replay follows recorded observation positions, never the replay machine's speed.
        if (
            self.expected is None
            and self.clock() - self.start >= self.limits["seconds"] * 1_000_000_000
        ):
            raise EpisodeLimit("timeout")

    def attach(self):
        self.original = self.client.complete
        self.client.complete = self._complete
        self.client.observers.insert(0, self._observe_request)
        self._persist()

    def close(self):
        if self.original is not None:
            self.client.complete = self.original
            self.original = None
            self.client.observers.remove(self._observe_request)

    def _observe_request(self, stage, value):
        if stage == "output" and self.pending_model is not None:
            self.output_observations.append({"stage": stage, "value": deepcopy(value)})
        if stage == "request" and self.pending_model is not None and self.client.calls:
            trace = self.client.calls[-1]
            self.pending_model.update(
                request_hash=trace["request_hash"],
                call_count=1,
                replay_file=str(
                    self.client.replay_dir
                    / trace["replay_key"][:16]
                    / (trace["prompt_hash"][:16] + ".json")
                ),
            )
            self._persist()

    def _complete(self, **kwargs):
        request_hash = H({key: value for key, value in kwargs.items() if key != "scripted"})
        self.checkpoint("model.before", arguments_hash=request_hash)
        self._check_time()
        count = len(self.client.calls)
        self.active_model = True
        self.output_observations = []
        self.pending_model = {
            "position": len(self.data["checkpoints"]),
            "arguments_hash": request_hash,
            "request_hash": None,
            "call_count": 0,
        }
        self._persist()
        timeout = self.client.request_timeout_seconds
        if self.expected is None:
            remaining = self.limits["seconds"] - (self.clock() - self.start) // 1_000_000_000
            self.client.request_timeout_seconds = min(timeout, max(1, remaining))
        try:
            assert self.original is not None
            result = self.original(**kwargs)
        except Exception as exc:
            traces = self.client.calls[count:]
            trace = traces[-1] if traces else {}
            failure = {
                "position": len(self.data["checkpoints"]),
                "arguments_hash": request_hash,
                "request_hash": trace.get("request_hash"),
                "call_count": len(traces),
            }
            terminal = self.expected.get("terminal") if self.expected is not None else None
            if terminal and terminal["stage"] == "model":
                expected_failure = self.expected["model_failures"][-1]
                expected_identity = {
                    key: value
                    for key, value in expected_failure.items()
                    if key not in {"diagnostics", "output_observations"}
                }
                if (
                    not isinstance(exc, ReplayMiss) and type(exc).__name__ != terminal["error_type"]
                ) or failure != expected_identity:
                    raise EpisodeReplayMismatch("Failed model request prefix differs") from None
                self.data["model_failures"].append(deepcopy(expected_failure))
                for event in expected_failure.get("output_observations", []):
                    self.client._observe(event["stage"], deepcopy(event["value"]))
                if trace:
                    trace.update(deepcopy(expected_failure.get("diagnostics", {})))
                    trace["source"] = "replay-error"
                raise RecordedEpisodeStop(terminal) from None
            failure["diagnostics"] = {
                key: deepcopy(value)
                for key, value in trace.items()
                if key
                in RESPONSE_DIAGNOSTIC_FIELDS
                | {
                    "error_stage",
                    "error_type",
                    "error_cause_types",
                    "http_status",
                    "provider_error_code",
                    "provider_error_type",
                    "provider_error_param",
                    "envelope_error_reason",
                    "envelope_float_paths",
                    "response_shape",
                }
            }
            failure["output_observations"] = deepcopy(self.output_observations)
            self.data["model_failures"].append(failure)
            self._persist()
            if self.expected is None and self.clock() - self.start >= (
                self.limits["seconds"] * 1_000_000_000
            ):
                raise EpisodeLimit("timeout") from None
            raise
        else:
            self.active_model = False
            self.checkpoint("model.after", arguments_hash=request_hash)
            self._check_time()
            return result
        finally:
            self.client.request_timeout_seconds = timeout
            self.pending_model = None
            self._persist()

    def capture(self, exc):
        if isinstance(exc, RecordedEpisodeStop):
            terminal = exc.terminal
            if terminal.get("model_output_diagnostics") and self.client.calls:
                self.client.calls[-1].update(deepcopy(terminal["model_output_diagnostics"]))
        else:
            trace = (
                self.client.calls[-1]
                if self.active_model and len(self.client.calls) > self.call_start
                else {}
            )
            classified = error_class(exc, trace)
            if not self.active_model and classified == "network_quota":
                classified = "apparatus_defect"
            resource = classified == "local_resource"
            terminal = {
                "position": len(self.data["checkpoints"]),
                "stage": "model" if self.active_model else "episode",
                "status": "truncated" if resource else "runtime_error",
                "reason": exc.limit
                if isinstance(exc, EpisodeLimit)
                else "local_budget"
                if resource
                else type(exc).__name__,
                "error_class": classified,
                "error_type": type(exc).__name__,
            }
            if isinstance(exc, LLMOutputError) and len(self.client.calls) > self.call_start:
                diagnostics = {
                    key: deepcopy(value)
                    for key, value in self.client.calls[-1].items()
                    if key in RESPONSE_DIAGNOSTIC_FIELDS
                }
                if diagnostics:
                    terminal["model_output_diagnostics"] = diagnostics
            if classified == "apparatus_defect":
                terminal["traceback"] = safe_traceback(exc)
        self.data["terminal"] = terminal
        self._persist()
        result = {
            "status": terminal["status"],
            "completed": False,
            "truncated": terminal["status"] == "truncated",
            "error_class": terminal["error_class"],
            "error_type": terminal["error_type"],
            "terminal_error": {"type": terminal["error_type"], "message": terminal["reason"]},
        }
        if result["truncated"]:
            result.update(termination="budget_truncated", truncation_limit=terminal["reason"])
        if "traceback" in terminal:
            result["traceback"] = terminal["traceback"]
        return result

    def finish(self):
        if self.expected is not None and self.data != self.expected:
            raise EpisodeReplayMismatch("Recorded episode prefix was not completely replayed")
        self._persist()
        return deepcopy(self.data)


def finish_interrupted_episode(control_path, reason, error=None):
    """Seal the durable checkpoint prefix after an externally interrupted worker.

    An in-flight tool can have unexported effects; callers must keep it unresolved
    until reconstructed business observations can be independently compared.
    """
    data = loads_exact(Path(control_path).read_bytes())
    pending = data.pop("in_flight_model", None)
    stage = "episode"
    if pending and pending.get("request_hash"):
        replay_file = pending.pop("replay_file", None)
        if replay_file and Path(replay_file).is_file():
            # A completed response may have reached its cache immediately before
            # the worker died. Its strict read is still mandatory.
            data["checkpoints"].append({
                "label": "model.after",
                "arguments_hash": pending["arguments_hash"],
            })
        else:
            pending["diagnostics"] = {}
            data["model_failures"].append(pending)
            stage = "model"
    data["terminal"] = {
        "position": len(data["checkpoints"]),
        "stage": stage,
        "status": "truncated" if reason == "timeout" else "runtime_error",
        "reason": reason,
        "error_class": "local_resource" if reason == "timeout" else "apparatus_defect",
        "error_type": "EpisodeLimit" if reason == "timeout" else "WorkerInterrupted",
    }
    if reason != "timeout":
        # A killed worker's original stack cannot be recovered. Preserve an
        # available parent exception, or explicitly record the unavailable stack.
        data["terminal"]["traceback"] = safe_traceback(error or RuntimeError(reason))
        data["terminal"]["original_worker_traceback_available"] = False
    return {"replay_control": data, "interrupted_prefix": True}
