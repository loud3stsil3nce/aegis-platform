import json, os, sys, unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(REPO_ROOT))
from core.agent_runtime import AgentRuntime, Message, ModelPolicy, build_providers
from core.agent_runtime.config import ProviderConfig

POLICY = ModelPolicy(5,2,1,100,100,1,0,2,1)

def completion(text):
    return 200, {}, json.dumps({"id":"request","choices":[{"finish_reason":"stop","message":{"content":text}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}).encode()

async def no_tools(*_): raise AssertionError("no tools expected")

class ModeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_only_configuration_end_to_end(self):
        configs=[ProviderConfig("local","ollama-openai","http://ollama:11434/v1","local-model",None)]
        providers=build_providers(configs,lambda _: lambda *_: completion("local"))
        run=await AgentRuntime(providers,no_tools).run([Message("user","hi")],[],POLICY)
        self.assertEqual((run.provider,run.text),("local","local"))

    async def test_cloud_only_configuration_end_to_end(self):
        configs=[ProviderConfig("cloud","openrouter","https://openrouter.ai/api/v1","cloud-model","OPENROUTER_API_KEY")]
        providers=build_providers(configs,lambda _: lambda *_: completion("cloud"))
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"test-token"}):
            run=await AgentRuntime(providers,no_tools).run([Message("user","hi")],[],POLICY)
        self.assertEqual((run.provider,run.text),("cloud","cloud"))

    async def test_mixed_configuration_transient_fallback_end_to_end(self):
        configs=[
            ProviderConfig("cloud","openrouter","https://openrouter.ai/api/v1","cloud-model","OPENROUTER_API_KEY"),
            ProviderConfig("local","ollama-openai","http://ollama:11434/v1","local-model",None),
        ]
        def transports(config):
            return (lambda *_: (503,{},b"unavailable")) if config.id=="cloud" else (lambda *_: completion("local fallback"))
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"test-token"}):
            run=await AgentRuntime(build_providers(configs,transports),no_tools).run([Message("user","hi")],[],POLICY)
        self.assertEqual((run.provider,run.text),("local","local fallback"))

if __name__ == "__main__": unittest.main()
