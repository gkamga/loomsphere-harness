"""Shared fixtures for the standalone runtime tests (no network, no real model)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from loomsphere_harness.config import RuntimeConfig
from loomsphere_harness.engine import Engine
from loomsphere_harness.pydantic_harness import PydanticHarnessConfig, PydanticHarnessRunner
from loomsphere_harness.repository import GitRepository, OsUserIdentity
from loomsphere_harness.store import SqliteStore

HARNESS_ROOT = Path(__file__).resolve().parents[1]

INTAKE_ASSESSMENT = {
    "intent": {"summary": "Add a greeting endpoint.", "outcome": "GET /hello answers.",
               "request_type": "feature", "confidence": "high"},
    "affected_components": [{"path": "app", "kind": "backend", "reason": "hosts the route"}],
    "implications": {"security": [], "ux_ui": [], "database": [], "architecture": []},
    "ambiguities": [],
    "specialist_recommendations": [],
    "risk": {"level": "low", "reasons": []},
    "assumptions": [],
    "notes": [],
}
PROPOSAL = {
    "summary": "Add a /hello route.",
    "implementation_approach": ["Add the route.", "Test it."],
    "components": [{"path": "app", "change": "Add the route."}],
    "acceptance_criteria_mapping": [
        {"criterion": "GET /hello returns 200", "approach": "Call it.",
         "verification": {"kind": "pytest", "selector": "tests/test_hello.py::test_hello"}}
    ],
    "risks": ["None significant."],
    "assumptions": [],
    "dependencies": [],
    "validation_strategy": ["Run pytest."],
    "open_questions": [],
}


def fenced(document: dict) -> str:
    return "```json\n" + json.dumps(document) + "\n```"


def scripted_model(*, intake: dict | None = None, proposal: dict | None = None,
                   calls: list | None = None) -> FunctionModel:
    def respond(messages, info):
        prompt = "\n".join(
            str(getattr(part, "content", ""))
            for message in messages for part in getattr(message, "parts", ())
        )
        if calls is not None:
            calls.append(prompt)
        is_intake = "LoomSphere Intake agent" in prompt
        document = (intake or INTAKE_ASSESSMENT) if is_intake else (proposal or PROPOSAL)
        return ModelResponse(parts=[TextPart(fenced(document))])

    return FunctionModel(respond)


def model_config(**overrides) -> PydanticHarnessConfig:
    values = dict(base_url="https://models.test/v1", model_id="test-model",
                  approved_hosts=("models.test",), api_key="not-a-real-key")
    values.update(overrides)
    return PydanticHarnessConfig(**values)


class RuntimeCase(unittest.TestCase):
    """A temp Git repository, a temp state directory, and the real harness root."""

    harness_root = HARNESS_ROOT

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="lsh-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "target"
        self.repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.test"],
                     ["config", "user.name", "Test"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        (self.repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        self.state = self.tmp / "state" / "state.sqlite"

    def config(self, model: PydanticHarnessConfig | None = None, harness_root: Path | None = None
               ) -> RuntimeConfig:
        return RuntimeConfig(harness_root=harness_root or self.harness_root, state_dir=None,
                             model=model or model_config(), timeout_seconds=30)

    def engine(self, *, model: object | None = None, config: RuntimeConfig | None = None,
               actor: str = "alice") -> Engine:
        factory = (lambda cfg: PydanticHarnessRunner(cfg, model=model)) if model else None
        return Engine(store=SqliteStore(self.state), config=config or self.config(),
                      repository=GitRepository(self.repo), identity=OsUserIdentity(actor),
                      repo_path=self.repo, runner_factory=factory)

    def submitted(self, **engine_kwargs) -> tuple[Engine, str]:
        engine = self.engine(**engine_kwargs)
        engine.initialize()
        task = engine.submit(title="Hello", requirements="Add GET /hello.",
                             acceptance_criteria="GET /hello returns 200.")
        return engine, task["id"]
