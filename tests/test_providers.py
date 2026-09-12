from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localcode.providers import OpenAIClient, ProviderError


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        marker = ""
        for message in body.get("messages", []):
            marker = str(message.get("content") or "")
        requested_usage = "stream_options" in body

        if marker == "reject-stream-options" and requested_usage:
            self._error(400, "stream_options is not supported by this endpoint")
            return
        if marker == "auth-fail":
            self._error(401, "invalid api key")
            return
        if marker == "hang-connect":
            time.sleep(5)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.flush()

        if marker == "empty":
            return
        if marker == "garbage":
            self.wfile.write(b"this is not server-sent events at all\n")
            return
        if marker == "hang-stream":
            self._sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
            self.wfile.flush()
            time.sleep(5)
            return
        if marker == "cut":
            self._sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
            self.wfile.flush()
            return
        if marker == "mixed":
            self.wfile.write(b"garbage without a data prefix\n")
            self._sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]})
            self._sse(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 33, "completion_tokens": 2},
                }
            )
            self._done()
            return
        if marker == "usage-late":
            self._sse({"choices": [{"delta": {"content": "late"}, "finish_reason": None}]})
            self._sse({"choices": [], "usage": {"prompt_tokens": 55, "completion_tokens": 4}})
            self._done()
            return
        if marker == "no-usage":
            self._sse({"choices": [{"delta": {"content": "plain"}, "finish_reason": "stop"}]})
            self._done()
            return
        if marker == "tools":
            self._sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "write_file", "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
            self._sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": '{"path": "a.txt", "content": "hi"}'
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
            self._sse(
                {
                    "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 12},
                }
            )
            return

        self._sse({"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]})
        self._sse({"choices": [{"delta": {"content": "world"}, "finish_reason": None}]})
        self._sse(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8},
            }
        )
        self._done()

    def _sse(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _error(self, code: int, message: str) -> None:
        data = json.dumps({"error": {"message": message}}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args) -> None:
        pass


class OpenAIClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.client = OpenAIClient(f"http://{host}:{port}", api_key="test-key")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _chat(self, marker: str, **kwargs):
        return self.client.chat(
            model="test-model",
            messages=[{"role": "user", "content": marker}],
            context_window=8192,
            output_tokens=256,
            **kwargs,
        )

    def test_streams_and_returns_exact_usage(self) -> None:
        chunks: list[str] = []
        result = self._chat("normal", on_chunk=chunks.append)
        self.assertEqual(result.content, "hello world")
        self.assertEqual(chunks, ["hello ", "world"])
        self.assertEqual(result.prompt_tokens, 120)
        self.assertEqual(result.eval_tokens, 8)
        self.assertTrue(result.counts_exact)
        self.assertFalse(result.usage_unavailable)
        self.assertEqual(result.done_reason, "stop")

    def test_usage_chunk_before_done_still_counts_as_exact(self) -> None:
        result = self._chat("usage-late")
        self.assertEqual(result.content, "late")
        self.assertEqual(result.prompt_tokens, 55)
        self.assertEqual(result.eval_tokens, 4)
        self.assertTrue(result.counts_exact)
        self.assertEqual(result.done_reason, "stop")

    def test_missing_usage_is_never_reported_as_exact_zero(self) -> None:
        result = self._chat("no-usage")
        self.assertEqual(result.content, "plain")
        self.assertTrue(result.usage_unavailable)
        self.assertFalse(result.counts_exact)
        self.assertEqual(result.prompt_tokens, 0)

    def test_empty_response_body_raises_provider_error(self) -> None:
        with self.assertRaises(ProviderError) as context:
            self._chat("empty")
        self.assertIn("empty", str(context.exception).casefold())

    def test_stream_without_sse_records_raises_provider_error(self) -> None:
        with self.assertRaises(ProviderError):
            self._chat("garbage")

    def test_malformed_sse_lines_are_skipped(self) -> None:
        result = self._chat("mixed")
        self.assertEqual(result.content, "ok")
        self.assertTrue(result.counts_exact)

    def test_stream_cut_before_finish_is_interrupted_with_partial_content(self) -> None:
        result = self._chat("cut")
        self.assertTrue(result.interrupted)
        self.assertEqual(result.content, "partial")
        self.assertEqual(result.done_reason, "connection_closed")
        self.assertTrue(result.usage_unavailable)

    def test_tool_call_deltas_are_assembled(self) -> None:
        result = self._chat("tools")
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.name, "write_file")
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.arguments, {"path": "a.txt", "content": "hi"})
        self.assertEqual(result.done_reason, "tool_calls")

    def test_normalizes_canonical_tool_messages_for_openai(self) -> None:
        messages = OpenAIClient._normalize_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_7",
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "main.py"},
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_7",
                    "tool_name": "read_file",
                    "content": "verified",
                },
            ]
        )
        arguments = messages[0]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments), {"path": "main.py"})
        self.assertEqual(
            messages[1],
            {"role": "tool", "tool_call_id": "call_7", "content": "verified"},
        )

    def test_retries_without_stream_options_when_rejected(self) -> None:
        result = self._chat("reject-stream-options")
        self.assertEqual(result.content, "hello world")
        self.assertTrue(result.counts_exact)

    def test_auth_failure_raises_provider_error(self) -> None:
        with self.assertRaises(ProviderError) as context:
            self._chat("auth-fail")
        self.assertIn("401", str(context.exception))

    def test_cancel_interrupts_streaming(self) -> None:
        cancel = threading.Event()
        results = []
        worker = threading.Thread(
            target=lambda: results.append(self._chat("hang-stream", cancel=cancel))
        )
        worker.start()
        time.sleep(0.3)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].interrupted)
        self.assertEqual(results[0].done_reason, "cancelled")
        self.assertEqual(results[0].content, "partial")

    def test_cancel_interrupts_wait_for_response_headers(self) -> None:
        cancel = threading.Event()
        results = []
        worker = threading.Thread(
            target=lambda: results.append(self._chat("hang-connect", cancel=cancel))
        )
        worker.start()
        time.sleep(0.3)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].interrupted)
        self.assertEqual(results[0].done_reason, "cancelled")


if __name__ == "__main__":
    unittest.main()
