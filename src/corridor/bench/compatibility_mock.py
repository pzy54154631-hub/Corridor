"""Loopback-only OpenAI-compatible protocol fixture, never a model evaluation."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from corridor.agent.llm import LLMClient, LLMResult
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact


class LocalCompatibilityMock:
    """Serve each runner's deterministic answer over the real chat HTTP protocol."""

    def __init__(self) -> None:
        self.output: dict[str, Any] | None = None
        self.request_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions" or owner.output is None:
                    self.send_error(400)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 1_048_576:
                    self.send_error(400)
                    return
                body = loads_exact(self.rfile.read(size))
                owner.request_count += 1
                output = deepcopy(owner.output)
                if "tools" in body:
                    named = set(output) == {"tool", "arguments"}
                    name = output["tool"] if named else "emit_result"
                    arguments = output["arguments"] if named else output
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_local_mock",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": jcs_bytes(arguments).decode("utf-8"),
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": jcs_bytes(output).decode("utf-8")}
                    finish = "stop"
                payload = jcs_bytes({
                    "id": "chatcmpl_local_mock",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                    # Synthetic usage exercises budget arithmetic; it is not provider billing.
                    "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
                })
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api_base = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def __enter__(self) -> "LocalCompatibilityMock":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, model: str, provider: str, replay_dir: Path) -> LLMClient:
        owner = self

        class ScriptedHTTPClient(LLMClient):
            def complete(self, **request: Any) -> LLMResult:
                owner.output = deepcopy(request["scripted"]())
                try:
                    return super().complete(**request)
                finally:
                    owner.output = None

        return ScriptedHTTPClient(
            mode="record",
            model=model,
            provider=provider,
            api="chat",
            api_base=self.api_base,
            local_mock=True,
            replay_dir=replay_dir,
            api_key="synthetic-local-protocol-credential",
            env_file=None,
            max_calls=1000,
            max_usd="10",
            assumed_price_in="10",
            assumed_price_out="40",
        )
