# 共享路径、清单加载与投影文本生成。路径优先用环境变量覆盖，便于换机和测试。
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SKIP_DIRS = {"vendor", "node_modules", ".git", "__pycache__", "_trash", ".venv"}
SECRET = re.compile(
    r"(sk-(?:proj|ant|live|or)-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+[A-Za-z0-9._\-]{20,})"
)


class LibraryError(Exception):
    """库内容或路径不符合约定。"""


def default_store() -> Path:
    return Path(__file__).resolve().parents[1]


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


STORE = env_path("AGLIB_STORE", default_store())
ENTRY = env_path("AGLIB_ENTRY", Path.home() / ".agent-library")
CATALOG = STORE / "catalog"
ROLES = STORE / "roles"
ACCESSORS = STORE / "accessors"
MCP = STORE / "mcp"
PROJECTIONS = STORE / "projections"
INDEX = STORE / "index"


def fail(message: str) -> None:
    raise LibraryError(message)


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"找不到文件：{path}")
    except json.JSONDecodeError as exc:
        fail(f"JSON 格式不对：{path}（{exc}）")
    if not isinstance(data, dict):
        fail(f"需要对象：{path}")
    return data


def reject_secrets(text: str, label: str) -> None:
    if SECRET.search(text):
        fail(f"发现疑似密钥，已停止：{label}")


def inside(root: Path, path: Path) -> Path:
    root_r = root.resolve()
    path_r = path.resolve()
    if path_r != root_r and root_r not in path_r.parents:
        fail(f"拒绝写到投影目录以外：{path_r}")
    return path_r


def manifest_paths(catalog: Path) -> list[Path]:
    """按目录遍历 manifest.json，跳过 vendor 等第三方树。"""
    found: list[Path] = []
    if not catalog.is_dir():
        return found
    for current, dirs, files in os.walk(catalog):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if "manifest.json" in files:
            found.append(Path(current) / "manifest.json")
    return sorted(found)


def catalog_items(catalog: Path) -> list[tuple[dict, Path]]:
    """返回 (manifest 内容, 条目目录)，只含 skill/rule。"""
    items: list[tuple[dict, Path]] = []
    for path in manifest_paths(catalog):
        item = load_json(path)
        if item.get("kind") not in {"skill", "rule"}:
            continue
        items.append((item, path.parent))
    return items


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def description(item: dict) -> str:
    when = "、".join(item.get("when") or [])
    when_not = "；".join(item.get("whenNot") or [])
    return f"用户提到这些事才调用：{when}。不要在这些情况调用：{when_not}。"


def skill_text(item: dict, skill_dir: Path, accessor_id: str) -> str:
    body_path = skill_dir / "SKILL.md"
    body = strip_frontmatter(body_path.read_text(encoding="utf-8")).strip()
    if item.get("merge") == "split":
        overlay = skill_dir / "overlays" / f"{accessor_id}.md"
        if overlay.is_file():
            body = body + "\n\n" + overlay.read_text(encoding="utf-8").strip()
    front = (
        "---\n"
        f"name: {item['id']}\n"
        "description: >-\n"
        f"  {description(item)}\n"
        "---\n\n"
    )
    return front + body + "\n"


def resident_text(accessor: dict, role: dict, entry: Path = ENTRY) -> str:
    if role.get("id") == "admin":
        scope = "可以改共享正文、角色和访问员登记。不改其他访问员自己的 overlay。"
    elif role.get("writeScope") == "own":
        scope = "只改自己的分录，不改共享正文，也不改其他访问员。"
    else:
        scope = "只读，不改库里的正文。"
    aid = accessor["id"]
    return (
        f"技能库入口是 {entry}。当前访问员 {aid}，角色 {role['id']}。{scope}\n"
        "具体技能按 catalog 里的 when 再读，没有提到就不调用。常驻规则（kind=rule）在 catalog 里。\n"
        "硬约束：安装/下载/解压/选路径/往 agent 目录写文件前，先读本目录的常驻说明与 rules/；"
        "严格按 catalog 里的规则正文执行。"
        "Skill 列表看不到 agent-library 条目（projectLive=false）不等于没有规则；"
        "搜 catalog 按任务意图对 when，不要只按产品名搜。"
        "禁止用 mklink 或 projectLive 把投影强接进各 Agent 目录。\n"
        "未安装的访问员不投影。密钥不在这个库里。\n"
    )


def grants_cover(requires: list, grants: list) -> bool:
    return set(requires).issubset(set(grants))


def assert_entry(entry: Path, store: Path, require: bool = True) -> Path | None:
    """入口必须是指向 store 的目录联接；require=False 时缺失只跳过。"""
    if not entry.exists():
        if require:
            fail(
                f"入口不存在：{entry}。换机时设置 AGLIB_ENTRY，"
                "或给 project.py 加 --no-entry-check。"
            )
        return None
    if os.name == "nt":
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(entry))
        if attrs == -1 or not (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            fail(f"入口不是目录联接：{entry}")
    resolved = entry.resolve()
    if resolved != store.resolve():
        fail(f"联接目标不是 {store}，实际是 {resolved}")
    return resolved
