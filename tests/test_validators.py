from pathlib import Path

import pytest
import yaml

from tools.validators import validate_iac, validate_lza_config

_VALID_IAC = """
resource "aws_ecr_repository" "app" {
  name = "catalog"
}

resource "aws_ecs_task_definition" "app" {
  family                = "catalog"
  container_definitions = "[{\\"name\\":\\"catalog\\",\\"image\\":\\"catalog:latest\\"}]"
}

resource "aws_ecs_service" "app" {
  name            = "catalog"
  cluster         = "prod"
  task_definition = "catalog:1"
  desired_count   = 2
}
"""

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


# --- validate_iac: offline structural fallback (no terraform binary) ---


def test_iac_accepts_a_minimal_container_deployment():
    assert validate_iac(_VALID_IAC).ok


def test_iac_rejects_non_container_infra():
    r = validate_iac('resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\n')
    assert not r.ok and "container compute target" in r.error


def test_iac_rejects_task_def_without_an_image_source():
    r = validate_iac('resource "aws_ecs_task_definition" "a" {\n  family = "a"\n}\n')
    assert not r.ok and "image source" in r.error


def test_iac_rejects_unparseable_hcl():
    assert not validate_iac("this is not { valid hcl").ok


REAL_FARGATE_HCL = '''provider "aws" {
  region = "us-west-2"
}

resource "aws_ecs_cluster" "example" {
  name = "example-cluster"
}

resource "aws_ecs_task_definition" "example" {
  family                   = "example-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"

  container_definitions = jsonencode([
    {
      name      = "example-container"
      image     = "${aws_ecr_repository.example.repository_url}:latest"
      essential = true
    }
  ])
}

resource "aws_ecr_repository" "example" {
  name = "example-repo"
}

resource "aws_ecs_service" "example" {
  name            = "example-service"
  cluster         = aws_ecs_cluster.example.id
  task_definition = aws_ecs_task_definition.example.arn
  desired_count   = 1
  launch_type     = "FARGATE"
}
'''


def test_a_whole_document_is_never_probed_as_a_path():
    # pathlib lets ENAMETOOLONG escape is_dir()/is_file() on Linux, so passing the generated HCL
    # straight through used to raise OSError(36) inside the container while passing on Windows.
    outcome = validate_iac(REAL_FARGATE_HCL)
    assert outcome.ok, outcome.error


def test_long_single_line_document_is_not_probed_either():
    outcome = validate_iac('resource "aws_ecs_service" "x" { name = "' + "a" * 300 + '" }')
    assert isinstance(outcome.ok, bool)  # no OSError
