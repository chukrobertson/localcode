from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcode.backend import resolve_backend
from localcode.database import Database
from localcode.ollama import OllamaClient
from localcode.providers import OpenAIClient
from localcode.settings import AppSettings


class ResolveBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "localcode.db")
        self.database.add_provider(
            "OpenAI",
            endpoint="https://api.openai.com/v1",
            api_key="secret-key",
            context_window=128000,
        )
        self.settings = AppSettings(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_provider_prefixed_model_routes_to_api_client(self) -> None:
        client, model, context = resolve_backend("OpenAI/gpt-4o", self.settings)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(model, "gpt-4o")
        self.assertEqual(context, 128000)

    def test_legacy_display_name_still_routes_to_api_client(self) -> None:
        client, model, context = resolve_backend("gpt-4o (OpenAI)", self.settings)
        self.assertIsInstance(client, OpenAIClient)
        self.assertEqual(model, "gpt-4o")
        self.assertEqual(context, 128000)

    def test_unmatched_model_routes_to_ollama(self) -> None:
        client, model, context = resolve_backend("llama3", self.settings)
        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(model, "llama3")
        self.assertEqual(context, self.settings.default_context_window)

    def test_provider_api_key_is_passed_but_not_returned(self) -> None:
        client, _model, _context = resolve_backend("OpenAI/gpt-4o", self.settings)
        self.assertEqual(client.api_key, "secret-key")


if __name__ == "__main__":
    unittest.main()
