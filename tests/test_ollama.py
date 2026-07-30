from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localcode.ollama import ChatResult, OllamaClient


class OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/tags":
            self._json(
                {
                    "models": [
                        {
                            "name": "code:test",
                            "capabilities": ["completion", "tools"],
                            "details": {"parameter_size": "1B", "quantization_level": "Q4"},
                        },
                        {"name": "embed:test", "capabilities": ["embedding"], "details": {}},
                    ]
                }
            )
        elif self.path == "/api/ps":
            self._json({"models": [{"name": "code:test", "context_length": 8192}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/show":
            self._json(
                {
                    "model_info": {
                        "general.architecture": "testarch",
                        "testarch.context_length": 16384,
                    },
                    "details": {"parameter_size": "1B", "quantization_level": "Q4"},
                    "capabilities": ["completion", "tools"],
                }
            )
            return
        if self.path == "/api/chat":
            if any(
                message.get("content") == "headers-hang"
                for message in body.get("messages", [])
            ):
                time.sleep(5)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            if any(message.get("content") == "hang" for message in body.get("messages", [])):
                self.wfile.flush()
                time.sleep(5)
                return
            if any(message.get("content") == "interrupt" for message in body.get("messages", [])):
                self.wfile.write(
                    json.dumps(
                        {"message": {"role": "assistant", "content": "partial"}, "done": False}
                    ).encode("utf-8")
                    + b"\n"
                )
                return
            events = [
                {"message": {"role": "assistant", "content": "hello "}, "done": False},
                {"message": {"role": "assistant", "content": "world"}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 100,
                    "eval_count": 2,
                },
            ]
            for event in events:
                self.wfile.write(json.dumps(event).encode("utf-8") + b"\n")
            return
        self.send_error(404)

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args) -> None:
        pass


class OllamaClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.client = OllamaClient(f"http://{host}:{port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_lists_only_completion_models(self) -> None:
        self.assertEqual([model.name for model in self.client.list_models()], ["code:test"])

    def test_extracts_dynamic_context_key(self) -> None:
        info = self.client.show_model("code:test")
        self.assertEqual(info.context_length, 16384)
        self.assertIn("tools", info.capabilities)

    def test_streams_and_returns_exact_counts(self) -> None:
        chunks: list[str] = []
        result = self.client.chat(
            model="code:test",
            messages=[{"role": "user", "content": "hello"}],
            context_window=8192,
            output_tokens=100,
            on_chunk=chunks.append,
        )
        self.assertEqual(result.content, "hello world")
        self.assertEqual(chunks, ["hello ", "world"])
        self.assertEqual(result.prompt_tokens, 100)
        self.assertEqual(result.effective_context, 8192)

    def test_eof_without_done_is_an_interrupted_result(self) -> None:
        result = self.client.chat(
            model="code:test",
            messages=[{"role": "user", "content": "interrupt"}],
            context_window=8192,
            output_tokens=100,
        )
        self.assertTrue(result.interrupted)
        self.assertFalse(result.counts_exact)
        self.assertEqual(result.content, "partial")
        self.assertEqual(result.done_reason, "connection_closed")

    def test_context_exhaustion_requires_length_at_effective_limit(self) -> None:
        exhausted = ChatResult(
            "",
            prompt_tokens=2037,
            eval_tokens=11,
            done_reason="length",
            effective_context=2048,
        )
        capped = ChatResult(
            "",
            prompt_tokens=100,
            eval_tokens=16,
            done_reason="length",
            effective_context=2048,
        )
        self.assertTrue(exhausted.exhausted_context(16))
        self.assertFalse(capped.exhausted_context(16))

    def test_cancel_interrupts_a_blocked_stream_read(self) -> None:
        cancel = threading.Event()
        results: list[ChatResult] = []
        worker = threading.Thread(
            target=lambda: results.append(
                self.client.chat(
                    model="code:test",
                    messages=[{"role": "user", "content": "hang"}],
                    context_window=8192,
                    output_tokens=100,
                    cancel=cancel,
                )
            )
        )
        worker.start()
        time.sleep(0.2)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].interrupted)
        self.assertEqual(results[0].done_reason, "cancelled")

    def test_cancel_interrupts_wait_for_response_headers(self) -> None:
        cancel = threading.Event()
        results: list[ChatResult] = []
        worker = threading.Thread(
            target=lambda: results.append(
                self.client.chat(
                    model="code:test",
                    messages=[{"role": "user", "content": "headers-hang"}],
                    context_window=8192,
                    output_tokens=100,
                    cancel=cancel,
                )
            )
        )
        worker.start()
        time.sleep(0.2)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].interrupted)
        self.assertEqual(results[0].done_reason, "cancelled")


if __name__ == "__main__":
    unittest.main()
