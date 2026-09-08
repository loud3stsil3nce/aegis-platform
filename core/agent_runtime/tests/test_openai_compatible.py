import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(REPO_ROOT))
from core.agent_runtime.adapters import ModelProviderError, OpenAICompatibleAdapter
from core.agent_runtime.config import ProviderConfig
from core.agent_runtime.types import Message, ModelPolicy, RetryClass, ToolDefinition

POLICY = ModelPolicy(30, 4, 4, 1000, 500, 1.0, 1, 2, 5)

def response(value, status=200): return status, {}, json.dumps(value).encode()

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_text_tool_calls_usage_and_cost(self):
        def transport(request, timeout):
            body = json.loads(request.data); self.assertEqual(body["model"], "test-model"); self.assertEqual(timeout, 5)
            arguments = json.dumps({"plugin_id": "hello"})
            return response({"id":"req-1","choices":[{"finish_reason":"tool_calls","message":{"content":"inspect","tool_calls":[{"id":"call-1","function":{"name":"health","arguments":arguments}}]}}],"usage":{"prompt_tokens":100,"completion_tokens":20}})
        config = ProviderConfig("cloud", "openrouter", "https://example.test/v1", "test-model", "TEST_KEY", 2.0, 4.0)
        with patch.dict(os.environ, {"TEST_KEY":"secret"}):
            turn = await OpenAICompatibleAdapter(config, transport).run_turn([Message("user","hi")],[ToolDefinition("health","read",{"type":"object"})],POLICY)
        self.assertEqual(turn.tool_calls[0].arguments, {"plugin_id":"hello"}); self.assertEqual(turn.provider_request_id,"req-1")
        self.assertAlmostEqual(turn.estimated_cost_usd, 0.00028)

    async def test_model_validation(self):
        adapter = OpenAICompatibleAdapter(ProviderConfig("local","ollama-openai","http://ollama:11434/v1","qwen",None), lambda *_: response({"data":[{"id":"qwen"}]}))
        await adapter.validate_model()

    async def test_missing_cloud_key_is_nonretryable(self):
        adapter = OpenAICompatibleAdapter(ProviderConfig("cloud","openrouter","https://example.test/v1","m","MISSING_KEY"), lambda *_: response({}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelProviderError) as caught: await adapter.run_turn([],[],POLICY)
        self.assertEqual(caught.exception.retry_class, RetryClass.AUTHENTICATION); self.assertFalse(caught.exception.retry_class.allows_fallback)

    async def test_reads_cloud_key_from_file(self):
        seen = {}
        def transport(request, _timeout):
            seen["authorization"] = request.headers.get("Authorization")
            return response({"choices":[{"message":{"content":"READY"}}]})
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("file-secret\n"); handle.flush()
            config = ProviderConfig("cloud", "openrouter", "https://example.test/v1", "m", None, api_key_file=handle.name)
            await OpenAICompatibleAdapter(config, transport).run_turn([], [], POLICY)
        self.assertEqual(seen["authorization"], "Bearer file-secret")

    async def test_missing_or_oversized_key_file_is_nonretryable(self):
        configs = [ProviderConfig("cloud", "openrouter", "https://example.test/v1", "m", None, api_key_file="/not/present")]
        with tempfile.NamedTemporaryFile("wb") as handle:
            handle.write(b"x" * 4097); handle.flush()
            configs.append(ProviderConfig("cloud", "openrouter", "https://example.test/v1", "m", None, api_key_file=handle.name))
            for config in configs:
                with self.subTest(path=config.api_key_file):
                    with self.assertRaises(ModelProviderError) as caught:
                        await OpenAICompatibleAdapter(config).run_turn([], [], POLICY)
                    self.assertEqual(caught.exception.retry_class, RetryClass.AUTHENTICATION)
                    self.assertFalse(caught.exception.retry_class.allows_fallback)

    async def test_http_classification_controls_fallback(self):
        for code, expected in ((429,RetryClass.RATE_LIMIT),(503,RetryClass.RETRYABLE_UPSTREAM),(401,RetryClass.AUTHENTICATION),(400,RetryClass.INVALID_REQUEST)):
            adapter = OpenAICompatibleAdapter(ProviderConfig("local","ollama-openai","http://local/v1","m",None), lambda *_, code=code: (code,{},b"error"))
            with self.assertRaises(ModelProviderError) as caught: await adapter.run_turn([],[],POLICY)
            self.assertEqual(caught.exception.retry_class, expected)

    async def test_invalid_tool_arguments_fail_as_programming_error(self):
        value={"choices":[{"message":{"tool_calls":[{"id":"x","function":{"name":"t","arguments":"[]"}}]}}]}
        adapter=OpenAICompatibleAdapter(ProviderConfig("local","ollama-openai","http://local/v1","m",None),lambda *_:response(value))
        with self.assertRaises(ModelProviderError) as caught: await adapter.run_turn([],[],POLICY)
        self.assertEqual(caught.exception.retry_class,RetryClass.PROGRAMMING)

if __name__ == "__main__": unittest.main()
