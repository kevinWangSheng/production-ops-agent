"""只读环境诊断；不安装依赖、不启动服务，不证明虚拟环境与锁完全一致。"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def probe(argv, cwd):
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def report(label, ok, detail):
    print(f"{'OK' if ok else '缺失/不可用'} | {label} | {detail}")
    return ok


def diagnose(root, lab=False):
    checks = []
    ok, output = probe(["git", "rev-parse", "--show-toplevel"], root)
    checks.append(
        report("项目根目录", ok and Path(output).resolve() == root, str(root))
    )
    checks.append(
        report("项目声明", (root / "pyproject.toml").is_file(), "pyproject.toml")
    )
    checks.append(report("锁文件存在", (root / "uv.lock").is_file(), "仅检查存在"))
    uv = shutil.which("uv")
    ok, output = probe([uv, "--version"], root) if uv else (False, "")
    checks.append(report("uv", ok, output if ok else "需安装 uv"))

    python = root / ".venv/bin/python"
    code = (
        "import sys,json,importlib.metadata as m; "
        "print(json.dumps({'version':list(sys.version_info[:3]),"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'tools':{n:m.version(n) for n in ['pytest','ruff']}}))"
    )
    ok, output = probe([str(python), "-I", "-B", "-c", code], root)
    try:
        data = json.loads(output) if ok else {}
        version = data.get("version", [])
        valid = (
            version[:2] == [3, 12]
            and Path(data.get("prefix", "")).resolve() == (root / ".venv").resolve()
            and data.get("prefix") != data.get("base_prefix")
            and (root / ".venv/bin/ruff").is_file()
        )
        detail = f"Python {version}; tools={data.get('tools', {})}"
    except (ValueError, TypeError, AttributeError):
        valid, detail = False, "无法读取环境版本"
    checks.append(report("项目 Python 3.12 与开发工具", valid, detail))
    ready = all(checks)
    print("基础开发环境：" + ("前提可用" if ready else "未就绪；检查后运行 make setup"))
    print("环境诊断不证明依赖完全匹配锁文件；同步由 make setup 完成。")

    lab_ready = True
    if lab:
        for label, argv in [
            ("Docker 客户端", ["docker", "--version"]),
            ("Docker Compose", ["docker", "compose", "version"]),
            ("Docker daemon", ["docker", "info", "--format", "{{.ServerVersion}}"]),
            ("kubectl", ["kubectl", "version", "--client=true"]),
            ("Helm", ["helm", "version", "--short"]),
        ]:
            ok, _ = probe(argv, root)
            lab_ready = report(label, ok, "只读探测，不自动准备") and lab_ready
        print("实验设施：" + ("工具可用；非实验验收" if lab_ready else "未就绪"))
    return 1 if not ready else (2 if lab and not lab_ready else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", action="store_true", help="额外探测实验工具与 daemon")
    args = parser.parse_args()
    return diagnose(ROOT, args.lab)


if __name__ == "__main__":
    raise SystemExit(main())
