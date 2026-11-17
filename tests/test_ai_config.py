"""Конфіг AI-асистента (ai: у config.yaml): значення за замовчуванням, перекриття, межі."""
import pytest
from pydantic import ValidationError

from dronewatch.service import config


def test_ai_config_defaults_and_overrides():
    cfg = config.Config(mode="demo")
    assert cfg.ai.enabled is False and cfg.ai.model == "mistral-small-latest"
    cfg2 = config.Config(mode="demo", ai={"enabled": True, "max_tool_iterations": 2})
    assert cfg2.ai.enabled is True and cfg2.ai.max_tool_iterations == 2


def test_ai_config_rejects_out_of_range_iterations():
    with pytest.raises(ValidationError):
        config.Config(mode="demo", ai={"max_tool_iterations": 0})
