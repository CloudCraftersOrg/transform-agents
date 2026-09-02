from pathlib import Path

from agents.interpreter import build_interpreter, generate_lza_config, generate_modernization_iac
from agents.model import FakeModel

VALID = (Path(__file__).resolve().parents[1] / "fixtures" / "lza" / "valid_config.yaml").read_text(
    encoding="utf-8"
)

_VALID_IAC = """
resource "aws_ecr_repository" "app" { name = "svc" }
resource "aws_ecs_task_definition" "app" {
  family                = "svc"
  container_definitions = "[{\\"name\\":\\"svc\\",\\"image\\":\\"svc:latest\\"}]"
}
resource "aws_ecs_service" "app" { name = "svc" cluster = "prod" }
"""


def test_converges_on_valid_config():
    r = generate_lza_config("minimal landing zone", FakeModel([VALID]))
    assert r.ok and r.iterations == 1


def test_regenerates_after_invalid_config():
    bad = "global_config:\n  homeRegion: us-east-1\n  enabledRegions: [eu-west-1]\n"
    model = FakeModel([bad, VALID])
    r = generate_lza_config("x", model)
    assert r.ok and r.iterations == 2
    assert "rejected the previous attempt" in model.prompts[1]


def test_exhausts_when_never_valid():
    r = generate_lza_config("x", FakeModel(["global_config: {}\n"]), max_iter=4)
    assert not r.ok and r.iterations == 4


def test_build_interpreter_returns_callable():
    delegate = build_interpreter(FakeModel([VALID]))
    assert delegate("objective").ok


def test_fenced_yaml_is_stripped():
    r = generate_lza_config("x", FakeModel([f"```yaml\n{VALID}```"]))
    assert r.ok


def test_modernization_iac_converges_on_valid_hcl():
    r = generate_modernization_iac("catalog service", FakeModel([_VALID_IAC]))
    assert r.ok and r.iterations == 1


def test_modernization_iac_gives_up_on_non_container_hcl():
    r = generate_modernization_iac(
        "x", FakeModel(['resource "aws_instance" "vm" {\n  ami = "ami-1"\n}\n']), max_iter=3
    )
    assert not r.ok and r.iterations == 3


def test_modernization_iac_strips_a_code_fence():
    r = generate_modernization_iac("x", FakeModel([f"```hcl\n{_VALID_IAC}\n```"]))
    assert r.ok
