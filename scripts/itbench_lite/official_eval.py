"""Wrapper around the pinned official ITBench evaluator (#582, L6/L7).

The evaluator ``ITBench-Evaluations@14f026fc`` lives outside the repository in
its own venv (L7); nothing here imports it.  It reads ``JUDGE_BASE_URL``,
``JUDGE_API_KEY`` and ``JUDGE_MODEL`` (``client.py``), and its judge must
support tool calls (``calculator_tool``).

Judge receiver per L6 (user ruling 2026-11-13: a): one of the receivers the
repository already uses (Z.ai, OpenAI, DeepSeek), never OpenRouter.  The
provider is ``ITBENCH_JUDGE_PROVIDER`` (default ``zai``); the key, base URL and
model come from the same variables as ``evals/judge.env.example``, and
``ITBENCH_JUDGE_MODEL`` overrides the model.  Results are labelled as using a
judge that differs from the official example.

The default mode is a dry run that prints the command and a redacted
environment.  ``--run`` is the only path that calls the judge.

L5: a trial whose ``conversion_meta.json`` says ``report_present: false`` is
never sent to the judge.  ``run`` stages only report-bearing trials for the
evaluator, then ``trial_scores`` writes every trial back with the reportless
ones at 0 on every metric, so they stay in the denominator of the published
table (``coverage.py``).  The evaluator's own ``statistics`` block excludes
them and is not the published number.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scripts.itbench_lite.coverage import scenario_key

EVALUATOR_COMMIT = "14f026fc"
EVALUATOR_DIR_ENV = "ITBENCH_EVALUATIONS_DIR"
PROVIDER_ENV = "ITBENCH_JUDGE_PROVIDER"
MODEL_OVERRIDE_ENV = "ITBENCH_JUDGE_MODEL"
DEFAULT_PROVIDER = "zai"
OFFICIAL_EXAMPLE_JUDGE = "gemini-2.5-pro via OpenRouter"
META_FILE = "conversion_meta.json"

# Flattened metric keys, as the pinned aggregator.py derives them from the
# judge's ``calculation*`` fields.
_METRIC_NAMES = (
    "root_cause_entity",
    "root_cause_entity_k",
    "root_cause_reasoning",
    "propagation_chain",
    "fault_localization_component_identification",
    "root_cause_reasoning_partial",
    "root_cause_proximity_no_fp",
    "root_cause_proximity_with_fp",
)
_VALUE_KEYS = {
    "calculation": "",
    "calculation_precision": "_precision",
    "calculation_recall": "_recall",
    "calculation_f1": "_f1",
}
METRIC_KEYS = (
    "root_cause_entity_precision",
    "root_cause_entity_recall",
    "root_cause_entity_f1",
    "root_cause_entity_k_precision",
    "root_cause_entity_k_recall",
    "root_cause_entity_k_f1",
    "root_cause_reasoning",
    "propagation_chain",
    "fault_localization_component_identification",
    "root_cause_reasoning_partial",
    "root_cause_proximity_no_fp_precision",
    "root_cause_proximity_no_fp_recall",
    "root_cause_proximity_no_fp_f1",
    "root_cause_proximity_with_fp_precision",
    "root_cause_proximity_with_fp_recall",
    "root_cause_proximity_with_fp_f1",
)


@dataclass(frozen=True)
class ProviderSpec:
    key_env: str
    base_env: str
    base_default: str
    model_env: str | None
    model_default: str | None


# Same receivers, variables and defaults as opspilot/evaluation/judge.py.
PROVIDERS: Mapping[str, ProviderSpec] = {
    "zai": ProviderSpec(
        "ZAI_API_KEY",
        "ZAI_BASE_URL",
        "https://api.z.ai/api/paas/v4",
        "OPSPILOT_JUDGE_MODEL",
        "glm-5.3-flashx",
    ),
    "openai": ProviderSpec(
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
        "OPENAI_JUDGE_MODEL",
        "gpt-5.6-luna",
    ),
    "deepseek": ProviderSpec(
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com",
        None,
        "deepseek-flash",
    ),
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class JudgeConfig:
    provider: str
    base_url: str
    model: str
    api_key: str

    def labels(self) -> dict[str, object]:
        """What every published official score must carry."""

        return {
            "judge_provider": self.provider,
            "judge_model": self.model,
            "judge_differs_from_official_example": True,
            "official_example_judge": OFFICIAL_EXAMPLE_JUDGE,
            "evaluator": f"ITBench-Evaluations@{EVALUATOR_COMMIT}",
            "entity_conversion": "启发式",
            "propagation_chain": "受转换限制",
        }

    def redacted(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "base_url": _strip_userinfo(self.base_url),
            "model": self.model,
            "api_key": "<set>" if self.api_key else "<missing>",
        }


def _strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def judge_config(environ: Mapping[str, str] | None = None) -> JudgeConfig:
    env = os.environ if environ is None else environ
    provider = (env.get(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().casefold()
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ConfigError(
            f"{PROVIDER_ENV}={provider!r} is not an existing receiver; choose one of {sorted(PROVIDERS)}"
        )
    model = (
        env.get(MODEL_OVERRIDE_ENV)
        or (env.get(spec.model_env) if spec.model_env else None)
        or spec.model_default
        or ""
    ).strip()
    if not model:
        raise ConfigError("judge model is not configured")
    base_url = (env.get(spec.base_env) or spec.base_default).strip()
    return JudgeConfig(provider, base_url, model, (env.get(spec.key_env) or "").strip())


def evaluator_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    value = env.get(EVALUATOR_DIR_ENV)
    if not value:
        raise ConfigError(f"{EVALUATOR_DIR_ENV} is not set")
    return Path(value).expanduser()


def check_pinned(directory: Path) -> str:
    """Return the evaluator checkout's HEAD; refuse anything but the pin."""

    head = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not head.startswith(EVALUATOR_COMMIT):
        raise ConfigError(f"evaluator at {head[:12]}, expected {EVALUATOR_COMMIT}")
    return head


