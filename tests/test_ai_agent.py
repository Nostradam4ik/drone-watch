"""Цикл tool-calling агента: обмежена кількість ітерацій, trace викликів, стійкість до невідомого тулу."""
from dronewatch.fusion.sim import default_sensors
from dronewatch.service import app
from dronewatch.service.ai import agent
from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.ai.llm import ChatResult, ToolCall
from dronewatch.service.engine import Engine, Zone


class _ScriptedLlm:
    def __init__(self, responses: list[ChatResult]):
        self._responses = iter(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        return next(self._responses)


class _FakeIndex:
    def __init__(self, hits=None, fail=False):
        self.hits, self.fail = hits or [], fail

    def search(self, query, k=3):
        if self.fail:
            raise AiUnavailable("down")
        return self.hits


def _runtime():
    return app.Runtime(engine=Engine(default_sensors(), [Zone("z", 100, "WARNING")]))


def test_ask_returns_immediate_answer_with_no_tools():
    result = agent.ask("хай", llm=_ScriptedLlm([ChatResult(content="привіт", tool_calls=[])]),
                       rt=_runtime(), index=_FakeIndex(), max_iterations=4)
    assert result == agent.AgentAnswer(answer="привіт", tool_calls=[])


def test_ask_calls_tool_then_answers():
    scripted = _ScriptedLlm([
        ChatResult(content=None, tool_calls=[ToolCall(id="1", name="get_state", args={})]),
        ChatResult(content="зараз так", tool_calls=[]),
    ])
    result = agent.ask("що зараз?", llm=scripted, rt=_runtime(), index=_FakeIndex(), max_iterations=4)
    assert result.answer == "зараз так"
    assert result.tool_calls == [{"name": "get_state", "args": {}}]


def test_ask_stops_after_max_iterations_without_crashing():
    always_calls = _ScriptedLlm([ChatResult(content=None, tool_calls=[ToolCall(id=str(i), name="get_state", args={})])
                                 for i in range(10)])
    result = agent.ask("...", llm=always_calls, rt=_runtime(), index=_FakeIndex(), max_iterations=3)
    assert "не вдалося" in result.answer.lower()
    assert len(result.tool_calls) == 3


def test_ask_unknown_tool_name_becomes_tool_error_not_crash():
    scripted = _ScriptedLlm([
        ChatResult(content=None, tool_calls=[ToolCall(id="1", name="delete_everything", args={})]),
        ChatResult(content="ясно", tool_calls=[]),
    ])
    result = agent.ask("...", llm=scripted, rt=_runtime(), index=_FakeIndex(), max_iterations=4)
    assert result.answer == "ясно"


def test_tool_result_travels_as_tool_role_message_not_folded_into_prompt():
    scripted = _ScriptedLlm([
        ChatResult(content=None, tool_calls=[ToolCall(id="1", name="get_state", args={})]),
        ChatResult(content="готово", tool_calls=[]),
    ])
    agent.ask("...", llm=scripted, rt=_runtime(), index=_FakeIndex(), max_iterations=4)
    second_call_messages = scripted.calls[1]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    non_tool_messages = [m for m in second_call_messages if m["role"] != "tool"]
    assert tool_messages, "очікувався хоча б один message з role=tool"
    assert all('"tracks"' not in m["content"] for m in non_tool_messages if isinstance(m.get("content"), str))
