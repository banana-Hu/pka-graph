import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.local_config import load_local_ai_config


class LocalConfigTests(unittest.TestCase):
    def test_shared_key_and_separate_budgets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            source = root / "source.env"
            original = 'OTHER_SECRET=not-for-this-app\nMINIMAX_SEARCH_API_KEY="test-only"\n'
            source.write_text(original, encoding="utf-8")
            config = root / "ai.local.json"
            config.write_text(json.dumps(dict(api_base="https://example.test",
                model="test-model", key_env_file=str(source),
                key_env_name="MINIMAX_SEARCH_API_KEY")), encoding="utf-8")
            os.environ["PKA_QUIZ_MAX_CALLS"] = "3"
            os.environ["PKA_DISCOVERY_DAILY_LIMIT"] = "12"
            self.assertTrue(load_local_ai_config(config))
            for prefix in ("PKA_QUIZ", "PKA_DISCOVERY"):
                self.assertEqual(os.environ[prefix + "_API_KEY"], "test-only")
                self.assertEqual(os.environ[prefix + "_MODEL"], "test-model")
            self.assertNotIn("OTHER_SECRET", os.environ)
            self.assertEqual(os.environ["PKA_QUIZ_MAX_CALLS"], "3")
            self.assertEqual(os.environ["PKA_DISCOVERY_DAILY_LIMIT"], "12")
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_missing_config_preserves_environment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            self.assertFalse(load_local_ai_config(Path(tmp) / "missing.json"))
            self.assertNotIn("PKA_QUIZ_ADAPTER", os.environ)

    def test_failure_does_not_disclose_or_partially_configure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            source = root / "source.env"
            source.write_text("KEY=test-secret\nKEY=duplicate-secret", encoding="utf-8")
            config = root / "ai.local.json"
            config.write_text(json.dumps(dict(api_base="https://example.test",
                model="test-model", key_env_file=str(source), key_env_name="KEY")), encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                load_local_ai_config(config)
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("PKA_QUIZ_ADAPTER", os.environ)
