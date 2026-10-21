import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adra.cli import main
from adra.configuration import RuntimeConfig
from adra.live_evaluation import collect_result, prepare_cases
from adra.policy import ModelBudgetExceeded, ModelPolicy
from adra.state import Action


class Model:
    def __init__(self):
        self.calls = 0

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        return {"raw": SimpleNamespace(usage_metadata={"total_tokens": 5}),
                "parsed": Action(kind="STOP")}


def test_phase_routing_and_hard_request_limit():
    default, planner = Model(), Model()
    policy = ModelPolicy(default, phase_models={"planning": planner}, max_model_calls=2)
    policy._invoke(Action, "test", {}, phase="planning")
    policy._invoke(Action, "test", {}, phase="decision")
    with pytest.raises(ModelBudgetExceeded):
        policy._invoke(Action, "test", {})
    assert (default.calls, planner.calls) == (1, 1)
    assert policy.tokens == 10
    assert policy.budget_exhausted
    assert policy.write({"query": "q", "evidence": []}).claims == []


def test_runtime_config_never_serializes_credentials(monkeypatch):
    monkeypatch.setenv("ADRA_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-secret")
    monkeypatch.setenv("ADRA_COVERAGE_MODEL", "test:judge")
    config = RuntimeConfig.from_env()
    assert config.coverage_model == "test:judge"
    assert "private-secret" not in config.model_dump_json()


def test_fixed_plan_failure_also_saves_safe_record(monkeypatch, tmp_path):
    broken = tmp_path / "plan.json"
    broken.write_text('{"query": "q", "tasks": []}')
    monkeypatch.setattr("sys.argv", ["adra", "q", "--plan", str(broken),
                                    "--strategy", "research", "--output", str(tmp_path / "out")])
    with pytest.raises(SystemExit):
        main()
    failure = json.loads(next((tmp_path / "out").glob("*/failure.json")).read_text())
    assert failure["status"] == "failed"
    assert failure["error_type"] == "ValidationError"


def test_batch_dry_run_does_not_spawn_or_create_outputs(monkeypatch, tmp_path, capsys):
    from adra.live_evaluation import main as batch_main
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("Dry run executed a command"))
    output = tmp_path / "out"
    monkeypatch.setattr("sys.argv", ["batch", "--output", str(output)])
    batch_main()
    assert "3 runs" in capsys.readouterr().out
    assert not output.exists()


def test_batch_inputs_and_failure_preservation(tmp_path):
    with pytest.raises(ValueError):
        prepare_cases(Path("data/live-eval/manifest.json"), ["unknown"])
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "failure.json").write_text(json.dumps({
        "error_type": "TimeoutError", "total_tokens": 8}))
    result = collect_result(tmp_path, 1)
    assert result["status"] == "failed"
    assert result["total_tokens"] == 8


def test_batch_execution_freezes_plans_and_keeps_failed_runs(monkeypatch, tmp_path):
    from adra.live_evaluation import main as batch_main
    calls = []
    def fake_run(command, check):
        calls.append(command)
        frozen = Path(command[command.index("--plan") + 1])
        assert frozen.parent.name == "plans"
        assert json.loads(frozen.read_text())["tasks"]
        output = Path(command[command.index("--output") + 1]) / "test-run"
        output.mkdir(parents=True)
        if len(calls) == 1:
            (output / "run.json").write_text(json.dumps({
                "metrics": {"tool_calls": 2}, "stop_reason": "plan_covered"}))
            return SimpleNamespace(returncode=0)
        (output / "failure.json").write_text(json.dumps({
            "error_type": "TimeoutError", "total_tokens": 12}))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("sys.argv", ["batch", "--execute", "--cases", "langgraph-state-routing", "python-asyncio",
                                    "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        batch_main()
    assert exc.value.code == 1
    summary = json.loads(next(tmp_path.glob("*/summary.json")).read_text())
    assert [r["status"] for r in summary] == ["completed", "failed"]
    assert summary[1]["total_tokens"] == 12
    assert next(tmp_path.glob("*/review.md")).exists()
