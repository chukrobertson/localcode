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

from .models import Provider


class ProviderError(RuntimeError):
    pass


class _StreamOptionsRejected(ProviderError):
    """The endpoint rejected the usage-requesting stream options."""


@dataclass(slots=True)
class ProviderModelInfo:
    provider_id: str
    provider_label: str
    name: str
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""
    model_id: str = ""

    def display_name(self) -> str:
        if self.provider_label == "Ollama":
            return self.name
        return f"{self.name} ({self.provider_label})"


@dataclass(slots=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    def as_message_dict(self) -> dict[str, Any]:
        if self.raw:
            return self.raw
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }


@dataclass(slots=True)
class ProviderChatResult:
    content: str
    thinking: str = ""
    tool_calls: list[ProviderToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    eval_tokens: int = 0
    done_reason: str = ""
    effective_context: int = 0
    interrupted: bool = False
    counts_exact: bool = True
    usage_unavailable: bool = False

    def exhausted_context(self, requested_output: int) -> bool:
        if self.effective_context <= 0:
            return False
        filled = self.prompt_tokens + self.eval_tokens >= self.effective_context - 1
        return self.done_reason == "length" and filled


class OpenAIClient:
    def __init__(self, endpoint: str, api_key: str, timeout: int = 600) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def list_models(self) -> list[str]:
        payload = self._request_json("GET", "/v1/models")
        models: list[str] = []
        for item in payload.get("data") or []:
            model_id = str(item.get("id", ""))
            if model_id:
                models.append(model_id)
        return models

    @staticmethod
    def _normalize_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Translate LocalCode's canonical tool messages to OpenAI chat format."""

        normalized: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "tool":
                tool_message: dict[str, Any] = {
                    "role": "tool",
                    "content": str(message.get("content") or ""),
                }
                call_id = str(message.get("tool_call_id") or "")
                if call_id:
                    tool_message["tool_call_id"] = call_id
                normalized.append(tool_message)
                continue

            copied = dict(message)
            if copied.get("role") == "assistant" and copied.get("tool_calls"):
                tool_calls: list[dict[str, Any]] = []
                for raw_call in copied["tool_calls"]:
                    call = dict(raw_call)
                    function = dict(call.get("function") or {})
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        function["arguments"] = json.dumps(
                            arguments, separators=(",", ":")
                        )
                    call["function"] = function
                    call.setdefault("type", "function")
                    tool_calls.append(call)
                copied["tool_calls"] = tool_calls
            normalized.append(copied)
        return normalized

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context_window: int,
        output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> ProviderChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._normalize_messages(messages),
            "stream": True,
            "max_tokens": max(1, int(output_tokens)),
            "temperature": 0.2,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools

        try:
            return self._chat_stream(body, context_window, on_chunk, cancel)
        except _StreamOptionsRejected:
            body.pop("stream_options", None)
            return self._chat_stream(body, context_window, on_chunk, cancel)

    def _chat_stream(
        self,
        body: dict[str, Any],
        context_window: int,
        on_chunk: Callable[[str], None] | None,
        cancel: threading.Event | None,
    ) -> ProviderChatResult:
        request = self._make_request("POST", "/v1/chat/completions", body)
        content_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        done_reason = ""
        interrupted = False
        received_data = False

        try:
            response = self._open_stream(request, cancel)
            if response is None:
                return ProviderChatResult(
                    content="",
                    done_reason="cancelled",
                    effective_context=context_window,
                    interrupted=True,
                    counts_exact=False,
                    usage_unavailable=True,
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
                        name="api-cancel",
                        daemon=True,
                    )
                    watcher.start()
                try:
                    while True:
                        if cancel and cancel.is_set():
                            response.close()
                            interrupted = True
                            done_reason = "cancelled"
                            break
                        try:
                            raw_line = response.readline()
                        except Exception as error:
                            if cancel and cancel.is_set():
                                interrupted = True
                                done_reason = "cancelled"
                                break
                            raise ProviderError(f"API stream failed: {error}") from error
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        if line.startswith(":"):
                            continue
                        if not line.startswith("data: "):
                            continue
                        received_data = True
                        data = line[6:].strip()
                        if data == "[DONE]":
                            if not done_reason:
                                done_reason = "stop"
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        event_usage = event.get("usage")
                        if isinstance(event_usage, dict) and not usage:
                            usage = event_usage
                        choice = (event.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        finish_reason = choice.get("finish_reason")
                        if finish_reason:
                            done_reason = finish_reason
                        chunk = str(delta.get("content") or "")
                        if chunk:
                            content_parts.append(chunk)
                            if on_chunk:
                                on_chunk(chunk)
                        for tc_delta in delta.get("tool_calls") or []:
                            idx = int(tc_delta.get("index", 0))
                            if idx not in tool_call_parts:
                                tool_call_parts[idx] = {
                                    "id": tc_delta.get("id") or "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            existing = tool_call_parts[idx]
                            if tc_delta.get("id"):
                                existing["id"] = tc_delta["id"]
                            func = tc_delta.get("function") or {}
                            if func.get("name"):
                                existing["function"]["name"] += func["name"]
                            if func.get("arguments"):
                                existing["function"]["arguments"] += func["arguments"]
                        if finish_reason:
                            break
                finally:
                    watcher_stop.set()
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            finally:
                error.close()
            try:
                detail = str(json.loads(detail).get("error", {}).get("message", detail))
            except json.JSONDecodeError:
                pass
            if error.code == 400 and "stream_options" in body:
                raise _StreamOptionsRejected(f"API HTTP 400: {detail}") from error
            raise ProviderError(f"API HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProviderError(
                f"Cannot reach API at {self.endpoint}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise ProviderError("API timed out while generating.") from error

        if interrupted:
            return ProviderChatResult(
                content="".join(content_parts),
                done_reason=done_reason or "cancelled",
                effective_context=context_window,
                interrupted=True,
                counts_exact=False,
                usage_unavailable=not usage,
            )
        if not received_data:
            raise ProviderError(
                f"The API at {self.endpoint} returned an empty or unusable stream."
            )
        if not done_reason:
            done_reason = "connection_closed"
            interrupted = True

        tool_calls: list[ProviderToolCall] = []
        for tc_entry in tool_call_parts.values():
            func = tc_entry.get("function") or {}
            arguments_str = func.get("arguments", "{}")
            try:
                arguments = json.loads(arguments_str)
                if not isinstance(arguments, dict):
                    arguments = {"input": arguments_str}
            except json.JSONDecodeError:
                arguments = {"input": arguments_str}
            tool_calls.append(
                ProviderToolCall(
                    id=tc_entry.get("id", ""),
                    name=func.get("name", ""),
                    arguments=arguments,
                    raw=tc_entry,
                )
            )

        usage_available = bool(usage)
        return ProviderChatResult(
            content="".join(content_parts),
            tool_calls=[call for call in tool_calls if call.name],
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            eval_tokens=int(usage.get("completion_tokens") or 0),
            done_reason=done_reason,
            effective_context=context_window,
            interrupted=interrupted,
            counts_exact=usage_available and not interrupted,
            usage_unavailable=not usage_available,
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

        threading.Thread(target=open_request, name="api-connect", daemon=True).start()
        while not cancel.wait(0.1):
            try:
                response, error = opened.get_nowait()
            except queue.Empty:
                continue
            if error:
                raise error
            return response

        threading.Thread(
            target=self._close_late_response,
            args=(opened,),
            name="api-connect-cleanup",
            daemon=True,
        ).start()
        return None

    @staticmethod
    def _close_late_response(opened: queue.Queue) -> None:
        response, _error = opened.get()
        if response is not None:
            response.close()

    def _request_json(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = self._make_request(method, path, body)
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 30)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            finally:
                error.close()
            raise ProviderError(f"API HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProviderError(
                f"Cannot reach API at {self.endpoint}: {error.reason}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderError(f"API returned invalid JSON: {error}") from error
        return payload if isinstance(payload, dict) else {}

    def _make_request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> urllib.request.Request:
        url = urllib.parse.urljoin(f"{self.endpoint}/", path.lstrip("/"))
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(url, data=data, method=method, headers=headers)


def discover_all_models(
    ollama_client, settings, providers: list[Provider]
) -> list[ProviderModelInfo]:
    models: list[ProviderModelInfo] = []

    for model in ollama_client.list_models():
        models.append(
            ProviderModelInfo(
                provider_id="ollama",
                provider_label="Ollama",
                name=model.name,
                context_length=model.context_length,
                capabilities=model.capabilities,
                parameter_size=model.parameter_size,
                quantization=model.quantization,
                model_id=model.name,
            )
        )

    for provider in providers:
        try:
            client = OpenAIClient(provider.endpoint, provider.api_key)
            names = client.list_models()
        except ProviderError:
            continue
        for name in names:
            models.append(
                ProviderModelInfo(
                    provider_id=provider.id,
                    provider_label=provider.name,
                    name=name,
                    context_length=provider.default_context_window,
                    model_id=f"{provider.name}/{name}",
                )
            )

    return models
