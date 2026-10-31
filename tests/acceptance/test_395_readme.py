"""Offline acceptance checks for issue #395's bilingual deployment README."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
INSTALLER = ROOT / "deploy/compose/install.sh"
UPGRADE = ROOT / "deploy/compose/UPGRADE.md"
EVIDENCE = ROOT / "docs/evidence/f9-393-installed-investigation/README.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _installer_subcommands() -> set[str]:
    source = _read(INSTALLER)
    match = re.search(r"case \"\$command\" in(?P<body>.*?)\n\s*esac", source, re.S)
    assert match, "install.sh must retain a parseable command dispatch"
    return set(
        re.findall(
            r"^\s*(install|upgrade|uninstall|compose)\)", match.group("body"), re.M
        )
    )


def _installer_env_names() -> set[str]:
    source = _read(INSTALLER)
    settings = re.search(r"readonly SETTINGS=\((?P<body>.*?)\)", source, re.S)
    assert settings, "install.sh must retain its documented settings list"
    names = set(re.findall(r"OPSPILOT_[A-Z0-9_]+", settings.group("body")))
    # This directory setting is documented beside SETTINGS and is part of the
    # operator-facing install contract.
    names.add("OPSPILOT_SECRETS_DIR")
    return names


def _fenced_shell_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:bash|sh|shell)\s*\n(.*?)```", text, re.S | re.I)


def _join_continued_lines(block: str) -> str:
    lines = block.splitlines()
    joined: list[str] = []
    for line in lines:
        if joined and joined[-1].rstrip().endswith("\\"):
            joined[-1] = joined[-1].rstrip()[:-1] + " " + line.strip()
        else:
            joined.append(line)
    return "\n".join(joined)


def _install_invocations(block: str) -> list[tuple[str, str]]:
    """Return (line, subcommand) for every install.sh call in a shell block."""
    calls: list[tuple[str, str]] = []
    block = _join_continued_lines(block)
    for line in block.splitlines():
        if "install.sh" not in line or line.lstrip().startswith("#"):
            continue
        after = line.split("install.sh", 1)[1].strip()
        command = after.split("#", 1)[0].split()
        calls.append((line, command[0] if command else ""))
    return calls


def _documented_subcommands(text: str) -> set[str]:
    return set(re.findall(r"install\.sh\s+(install|upgrade|uninstall|compose)\b", text))


def _documented_env_names(text: str) -> set[str]:
    return set(re.findall(r"\b(?:OPSPILOT_[A-Z0-9_]+|DEEPSEEK_API_KEY)\b", text))


def _walkthrough_text(text: str) -> str:
    return _join_continued_lines("\n".join(_fenced_shell_blocks(text)))


def test_bilingual_readmes_exist_and_link_to_each_other_near_top() -> None:
    for path in READMES:
        assert path.is_file(), f"missing bilingual README: {path.name}"
    english_top = "\n".join(_read(READMES[0]).splitlines()[:10])
    chinese_top = "\n".join(_read(READMES[1]).splitlines()[:10])
    assert "README.zh-CN.md" in english_top
    assert "README.md" in chinese_top


def test_install_walkthrough_uses_real_subcommands_and_env_names() -> None:
    known_commands = _installer_subcommands()
    known_envs = _installer_env_names()
    for path in READMES:
        text = _read(path)
        blocks = _fenced_shell_blocks(text)
        assert blocks, f"{path.name} needs a fenced install.sh walkthrough"
        for block in blocks:
            for line, command in _install_invocations(block):
                assert command in known_commands, (
                    f"{path.name} uses an unknown or missing install.sh subcommand "
                    f"in: {line}"
                )
            for name in re.findall(r"\bOPSPILOT_[A-Z0-9_]+\b", block):
                assert name in known_envs, (
                    f"{path.name} uses undocumented installer env {name}"
                )


def test_walkthrough_order_matches_upgrade_and_installed_evidence() -> None:
    upgrade = _read(UPGRADE)
    evidence = _read(EVIDENCE)
    # These anchors are taken from the operator procedure and the recorded run;
    # README prose may be translated or rephrased around them.
    assert "install.sh upgrade" in upgrade
    assert "worker_env" in upgrade and "observer_env" in upgrade
    assert "OPSPILOT_START_AGENT=1" in upgrade
    assert "starting runtimes" in evidence.lower()

    for path in READMES:
        walkthrough = _walkthrough_text(_read(path))
        assert walkthrough, f"{path.name} has no install walkthrough"
        positions = {
            "install": [
                m.start() for m in re.finditer(r"install\.sh\s+install\b", walkthrough)
            ],
            "worker_env": [
                m.start()
                for m in re.finditer(r"worker_env|DEEPSEEK_API_KEY", walkthrough)
            ],
            "observer": [
                m.start()
                for m in re.finditer(
                    r"observer_env|OPSPILOT_OBSERVER_PROMETHEUS_", walkthrough
                )
            ],
            "agent_upgrade": [
                m.start()
                for m in re.finditer(
                    r"OPSPILOT_START_AGENT=1[^\n]*install\.sh\s+upgrade\b", walkthrough
                )
            ],
            "workbench": [
                m.start() for m in re.finditer(r"workbench|工作台", _read(path), re.I)
            ],
        }
        missing = [name for name, hits in positions.items() if not hits]
        assert not missing, f"{path.name} walkthrough is missing: {', '.join(missing)}"
        order = [
            positions[name][0]
            for name in ("install", "worker_env", "observer", "agent_upgrade")
        ]
        assert order == sorted(order), (
            f"{path.name} walkthrough order is not install → credentials → agent upgrade"
        )
        assert positions["workbench"][0] > positions["agent_upgrade"][0], (
            f"{path.name} must open the workbench after the agent upgrade"
        )


def test_bilingual_readmes_describe_the_same_install_surface() -> None:
    english, chinese = (_read(path) for path in READMES)
    assert (
        _documented_subcommands(english)
        == _documented_subcommands(chinese)
        == _installer_subcommands()
    )
    expected_envs = _installer_env_names() | {
        "DEEPSEEK_API_KEY",
        "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME",
        "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD",
        "OPSPILOT_OBSERVER_PROMETHEUS_TOKEN",
    }
    english_envs = _documented_env_names(english)
    chinese_envs = _documented_env_names(chinese)
    assert english_envs & expected_envs == expected_envs
    assert chinese_envs & expected_envs == expected_envs
    assert english_envs & expected_envs == chinese_envs & expected_envs


@pytest.mark.parametrize(
    ("path", "phrase"),
    [
        (ROOT / "README.md", "Implementation has not started"),
        (ROOT / "README.zh-CN.md", "Implementation has not started"),
        (ROOT / ".env.example", "live 还需独立的私有批准合同"),
        (ROOT / ".env.example", "仅填本文件不能授权调用"),
        (ROOT / ".env.example", "额度与期限必须另有任务中的授权依据"),
        (ROOT / "opspilot/web/__main__.py", "not a deployment artifact"),
        (ROOT / "opspilot/worker_main.py", "not a deployment artifact"),
    ],
)
def test_b13_stale_wording_is_gone(path: Path, phrase: str) -> None:
    assert phrase not in _read(path)


def test_readmes_and_env_example_contain_no_secret_looking_values() -> None:
    assignment = re.compile(
        r"(?im)^\s*(?:[A-Z][A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|SECRET)[A-Z0-9_]*)\s*=\s*([^#\s]+)"
    )
    placeholders = {
        "",
        "...",
        "<value>",
        "<token>",
        "<password>",
        "your_key",
        "changeme",
    }
    for path in (*READMES, ROOT / ".env.example"):
        for value in assignment.findall(_read(path)):
            normalized = value.strip("'\"").lower()
            if re.match(r"^https?://[^/@\s]+(?:/[^\s]*)?$", normalized):
                continue
            assert normalized in placeholders or normalized.startswith(("${", "<")), (
                f"secret-looking value in {path.name}: {value}"
            )


def test_english_readme_declares_chinese_contract_documents_authoritative() -> None:
    english = _read(ROOT / "README.md").lower()
    assert "chinese contract" in english or "中文" in english
    assert (
        "authoritative" in english or "take precedence" in english or "权威" in english
    )
    assert any(word in english for word in ("conflict", "disagree", "differ", "冲突"))
