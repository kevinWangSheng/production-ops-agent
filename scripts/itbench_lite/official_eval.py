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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EVALUATOR_COMMIT = "14f026fc"
EVALUATOR_DIR_ENV = "ITBENCH_EVALUATIONS_DIR"
PROVIDER_ENV = "ITBENCH_JUDGE_PROVIDER"
MODEL_OVERRIDE_ENV = "ITBENCH_JUDGE_MODEL"
DEFAULT_PROVIDER = "zai"
OFFICIAL_EXAMPLE_JUDGE = "gemini-2.5-pro via OpenRouter"


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
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "<set>" if self.api_key else "<missing>",
        }


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
            "JUDGE_BASE_URL": config.base_url,
            "JUDGE_API_KEY": config.api_key,
            "JUDGE_MODEL": config.model,
        }
    )
    return child


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

    ``work_dir`` is the child's working directory: the evaluator calls
    ``load_dotenv()``, so it must not contain a ``.env``.
    """

    if not config.api_key:
        raise ConfigError(f"{PROVIDERS[config.provider].key_env} is not set")
    check_pinned(directory)
    work_dir.mkdir(parents=True, exist_ok=True)
    if (work_dir / ".env").exists():
        raise ConfigError(f"{work_dir}/.env would be loaded by the evaluator")
    result_file = work_dir / "evaluation_results.json"
    subprocess.run(
        build_command(
            directory,
            ground_truth,
            outputs,
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
    return result_file


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