def evaluator_python(directory: Path) -> Path:
    return directory / ".venv" / "bin" / "python"


def build_command(
    directory: Path,
    ground_truth: Path,
    outputs: Path,
    result_file: Path,
    *,
    criteria: Sequence[str] = (),
    max_concurrent: int = 1,
) -> list[str]:
    directory, ground_truth, outputs, result_file = (
        Path(p).expanduser().resolve()
        for p in (directory, ground_truth, outputs, result_file)
    )
    command = [
        str(evaluator_python(directory)),
        "-m",
        "itbench_evaluations",
        "--ground-truth",
        str(ground_truth),
        "--outputs",
        str(outputs),
        "--domain",
        "sre",
        "--result-file",
        str(result_file),
        "--max-concurrent",
        str(max_concurrent),
    ]
    if criteria:
        command += ["--eval-criteria", *criteria]
    return command


def subprocess_env(
    config: JudgeConfig, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A minimal child environment: only the judge variables and basic paths.

    Other credentials in the parent environment are not passed through.
    """

    env = os.environ if environ is None else environ
    child = {key: env[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in env}
    child.update(
        {
            # client.py calls load_dotenv(), which searches the evaluator's
            # directory and its parents; python-dotenv >= 1.1 honours this.
            "PYTHON_DOTENV_DISABLED": "1",
            "JUDGE_BASE_URL": config.base_url,
            "JUDGE_API_KEY": config.api_key,
            "JUDGE_MODEL": config.model,
        }
    )
    return child


def _trial_dirs(outputs: Path) -> list[tuple[str, int, Path]]:
    found = []
    for scenario in sorted(p for p in outputs.iterdir() if p.is_dir()):
        for trial in sorted(p for p in scenario.iterdir() if p.is_dir()):
            if trial.name.isdigit():
                found.append((scenario.name, int(trial.name), trial))
    return found


def report_present(trial_dir: Path) -> bool:
    """Read ``report_present`` from the trial's conversion sidecar."""

    meta_path = trial_dir / META_FILE
    if not meta_path.is_file():
        raise ConfigError(
            f"{meta_path} is missing; write trials with answer.write_trial"
        )
    value = json.loads(meta_path.read_text()).get("report_present")
    if not isinstance(value, bool):
        raise ConfigError(f"{meta_path} has no boolean report_present")
    return value


def stage_judged(outputs: Path, staging: Path) -> list[tuple[str, int]]:
    """Copy only report-bearing trials to ``staging``; return the reportless ones."""

    if staging.exists():
        raise ConfigError(f"{staging} already exists")
    staging.mkdir(parents=True)
    reportless = []
    for scenario, trial, path in _trial_dirs(outputs):
        if report_present(path):
            shutil.copytree(path, staging / scenario / str(trial))
        else:
            reportless.append((scenario, trial))
    return reportless


def _flatten(scores: Mapping[str, object]) -> dict[str, float]:
    flat: dict[str, float] = {}
    for metric in _METRIC_NAMES:
        data = scores.get(metric)
        if isinstance(data, Mapping):
            for value_key, suffix in _VALUE_KEYS.items():
                value = data.get(value_key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    flat[metric + suffix] = float(value)
        elif isinstance(data, (int, float)) and not isinstance(data, bool):
            flat[metric] = float(data)
    return flat


def trial_scores(
    results: Mapping[str, object], outputs: Path
) -> dict[str, list[dict[str, float]]]:
    """Per-scenario trial scores for ``coverage.build_table``.

    Judged trials take the evaluator's flattened scores (a failed judge call
    leaves them empty, which the table counts as 0).  Reportless trials get 0
    on every metric whatever the evaluator said (L5).
    """

    judged: dict[tuple[str, int], dict[str, float]] = {}
    for item in results.get("raw_results") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            key = (
                scenario_key(str(item.get("incident_id"))),
                int(str(item.get("trial_id"))),
            )
        except ValueError:
            continue
        scores = item.get("scores")
        judged[key] = _flatten(scores) if isinstance(scores, Mapping) else {}
    table: dict[str, list[dict[str, float]]] = {}
    for scenario, trial, path in _trial_dirs(outputs):
        key = (scenario_key(scenario), trial)
        if report_present(path):
            row = judged.get(key, {})
        else:
            row = {metric: 0.0 for metric in METRIC_KEYS}
        table.setdefault(key[0], []).append(row)
    return table


def run(
    directory: Path,
    ground_truth: Path,
    outputs: Path,
    work_dir: Path,
    config: JudgeConfig,
    *,
    criteria: Sequence[str] = (),
    max_concurrent: int = 1,
) -> Path:
    """Call the official evaluator; this sends requests to the judge.

    Returns ``trial_scores.json`` in ``work_dir``.  ``work_dir`` is the
    child's working directory and must not contain a ``.env``.  All paths are
    resolved before the child starts, so relative arguments keep their
    meaning.
    """

    directory, ground_truth, outputs, work_dir = (
        Path(p).expanduser().resolve()
        for p in (directory, ground_truth, outputs, work_dir)
    )
    if not config.api_key:
        raise ConfigError(f"{PROVIDERS[config.provider].key_env} is not set")
    check_pinned(directory)
    work_dir.mkdir(parents=True, exist_ok=True)
    if (work_dir / ".env").exists():
        raise ConfigError(f"{work_dir}/.env would be loaded by the evaluator")
    staging = work_dir / "judged_outputs"
    stage_judged(outputs, staging)
    result_file = work_dir / "evaluation_results.json"
    subprocess.run(
        build_command(
            directory,
            ground_truth,
            staging,
            result_file,
            criteria=criteria,
            max_concurrent=max_concurrent,
        ),
        check=True,
        cwd=work_dir,
        env=subprocess_env(config),
    )
    (work_dir / "judge_labels.json").write_text(
        json.dumps(config.labels(), ensure_ascii=False, indent=2) + "\n"
    )
    scores_file = work_dir / "trial_scores.json"
    scores_file.write_text(
        json.dumps(trial_scores(json.loads(result_file.read_text()), outputs), indent=2)
        + "\n"
    )
    return scores_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--outputs", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--criteria", nargs="*", default=())
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--run", action="store_true", help="actually call the judge")
    args = parser.parse_args(argv)
    try:
        config = judge_config()
        directory = evaluator_dir()
    except ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    if not args.run:
        command = build_command(
            directory,
            args.ground_truth,
            args.outputs,
            args.work_dir / "evaluation_results.json",
            criteria=args.criteria,
            max_concurrent=args.max_concurrent,
        )
        print(
            json.dumps(
                {
                    "command": command,
                    "judge": config.redacted(),
                    "labels": config.labels(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(
        run(
            directory,
            args.ground_truth,
            args.outputs,
            args.work_dir,
            config,
            criteria=args.criteria,
            max_concurrent=args.max_concurrent,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
