from __future__ import annotations

import threading
from typing import Any, Callable

from .ollama import OllamaClient
from .providers import OpenAIClient, ProviderChatResult, ProviderToolCall
from .settings import AppSettings


class BackendError(RuntimeError):
    pass


def resolve_backend(
    model: str,
    settings: AppSettings,
) -> tuple[object, str, int]:
    providers = settings.database.list_providers()
    for provider in providers:
        if model.startswith(f"{provider.name}/") or model == provider.name:
            actual_model = model.removeprefix(f"{provider.name}/")
            client = OpenAIClient(provider.endpoint, provider.api_key)
            context = provider.default_context_window
            return client, actual_model, context

    return OllamaClient(settings.ollama_url), model, settings.default_context_window


def run_chat(
    model: str,
    settings: AppSettings,
    *,
    messages: list[dict[str, Any]],
    context_window: int,
    output_tokens: int,
    tools: list[dict[str, Any]] | None = None,
    on_chunk: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> ProviderChatResult:
    client, actual_model, provider_context = resolve_backend(model, settings)
    effective_context = context_window or provider_context

    if isinstance(client, OllamaClient):
        result = client.chat(
            model=actual_model,
            messages=messages,
            context_window=effective_context,
            output_tokens=output_tokens,
            tools=tools,
            on_chunk=on_chunk,
            cancel=cancel,
        )
        return ProviderChatResult(
            content=result.content,
            thinking=result.thinking,
            tool_calls=[
                ProviderToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    raw=call.raw,
                )
                for call in result.tool_calls
            ],
            prompt_tokens=result.prompt_tokens,
            eval_tokens=result.eval_tokens,
            done_reason=result.done_reason,
            effective_context=result.effective_context or effective_context,
            interrupted=result.interrupted,
            counts_exact=result.counts_exact,
        )
    elif isinstance(client, OpenAIClient):
        result = client.chat(
            model=actual_model,
            messages=messages,
            context_window=effective_context,
            output_tokens=output_tokens,
            tools=tools,
            on_chunk=on_chunk,
            cancel=cancel,
        )
        result.effective_context = effective_context
        return result

    raise BackendError(f"No backend for model: {model}")


def run_complete(
    model: str,
    settings: AppSettings,
    *,
    system: str,
    prompt: str,
    context_window: int,
    output_tokens: int = 2048,
    cancel: threading.Event | None = None,
) -> ProviderChatResult:
    return run_chat(
        model=model,
        settings=settings,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        context_window=context_window,
        output_tokens=output_tokens,
        cancel=cancel,
    )


def show_model_info(
    model: str,
    settings: AppSettings,
) -> tuple[int, bool]:
    client, actual_model, provider_context = resolve_backend(model, settings)
    if isinstance(client, OllamaClient):
        info = client.show_model(actual_model)
        return info.context_length or provider_context, True
    return provider_context, True
