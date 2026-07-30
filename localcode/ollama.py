from __future__ import annotations

import json
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


class OllamaError(RuntimeError):
    pass


@dataclass(slots=True)
class ModelInfo:
    name: str
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    def as_message_dict(self) -> dict[str, Any]:
        if self.raw:
            return self.raw
        return {
            "id": self.id,
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(slots=True)
class ChatResult:
    content: str
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    eval_tokens: int = 0
    done_reason: str = ""
    effective_context: int = 0
    interrupted: bool = False
    counts_exact: bool = True

    def exhausted_context(self, requested_output: int) -> bool:
        if self.effective_context <= 0:
            return False
        filled = self.prompt_tokens + self.eval_tokens >= self.effective_context - 1
        return self.done_reason == "length" and filled


ChunkCallback = Callable[[str], None]


class OllamaClient:
    def __init__(self, endpoint: str = "http://127.0.0.1:11434", timeout: int = 600) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def check(self) -> tuple[bool, str]:
        try:
            payload = self._request_json("GET", "/api/tags")
            count = len(payload.get("models") or [])
            return True, f"Ollama is available with {count} model{'s' if count != 1 else ''}."
        except OllamaError as error:
            return False, str(error)

    def list_models(self) -> list[ModelInfo]:
        payload = self._request_json("GET", "/api/tags")
        models: list[ModelInfo] = []
        for item in payload.get("models") or []:
            capabilities = [str(value) for value in item.get("capabilities") or []]
            if capabilities and "completion" not in capabilities:
                continue
            details = item.get("details") or {}
            models.append(
                ModelInfo(
                    name=str(item.get("name") or item.get("model") or ""),
                    context_length=int(details.get("context_length") or 0),
                    capabilities=capabilities,
                    parameter_size=str(details.get("parameter_size") or ""),
                    quantization=str(details.get("quantization_level") or ""),
                )
            )
        return [model for model in models if model.name]

    def show_model(self, model: str) -> ModelInfo:
        payload = self._request_json("POST", "/api/show", {"model": model, "verbose": False})
        model_info = payload.get("model_info") or {}
        architecture = str(model_info.get("general.architecture") or "")
        context_length = int(model_info.get(f"{architecture}.context_length") or 0)
        if not context_length:
            for key, value in model_info.items():
                if str(key).endswith(".context_length"):
                    try:
                        context_length = int(value)
                    except (TypeError, ValueError):
                        pass
                    break
        details = payload.get("details") or {}
        return ModelInfo(
            name=model,
            context_length=context_length,
            capabilities=[str(value) for value in payload.get("capabilities") or []],
            parameter_size=str(details.get("parameter_size") or ""),
            quantization=str(details.get("quantization_level") or ""),
        )

    def effective_context(self, model: str, fallback: int) -> int:
        try:
            payload = self._request_json("GET", "/api/ps")
        except OllamaError:
            return fallback
        wanted = model.casefold()
        for item in payload.get("models") or []:
            loaded_name = str(item.get("name") or item.get("model") or "")
            if loaded_name.casefold() == wanted:
                return int(item.get("context_length") or fallback)
        return fallback

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context_window: int,
        output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: ChunkCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,
            "options": {
                "num_ctx": max(2048, int(context_window)),
                "num_predict": max(1, int(output_tokens)),
                "temperature": 0.2,
            },
        }
        if tools:
            body["tools"] = tools

        request = self._make_request("POST", "/api/chat", body)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        final: dict[str, Any] = {}
        interrupted = False

        try:
            response = self._open_stream(request, cancel)
            if response is None:
                return ChatResult(
                    content="",
                    done_reason="cancelled",
                    effective_context=context_window,
                    interrupted=True,
                    counts_exact=False,
                )
            with response:
                watcher_stop = threading.Event()

                def interrupt_response() -> None:
                    while not watcher_stop.wait(0.1):
                        if cancel and cancel.is_set():
                            try:
                                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                            except (AttributeError, OSError):
                                pass
                            response.close()
                            return

                watcher = None
                if cancel:
                    watcher = threading.Thread(
                        target=interrupt_response,
                        name="ollama-cancel",
                        daemon=True,
                    )
                    watcher.start()
                try:
                    while True:
                        if cancel and cancel.is_set():
                            final = {"done_reason": "cancelled"}
                            interrupted = True
                            break
                        try:
                            raw_line = response.readline()
                        except Exception as error:
                            if cancel and cancel.is_set():
                                final = {"done_reason": "cancelled"}
                                interrupted = True
                                break
                            raise OllamaError(f"Ollama stream failed: {error}") from error
                        if not raw_line:
                            break
                        try:
                            event = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as error:
                            raise OllamaError(
                                f"Ollama returned invalid stream data: {error}"
                            ) from error
                        if event.get("error"):
                            raise OllamaError(str(event["error"]))
                        message = event.get("message") or {}
                        chunk = str(message.get("content") or "")
                        thinking = str(message.get("thinking") or "")
                        if chunk:
                            content_parts.append(chunk)
                            if on_chunk:
                                on_chunk(chunk)
                        if thinking:
                            thinking_parts.append(thinking)
                        for raw_call in message.get("tool_calls") or []:
                            function = raw_call.get("function") or {}
                            arguments = function.get("arguments") or {}
                            if isinstance(arguments, str):
                                try:
                                    arguments = json.loads(arguments)
                                except json.JSONDecodeError:
                                    arguments = {"input": arguments}
                            tool_calls.append(
                                ToolCall(
                                    id=str(raw_call.get("id") or f"call_{len(tool_calls)}"),
                                    name=str(function.get("name") or ""),
                                    arguments=arguments if isinstance(arguments, dict) else {},
                                    raw=raw_call,
                                )
                            )
                        if event.get("done"):
                            final = event
                            break
                finally:
                    watcher_stop.set()
                if not final:
                    final = {"done_reason": "connection_closed"}
                    interrupted = True
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                detail = str(json.loads(detail).get("error") or detail)
            except json.JSONDecodeError:
                pass
            raise OllamaError(f"Ollama HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise OllamaError(f"Cannot reach Ollama at {self.endpoint}: {error.reason}") from error
        except TimeoutError as error:
            raise OllamaError("Ollama timed out while generating a response.") from error

        effective = self.effective_context(model, context_window)
        return ChatResult(
            content="".join(content_parts),
            thinking="".join(thinking_parts),
            tool_calls=[call for call in tool_calls if call.name],
            prompt_tokens=int(final.get("prompt_eval_count") or 0),
            eval_tokens=int(final.get("eval_count") or 0),
            done_reason=str(final.get("done_reason") or ""),
            effective_context=effective,
            interrupted=interrupted,
            counts_exact=not interrupted,
        )

    def _open_stream(
        self,
        request: urllib.request.Request,
        cancel: threading.Event | None,
    ):
        if cancel is None:
            return urllib.request.urlopen(request, timeout=self.timeout)
        opened: queue.Queue[tuple[object | None, BaseException | None]] = queue.Queue(maxsize=1)

        def open_request() -> None:
            try:
                response = urllib.request.urlopen(request, timeout=self.timeout)
                if cancel.is_set():
                    response.close()
                    return
                opened.put((response, None))
            except BaseException as error:
                if not cancel.is_set():
                    opened.put((None, error))

        threading.Thread(target=open_request, name="ollama-connect", daemon=True).start()
        while not cancel.wait(0.1):
            try:
                response, error = opened.get_nowait()
            except queue.Empty:
                continue
            if error:
                raise error
            return response

        def close_late_response() -> None:
            response, _error = opened.get()
            if response is not None:
                response.close()

        threading.Thread(
            target=close_late_response,
            name="ollama-connect-cleanup",
            daemon=True,
        ).start()
        return None

    def complete(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        context_window: int,
        output_tokens: int = 2048,
        cancel: threading.Event | None = None,
    ) -> ChatResult:
        return self.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            context_window=context_window,
            output_tokens=output_tokens,
            cancel=cancel,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._make_request(method, path, body)
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 30)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise OllamaError(f"Cannot reach Ollama at {self.endpoint}: {error.reason}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OllamaError(f"Ollama returned invalid JSON: {error}") from error
        return payload if isinstance(payload, dict) else {}

    def _make_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> urllib.request.Request:
        url = urllib.parse.urljoin(f"{self.endpoint}/", path.lstrip("/"))
        data = json.dumps(body).encode("utf-8") if body is not None else None
        return urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
