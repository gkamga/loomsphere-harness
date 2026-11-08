"""Measure QA context growth with a deterministic, offline replay.

This is a measurement tool, not a test: it drives the real
``PydanticHarnessRunner`` with a scripted ``FunctionModel`` over a fixture
workspace. The script is a *representative* sequence of capped reads modelled on
the QA work that exhausted a shared budget; it is not a recording of the Demandro
M00-02 run, and its paths and order are not claimed to be the ones that run used.
It reports, per model request, how many characters of context the runtime actually
handed the model, how much of that was retained tool-result content, and what the
run finally cost. It never records prompt text, file content, or tool arguments.

Three kinds of figure must stay distinct when these numbers are quoted:

* **Synthetic character-based estimates** — the ``characters // 4`` and
  ``characters // 6`` conversions printed here. They are arithmetic on measured
  character counts, not tokenizer output.
* **Actual recorded provider usage** — the ``recorded usage`` block, which comes
  only from what the model reported back through the runtime's own accumulator. In
  this offline replay the scripted model reports its own numbers, so the block
  shows the accounting path works; it is not DeepSeek usage.
* **Hypotheses about future savings** — any claim that a real provider bill will
  fall by a particular amount. Nothing here measures that.

Usage::

    .venv/bin/python tools/measure_qa_context.py [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import tempfile
from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from loomsphere_harness.agent_runner import AgentExecutionError, AgentInvocation
from loomsphere_harness.implementation_prompt import compose_qa_prompt
from loomsphere_harness.pydantic_harness import (
    ContextPolicy,
    PydanticHarnessConfig,
    PydanticHarnessRunner,
)
from loomsphere_harness.skill_selection import TaskFacts, select_role_skills
from loomsphere_harness.skills import declared_tools

ROOT = Path(__file__).resolve().parents[1]
QA_DEFINITION = ROOT / ".github/agents/loomsphere-qa.agent.md"
SKILLS_ROOT = ROOT / ".github/skills"

CRITERIA = [f"AC-{index}" for index in range(1, 6)]
CRITERIA_TEXT = "\n".join(
    f"{name}: the endpoint behaves as described in requirement {index}"
    for index, name in enumerate(CRITERIA, start=1)
)

#: A representative QA read sequence: two implementation files, one large source
#: file, and repeated reads of the same paths. It is scripted for deterministic
#: comparison, not recorded; the reference run's exact paths and order are not
#: claimed here.
READ_SEQUENCE = [
    "app/main.py",
    "app/helpers.py",
    "app/main.py",
    "tests/test_main.py",
    "app/main.py",
    "app/helpers.py",
    "app/main.py",
]

QA_REPORT = {
    "verdict": "passed",
    "summary": "Checked the recorded diff against each criterion.",
    "findings": [],
    "acceptance_criteria": [
        {"criterion_id": name, "criterion": "as approved", "status": "met",
         "evidence": "covered by the diff and the recorded test run"}
        for name in CRITERIA
    ],
    "residual_risks": [],
}


def write_fixture(repo: Path) -> None:
    """Two implementation files, one large source file, and a test module."""

    (repo / "app").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    # Long enough that one 400-line read reaches the 24,000-character cap, which
    # is what makes a single read cost a large share of the request in this replay.
    large = "".join(
        f"def helper_{index:04d}(value: int) -> int:  # padding padding padding\n"
        f"    return value + {index}  # extra padding padding padding padding\n"
        for index in range(1400)
    )
    (repo / "app/main.py").write_text(large, encoding="utf-8")
    (repo / "app/helpers.py").write_text(
        "VALUE = 1\n\ndef helper(value):\n    return value\n", encoding="utf-8"
    )
    (repo / "tests/test_main.py").write_text(
        "".join(
            f"def test_helper_{index:04d}() -> None:  # padding padding padding\n"
            f"    assert helper_0000({index}) == {index}  # padding padding padding\n"
            for index in range(1400)
        ),
        encoding="utf-8",
    )


def qa_prompt(repo: Path) -> tuple[str, dict[str, object]]:
    persona = QA_DEFINITION.read_text(encoding="utf-8")
    facts = TaskFacts("Add greeting endpoint", "Add GET /hello.", CRITERIA_TEXT)
    selection = select_role_skills(
        facts, role="qa", stage="validation", definition_path=QA_DEFINITION,
        skills_root=str(SKILLS_ROOT), tools=declared_tools(QA_DEFINITION),
    )
    diff = "".join(
        f"diff --git a/app/module_{index}.py b/app/module_{index}.py\n"
        f"--- a/app/module_{index}.py\n+++ b/app/module_{index}.py\n"
        + "".join(
            f"+def added_{index}_{line}(value):  # padding padding padding\n"
            f"+    return value + {line}\n"
            for line in range(12)
        )
        for index in range(7)
    )
    composed = compose_qa_prompt(
        persona=persona,
        task_title="Add greeting endpoint",
        requirements="Add a GET /hello endpoint returning a greeting.",
        acceptance_criteria=CRITERIA_TEXT,
        task_identifier="T-0001",
        implementation_summary="Added the endpoint and its test.",
        implementation_files=["app/main.py", "tests/test_main.py"],
        diff_excerpt=diff,
        validation_command=["python", "-m", "pytest", "-q"],
        validation_exit_code=0,
        validation_output="".join(
            f"tests/test_main.py::test_helper_{index:04d} PASSED {index:03d} "
            f"[ 1%] padding padding padding padding\n"
            for index in range(120)
        ),
        approved_criteria=[f"{name}: approved" for name in CRITERIA],
        skills=selection.documents,
    )
    return composed.text, selection.evidence()


def replay_model(*, reads: list[str] | None = None):
    """Read the fixture files in order, then answer with the QA report."""

    sequence = list(reads if reads is not None else READ_SEQUENCE)
    state = {"step": 0}
    per_request: list[dict[str, object]] = []

    def respond(messages, info):
        prompt_chars = sum(
            len(str(getattr(part, "content", "")))
            for message in messages
            for part in getattr(message, "parts", ())
        )
        calls = [
            part for message in messages for part in getattr(message, "parts", ())
            if isinstance(part, ToolCallPart)
        ]
        returns = [
            part for message in messages for part in getattr(message, "parts", ())
            if isinstance(part, ToolReturnPart)
        ]
        per_request.append(
            {
                "request": len(per_request) + 1,
                "prompt_chars": prompt_chars,
                "messages": len(messages),
                "tool_calls": len(calls),
                "tool_results": len(returns),
                "retained_tool_chars": sum(len(str(p.content)) for p in returns),
            }
        )
        step = state["step"]
        state["step"] += 1
        usage = RequestUsage(
            input_tokens=math.ceil(prompt_chars / 4),
            output_tokens=40,
        )
        if step < len(sequence):
            return ModelResponse(
                parts=[ToolCallPart("read_file", {"path": sequence[step]})],
                usage=usage,
            )
        return ModelResponse(parts=[TextPart("```json\n" + json.dumps(QA_REPORT) + "\n```")],
                             usage=usage)

    return FunctionModel(respond), per_request


def config(**overrides) -> PydanticHarnessConfig:
    values = dict(
        base_url="https://models.test/v1", model_id="replay-model",
        approved_hosts=("models.test",), api_key="not-a-real-key",
        request_limit=60, tool_call_limit=120, total_token_limit=150_000,
    )
    values.update(overrides)
    return PydanticHarnessConfig(**values)


def run(
    *,
    limit: int = 150_000,
    retained_chars: int | None = None,
    reads: list[str] | None = None,
    unbounded: bool = False,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    workdir = Path(tempfile.mkdtemp(prefix="qa-measure-"))
    try:
        repo = workdir / "repo"
        repo.mkdir()
        write_fixture(repo)
        prompt, selection = qa_prompt(repo)
        model, per_request = replay_model(reads=reads)
        if unbounded:
            # The behaviour before the fix: nothing is ever elided, so every request
            # resends every earlier tool result in full.
            policy = ContextPolicy(
                max_retained_chars=10**9, keep_recent=10**6, elidable_tools=()
            )
        elif retained_chars is not None:
            policy = ContextPolicy(max_retained_chars=retained_chars)
        else:
            policy = None
        runner = PydanticHarnessRunner(
            config(total_token_limit=limit), model=model, context_policy=policy
        )
        invocation = AgentInvocation(
            run_id=__import__("uuid").uuid4(), workspace_path=repo, prompt=prompt,
            timeout_seconds=120, read_only=True, role="qa",
            allowed_tools=("read", "search", "execute"),
        )
        summary: dict[str, object] = {
            "initial_prompt_chars": len(prompt),
            "skills": selection.get("names") if isinstance(selection, dict) else None,
        }
        try:
            result = runner.run(invocation)
        except AgentExecutionError as error:
            summary.update(
                outcome="failed", reason=error.reason, detail=error.detail,
                events=error.events,
            )
        else:
            summary.update(outcome="finished", events=result.events)
            summary["response_chars"] = len(result.response_text)
        summary["requests"] = per_request
        return summary, per_request
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def report(summary: dict[str, object]) -> str:
    lines = [
        f"outcome: {summary['outcome']}",
        f"reason: {summary.get('reason')}",
        f"initial prompt chars: {summary['initial_prompt_chars']}",
    ]
    requests = summary["requests"]
    lines.append("")
    lines.append(
        f"{'req':>3} {'prompt_chars':>12} {'tool_results':>12} "
        f"{'retained_tool_chars':>19} {'est_tokens':>10}"
    )
    total_est = 0
    for entry in requests:
        est = math.ceil(entry["prompt_chars"] / 4)
        total_est += est
        lines.append(
            f"{entry['request']:>3} {entry['prompt_chars']:>12} "
            f"{entry['tool_results']:>12} {entry['retained_tool_chars']:>19} {est:>10}"
        )
    lines.append("")
    lines.append(f"requests: {len(requests)}")
    lines.append(f"estimated input tokens (chars/4): {total_est}")
    usage = (summary.get("events") or {}).get("usage") or {}
    lines.append(f"recorded usage: {json.dumps(usage, sort_keys=True)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the raw measurement")
    parser.add_argument("--limit", type=int, default=150_000)
    parser.add_argument(
        "--retained-chars",
        type=int,
        default=None,
        help="override the context retention bound to compare policies",
    )
    parser.add_argument(
        "--unbounded",
        action="store_true",
        help="reproduce the pre-fix behaviour: never elide any tool result",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "read each fixture file once instead of repeating reads; a "
            "counterfactual for read discipline, not a measured provider saving"
        ),
    )
    arguments = parser.parse_args()
    reads = sorted(dict.fromkeys(READ_SEQUENCE)) if arguments.once else None
    summary, _ = run(
        limit=arguments.limit,
        retained_chars=arguments.retained_chars,
        reads=reads,
        unbounded=arguments.unbounded,
    )
    print(json.dumps(summary, indent=2, default=str) if arguments.json else report(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
