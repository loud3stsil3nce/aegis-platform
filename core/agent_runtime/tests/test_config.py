import json, sys, tempfile, unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(REPO_ROOT))
from core.agent_runtime.config import ModelConfigurationError, load_model_config

class ConfigTests(unittest.TestCase):
    def test_example_loads_without_provider_sdk(self):
        policy, providers = load_model_config(REPO_ROOT / "config/model-policy.example.json")
        self.assertEqual([p.adapter for p in providers], ["ollama-openai", "openrouter"])
        self.assertEqual(policy.max_turns, 8)

    def test_zero_providers_fails(self):
        source = json.loads((REPO_ROOT / "config/model-policy.example.json").read_text()); source["providers"] = []
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(source, handle); handle.flush()
            with self.assertRaisesRegex(ModelConfigurationError, "at least one"):
                load_model_config(handle.name)

    def test_nonpositive_budget_fails(self):
        source = json.loads((REPO_ROOT / "config/model-policy.example.json").read_text()); source["policy"]["max_turns"] = 0
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(source, handle); handle.flush()
            with self.assertRaisesRegex(ModelConfigurationError, "positive"):
                load_model_config(handle.name)

    def test_cloud_provider_requires_positive_pricing(self):
        source = json.loads((REPO_ROOT / "config/model-policy.example.json").read_text())
        source["providers"][1]["inputCostPerMillion"] = 0
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(source, handle); handle.flush()
            with self.assertRaisesRegex(ModelConfigurationError, "costs must be positive"):
                load_model_config(handle.name)

    def test_provider_rejects_ambiguous_credential_sources(self):
        source = json.loads((REPO_ROOT / "config/model-policy.example.json").read_text())
        source["providers"][1]["apiKeyEnv"] = "OPENROUTER_API_KEY"
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(source, handle); handle.flush()
            with self.assertRaisesRegex(ModelConfigurationError, "both apiKeyEnv and apiKeyFile"):
                load_model_config(handle.name)

if __name__ == "__main__": unittest.main()
