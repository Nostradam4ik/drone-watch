"""Read-only інструменти агента над Engine/Runtime. Жоден з них нічого не пише в систему — агент лише читає.
Mistral/OpenAI-формат схем (tool-calling), один словник на функцію нижче."""
from typing import TYPE_CHECKING, Any

from dronewatch.service.ai.errors import AiUnavailable
from dronewatch.service.engine import SensorStatus

if TYPE_CHECKING:
    from dronewatch.service.ai.rag import IncidentIndex
    from dronewatch.service.app import Runtime

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "get_state", "description": "Поточні треки й стан трекінгу.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_sensors", "description": "Здоров'я кожного датчика: OK/WAITING/LOST.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_incidents", "description": "Останні записані тривоги з журналу.",
                                      "parameters": {"type": "object", "properties": {
                                          "limit": {"type": "integer", "description": "Скільки останніх записів (типово 10)"}}}}},
    {"type": "function", "function": {"name": "search_similar_incidents",
                                      "description": "Семантичний пошук схожих минулих інцидентів.",
                                      "parameters": {"type": "object", "properties": {
                                          "query": {"type": "string", "description": "Опис ситуації, що шукаємо"}},
                                          "required": ["query"]}}},
]


def get_state(rt: "Runtime") -> dict[str, Any]:
    return rt.engine.state()


def get_sensors(rt: "Runtime") -> list[SensorStatus]:
    return list(rt.engine.sensor_status().values())


def get_incidents(rt: "Runtime", limit: int = 10) -> list[dict[str, Any]]:
    from dronewatch.service.app import read_incidents
    return read_incidents(rt.db_path, limit)


def search_similar_incidents(index: "IncidentIndex", query: str, k: int = 3) -> list[str] | str:
    try:
        return index.search(query, k)
    except AiUnavailable:
        return "пошук схожих інцидентів тимчасово недоступний"


def call_tool(name: str, args: dict[str, Any], *, rt: "Runtime", index: "IncidentIndex") -> dict[str, Any] | list[Any] | str:
    if name == "get_state":
        return get_state(rt)
    if name == "get_sensors":
        return get_sensors(rt)
    if name == "get_incidents":
        return get_incidents(rt, **({"limit": args["limit"]} if "limit" in args else {}))
    if name == "search_similar_incidents":
        return search_similar_incidents(index, args["query"], **({"k": args["k"]} if "k" in args else {}))
    raise KeyError(name)
