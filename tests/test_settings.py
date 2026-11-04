"""Mock-by-default mode detection and secret hygiene."""

from __future__ import annotations

import pytest

from sfdq.settings import ConfigError, load_settings

SF = {"SF_USERNAME": "u@x.com", "SF_PASSWORD": "pw", "SF_SECURITY_TOKEN": "tok"}


def test_empty_environment_is_fully_mock() -> None:
    s = load_settings({})
    assert (s.salesforce_mode, s.llm_mode) == ("mock", "mock")
    assert s.describe() == "Salesforce: mock | LLM: mock"


def test_each_component_goes_real_only_with_its_own_credentials() -> None:
    assert load_settings(SF).salesforce_mode == "real" and load_settings(SF).llm_mode == "mock"
    only_key = load_settings({"ANTHROPIC_API_KEY": "sk-1"})
    assert (only_key.salesforce_mode, only_key.llm_mode) == ("mock", "real")
    partial_sf = load_settings({"SF_USERNAME": "u", "SF_PASSWORD": "p"})  # no token
    assert partial_sf.salesforce_mode == "mock"
    both = load_settings({**SF, "ANTHROPIC_API_KEY": "sk-1", "SF_DOMAIN": "test"})
    assert (both.salesforce_mode, both.llm_mode) == (
        "real",
        "real",
    ) and both.salesforce.domain == "test"


def test_blank_values_count_as_unset() -> None:
    s = load_settings({"ANTHROPIC_API_KEY": "  ", "SF_USERNAME": "", "SFDQ_MODEL": ""})
    assert (s.salesforce_mode, s.llm_mode, s.model) == ("mock", "mock", "claude-opus-5-5")


def test_mock_mode_can_be_forced_even_with_credentials() -> None:
    s = load_settings({**SF, "ANTHROPIC_API_KEY": "sk-1", "SFDQ_MODE": "mock"})
    assert (s.salesforce_mode, s.llm_mode) == ("mock", "mock")
    assert s.anthropic_api_key is None and s.salesforce is None


def test_forcing_real_without_credentials_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings({**SF, "SFDQ_MODE": "real"})
    with pytest.raises(ConfigError, match="SF_USERNAME"):
        load_settings({"ANTHROPIC_API_KEY": "k", "SFDQ_MODE": "real"})
    with pytest.raises(ConfigError, match="must be"):
        load_settings({"SFDQ_MODE": "banana"})


def test_secrets_never_appear_in_repr() -> None:
    s = load_settings({**SF, "ANTHROPIC_API_KEY": "sk-super-secret"})
    assert "pw" not in repr(s.salesforce) and "tok" not in repr(s.salesforce)
    assert s.salesforce is not None and "u@x.com" in repr(s.salesforce)
    assert "sk-super-secret" not in repr(s) and "sk-super-secret" not in str(s)
    assert "pw" not in repr(s) and "tok" not in repr(s)
