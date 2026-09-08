import asyncio, sys, unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(REPO_ROOT))
from core.agent_runtime import AgentRuntime, Message, ModelPolicy, ModelTurn, RetryClass, RuntimePolicyError, TokenUsage, ToolCall, ToolDefinition
from core.agent_runtime.adapters import ModelProviderError

POLICY = ModelPolicy(2, 4, 3, 100, 100, 1.0, 1, 3, 0.5)

class FakeModel:
    def __init__(self, name, outcomes): self.name=name; self.outcomes=list(outcomes); self.calls=0
    async def run_turn(self, *_):
        self.calls += 1; value=self.outcomes.pop(0)
        if isinstance(value, Exception): raise value
        return value

async def tool(_name, _args): return "UNTRUSTED_DATA: healthy"

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_turn_and_result_are_normalized(self):
        model=FakeModel("local",[ModelTurn(tool_calls=(ToolCall("1","health",{}),),finish_reason="tool_calls"),ModelTurn("done",usage=TokenUsage(2,3))])
        run=await AgentRuntime([model],tool).run([Message("user","inspect")],[ToolDefinition("health","read",{})],POLICY)
        self.assertEqual((run.text,run.turns,run.tool_calls),("done",2,1)); self.assertEqual(run.messages[-2].role,"tool")

    async def test_transient_failure_retries_then_falls_back(self):
        transient=ModelProviderError("down",RetryClass.PROVIDER_OUTAGE)
        first=FakeModel("first",[transient,transient]); second=FakeModel("second",[ModelTurn("ok")])
        run=await AgentRuntime([first,second],tool).run([],[],POLICY)
        self.assertEqual(run.provider,"second"); self.assertEqual((first.calls,second.calls),(2,1))

    async def test_auth_failure_never_falls_back(self):
        first=FakeModel("first",[ModelProviderError("bad key",RetryClass.AUTHENTICATION)]); second=FakeModel("second",[ModelTurn("unsafe")])
        with self.assertRaises(ModelProviderError): await AgentRuntime([first,second],tool).run([],[],POLICY)
        self.assertEqual(second.calls,0)

    async def test_undeclared_tool_and_budgets_fail_closed(self):
        model=FakeModel("m",[ModelTurn(tool_calls=(ToolCall("1","restart",{}),))])
        with self.assertRaisesRegex(RuntimePolicyError,"undeclared"): await AgentRuntime([model],tool).run([],[],POLICY)
        costly=FakeModel("c",[ModelTurn("x",usage=TokenUsage(101,1),estimated_cost_usd=2)])
        with self.assertRaisesRegex(RuntimePolicyError,"input token"): await AgentRuntime([costly],tool).run([],[],POLICY)

    async def test_output_cost_tool_and_turn_budgets_stop_predictably(self):
        output=FakeModel("output",[ModelTurn("x",usage=TokenUsage(1,101))])
        with self.assertRaisesRegex(RuntimePolicyError,"output token"): await AgentRuntime([output],tool).run([],[],POLICY)
        cost=FakeModel("cost",[ModelTurn("x",estimated_cost_usd=1.01)])
        with self.assertRaisesRegex(RuntimePolicyError,"cost budget"): await AgentRuntime([cost],tool).run([],[],POLICY)
        calls=tuple(ToolCall(str(i),"health",{}) for i in range(4))
        too_many_tools=FakeModel("tools",[ModelTurn(tool_calls=calls)])
        with self.assertRaisesRegex(RuntimePolicyError,"tool-call budget"): await AgentRuntime([too_many_tools],tool).run([], [ToolDefinition("health","read",{})], POLICY)
        short=ModelPolicy(2,1,3,100,100,1,0,3,0.5)
        too_many_turns=FakeModel("turns",[ModelTurn(tool_calls=(ToolCall("1","health",{}),)),ModelTurn("late")])
        with self.assertRaisesRegex(RuntimePolicyError,"model turn budget"): await AgentRuntime([too_many_turns],tool).run([], [ToolDefinition("health","read",{})], short)

    async def test_timeout_is_transient_and_external_cancellation_propagates(self):
        class Slow:
            name="slow"
            async def run_turn(self,*_): await asyncio.sleep(2); return ModelTurn("late")
        fast=FakeModel("fast",[ModelTurn("ok")])
        run=await AgentRuntime([Slow(),fast],tool).run([],[],POLICY)
        self.assertEqual(run.provider,"fast")
        task=asyncio.create_task(AgentRuntime([Slow()],tool).run([],[],POLICY)); await asyncio.sleep(0.01); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_circuit_opens_at_threshold(self):
        policy=ModelPolicy(2,4,3,100,100,1,0,1,0.5)
        bad=FakeModel("bad",[ModelProviderError("down",RetryClass.PROVIDER_OUTAGE)]); good=FakeModel("good",[ModelTurn("one"),ModelTurn("two")])
        runtime=AgentRuntime([bad,good],tool)
        self.assertEqual((await runtime.run([],[],policy)).provider,"good")
        self.assertEqual((await runtime.run([],[],policy)).provider,"good"); self.assertEqual(bad.calls,1)

if __name__ == "__main__": unittest.main()
