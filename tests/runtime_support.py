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


#: The live acceptance requirement. It is deliberately complete: the entry point,
#: the response contract, the preserved behaviour, the test approach, and the
#: explicit exclusions are all stated, so a blocking Intake question would be a real
#: finding rather than an artifact of an underspecified request.
LIVE_REQUIREMENT = (
    "Add a GET /hello endpoint to the existing FastAPI application. The endpoint "
    'requires no parameters and returns HTTP 200 with JSON body {"message": '
    '"Hello, world!"}. Use the existing application entry point, preserve existing '
    "routes, and add a pytest test using FastAPI TestClient. No authentication, "
    "database, frontend, external integrations, or deployment are required. The "
    "project is disposable and local-only."
)

LIVE_ACCEPTANCE_CRITERIA = "\n".join((
    "1. GET /hello returns HTTP 200.",
    '2. Response JSON equals {"message": "Hello, world!"}.',
    "3. Existing application routes remain unchanged.",
    "4. Automated pytest verifies the endpoint response.",
))

#: A small, real FastAPI project with an explicit entry point, an existing route,
#: and an existing test. It is disposable and never deployed.
LIVE_FASTAPI_FILES = {
    "README.md": """# Disposable FastAPI acceptance fixture

This repository is a small, local-only FastAPI project used by the live acceptance
test. It is disposable and never deployed.

- Application entry point: `app/main.py`; the ASGI application object is `app`.
- Existing route: `GET /health` returns `{"status": "ok"}`.
- Existing test file: `tests/test_health.py`; run `pytest` from the repository root.
""",
    "requirements.txt": "fastapi\nuvicorn\npytest\nhttpx\n",
    "app/__init__.py": '"""FastAPI acceptance fixture package."""\n',
    "app/main.py": '''"""Explicit application entry point for the disposable acceptance fixture."""

from fastapi import FastAPI

app = FastAPI(title="LoomSphere acceptance fixture")


@app.get("/health")
def health() -> dict[str, str]:
    """An existing route that new work must preserve."""

    return {"status": "ok"}
''',
    "tests/__init__.py": "",
    "tests/test_health.py": '''"""Existing automated test for the fixture's only route."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_route() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
''',
}


class LiveFastApiCase(RuntimeCase):
    """A disposable FastAPI project for the opt-in live acceptance tests.

    ``RuntimeCase`` gives offline tests a README-only repository, which is enough
    for a scripted model but underspecified for a real Intake assessment. This
    fixture adds a real application entry point, an existing route, and an existing
    test so the live request can be assessed without invented requirements.
    """

    def setUp(self) -> None:
        super().setUp()
        for relative, content in LIVE_FASTAPI_FILES.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "fastapi fixture"],
                       check=True)

    def submitted_live(self, **engine_kwargs) -> tuple[Engine, str]:
        engine = self.engine(**engine_kwargs)
        engine.initialize()
        task = engine.submit(
            title="Add GET /hello to the FastAPI service",
            requirements=LIVE_REQUIREMENT,
            acceptance_criteria=LIVE_ACCEPTANCE_CRITERIA,
        )
        return engine, task["id"]

    def live_config(self, model: PydanticHarnessConfig, *,
                    timeout_seconds: int = 300) -> RuntimeConfig:
        """The offline default (30s) is too short for real provider latency."""

        return RuntimeConfig(harness_root=self.harness_root, state_dir=None,
                             model=model, timeout_seconds=timeout_seconds)
