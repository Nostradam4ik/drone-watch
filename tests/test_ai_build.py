"""build_ai: AI-асистент вмикається лише коли ai.enabled і справді є MISTRAL_API_KEY; інакше сервіс стартує
без нього, з попередженням у лог (спец: «ai.enabled: true без MISTRAL_API_KEY — сервіс стартує, лог містить
попередження, ендпоінт каже чому не працює»). Жодна з цих гілок не має робити мережевий виклик."""
from dronewatch.service import app, config


def _cfg(enabled: bool) -> config.Config:
    return config.Config(mode="demo", ai={"enabled": enabled})


def test_build_ai_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert app.build_ai(_cfg(enabled=False)) is None


def test_build_ai_returns_none_and_warns_when_enabled_without_key(monkeypatch, caplog):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with caplog.at_level("WARNING", logger="dronewatch"):
        result = app.build_ai(_cfg(enabled=True))
    assert result is None
    assert any("MISTRAL_API_KEY" in r.message for r in caplog.records)


def test_build_ai_returns_runtime_when_enabled_with_key(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    result = app.build_ai(_cfg(enabled=True))
    assert result is not None
    assert result.max_iterations == config.AiCfg().max_tool_iterations
