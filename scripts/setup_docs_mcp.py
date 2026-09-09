"""为指定项目配置公共 LangChain 文档 MCP；不修改用户级配置或调用模型。"""

import argparse
import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path

SERVERS = {
    "langchain-docs": {
        "url": "https://docs.langchain.com/mcp",
        "tools": [
            "search_docs_by_lang_chain",
            "query_docs_filesystem_docs_by_lang_chain",
        ],
    },
    "langchain-reference": {
        "url": "https://reference.langchain.com/mcp",
        "tools": ["search_api", "get_symbol"],
    },
}


class RollbackError(OSError):
    """Publication failed and at least one target needs manual recovery."""


def read_config(path, parser):
    if path.is_symlink():
        raise ValueError(f"拒绝覆盖符号链接：{path.name}")
    if not path.exists():
        return "", {}
    text = path.read_text()
    try:
        data = parser(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"无法解析现有配置：{path.name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"现有配置不是对象：{path.name}")
    return text, data


def prepare(root):
    if not (root / "AGENTS.md").is_file() or not (root / "SPEC.md").is_file():
        raise ValueError("目标必须是含 AGENTS.md 与 SPEC.md 的项目根目录")
    for parent in [root / ".codex", root / ".claude"]:
        if parent.exists() and not parent.is_dir():
            raise ValueError("配置目录必须是目录")
        if parent.is_symlink():
            raise ValueError("拒绝写入符号链接配置目录")
    codex = root / ".codex/config.toml"
    mcp = root / ".mcp.json"
    settings = root / ".claude/settings.local.json"
    text, data = read_config(codex, tomllib.loads)
    _, mcp_data = read_config(mcp, json.loads)
    _, settings_data = read_config(settings, json.loads)
    codex_servers = data.get("mcp_servers", {})
    claude_servers = mcp_data.setdefault("mcpServers", {})
    if not isinstance(codex_servers, dict) or not isinstance(claude_servers, dict):
        raise ValueError("现有 MCP 配置格式错误")
    for name, server in SERVERS.items():
        desired = {
            "url": server["url"],
            "enabled_tools": server["tools"],
            "startup_timeout_sec": 20,
            "tool_timeout_sec": 60,
        }
        if name in codex_servers:
            if codex_servers[name] != desired:
                raise ValueError(f"保留现有同名配置，需核查差异：{name}")
        else:
            text += f"\n[mcp_servers.{name}]\n"
            for key, value in desired.items():
                text += f"{key} = {json.dumps(value)}\n"
        desired_claude = {"type": "http", "url": server["url"]}
        if name in claude_servers and claude_servers[name] != desired_claude:
            raise ValueError(f"保留现有同名配置，需核查差异：{name}")
        claude_servers[name] = desired_claude
    enabled = settings_data.setdefault("enabledMcpjsonServers", [])
    permissions = settings_data.setdefault("permissions", {})
    if not isinstance(enabled, list) or not isinstance(permissions, dict):
        raise ValueError("现有 Claude 设置格式错误")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ValueError("现有 Claude deny 格式错误")
    for name in SERVERS:
        if name not in enabled:
            enabled.append(name)
    feedback = "mcp__langchain-docs__submit_feedback"
    if feedback not in deny:
        deny.append(feedback)
    # inline table 等合法旧写法不一定允许追加子表，写入前复验完整 TOML。
    tomllib.loads(text)
    # 保留其余设置，不授予未来服务器或任意工具自动调用权限。
    return {
        codex: text,
        mcp: json.dumps(mcp_data, indent=2, ensure_ascii=False) + "\n",
        settings: json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n",
    }


def install(root):
    changes = prepare(root)
    staged = {}
    backups = {}
    replaced = []
    created_dirs = []
    retained_backups = set()
    try:
        # Stage all new bytes and rollback copies before publishing any registration.
        for path, text in changes.items():
            if path.exists() and path.read_text() == text:
                continue
            if not path.parent.exists():
                path.parent.mkdir()
                created_dirs.append(path.parent)
            if path.exists():
                backups[path] = stage_file(
                    path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                )
            else:
                backups[path] = None
            staged[path] = stage_file(path, text.encode(), 0o600)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except OSError:
        rollback_failed = False
        for path in reversed(replaced):
            backup = backups[path]
            try:
                if backup is None:
                    path.unlink()
                else:
                    os.replace(backup, path)
            except OSError:
                rollback_failed = True
                if backup is not None:
                    retained_backups.add(backup)
        if rollback_failed:
            raise RollbackError("MCP_ROLLBACK_INCOMPLETE") from None
        raise
    finally:
        for temporary in [*staged.values(), *backups.values()]:
            if temporary is not None and temporary not in retained_backups:
                temporary.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            if not any(directory.iterdir()):
                directory.rmdir()
    return list(changes)


def stage_file(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=".mcp-stage-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        paths = install(args.project.resolve())
    except RollbackError:
        parser.exit(
            1,
            "配置回滚未完成：请暂停加载本项目 MCP，核查三个目标配置；"
            "未恢复的原件保留在对应目录的 .mcp-stage-*，请保留并恢复后重试。\n",
        )
    except (ValueError, OSError):
        # 不输出配置内容或解析异常，避免泄漏已有配置的私有值。
        parser.exit(1, "配置未完成：目标、格式或同名配置冲突；请保留现有文件核查。\n")
    for path in paths:
        print(f"配置已核对：{path}")
    print("仅项目配置；宿主信任/加载与远程查询须另行验证。")


if __name__ == "__main__":
    main()
