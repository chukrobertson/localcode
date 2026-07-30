from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import Provider


class ProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class ProviderModelInfo:
    provider_id: str
    provider_label: str
    name: str
    context_length: int = 0
    capabilities: list[str] = field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""

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
            "messages": messages,
            "stream": True,
            "max_tokens": max(1, int(output_tokens)),
            "temperature": 0.2,
        }
        if tools:
            body["tools"] = tools

        request = self._make_request("POST", "/v1/chat/completions", body)
        content_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        final: dict[str, Any] = {}
        interrupted = False

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                while True:
                    if cancel and cancel.is_set():
                        response.close()
                        final = {"done_reason": "cancelled"}
                        interrupted = True
                        break
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason")
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
                        final = event
                        if "usage" in event:
                            usage = event["usage"]
                            final["prompt_eval_count"] = usage.get("prompt_tokens", 0)
                            final["eval_count"] = usage.get("completion_tokens", 0)
                        else:
                            final["prompt_eval_count"] = 0
                            final["eval_count"] = 0
                        break
                if not final:
                    final = {"done_reason": "connection_closed"}
                    interrupted = True
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                detail = str(json.loads(detail).get("error", {}).get("message", detail))
            except json.JSONDecodeError:
                pass
            raise ProviderError(f"API HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProviderError(
                f"Cannot reach API at {self.endpoint}: {error.reason}"
            ) from error
        except TimeoutError as error:
            raise ProviderError("API timed out while generating.") from error

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

        return ProviderChatResult(
            content="".join(content_parts),
            tool_calls=[call for call in tool_calls if call.name],
            prompt_tokens=int(final.get("prompt_eval_count") or 0),
            eval_tokens=int(final.get("eval_count") or 0),
            done_reason=str(final.get("done_reason") or str(finish_reason or "stop")),
            effective_context=context_window,
            interrupted=interrupted,
            counts_exact=True,
        )

    def _request_json(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = self._make_request(method, path, body)
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 30)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
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
                )
            )

    return models
