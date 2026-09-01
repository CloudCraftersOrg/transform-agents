from pathlib import Path

import pytest
import yaml

from tools.validators import validate_lza_config

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml"


@pytest.fixture
def cfg():
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_valid_config_passes(cfg):
    assert validate_lza_config(cfg).ok


def test_text_input_supported():
    assert validate_lza_config(FIXTURE.read_text(encoding="utf-8")).ok


def test_home_region_must_be_enabled(cfg):
    cfg["global_config"]["homeRegion"] = "eu-west-1"
    assert not validate_lza_config(cfg).ok


def test_control_tower_must_be_off(cfg):
    cfg["global_config"]["controlTower"]["enable"] = True
    assert not validate_lza_config(cfg).ok


def test_missing_mandatory_account(cfg):
    cfg["accounts_config"]["mandatoryAccounts"] = [
        a for a in cfg["accounts_config"]["mandatoryAccounts"] if a["name"] != "Audit"
    ]
    assert not validate_lza_config(cfg).ok


def test_account_email_required(cfg):
    cfg["accounts_config"]["mandatoryAccounts"][0]["email"] = "not-an-email"
    assert not validate_lza_config(cfg).ok


def test_security_services_must_be_disabled(cfg):
    cfg["security_config"]["securityHub"]["enable"] = True
    assert not validate_lza_config(cfg).ok


def test_config_recorder_must_be_off(cfg):
    cfg["security_config"]["awsConfig"]["enableConfigurationRecorder"] = True
    assert not validate_lza_config(cfg).ok


def test_rulesets_must_be_empty(cfg):
    cfg["security_config"]["awsConfig"]["ruleSets"] = ["x"]
    assert not validate_lza_config(cfg).ok


def test_security_config_is_optional(cfg):
    del cfg["security_config"]
    assert validate_lza_config(cfg).ok


def test_non_mapping_root_fails():
    assert not validate_lza_config("- a\n- b").ok


def test_garbage_yaml_fails():
    assert not validate_lza_config("foo: bar: baz").ok
