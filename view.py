# 生成每个 Agent 的唯一 router stub；不复制正文/资源，不使用 junction。
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / "catalog"
CONFIG = CATALOG / "agents.json"
ROUTER_ID = "agentlib-router"
sys.path.insert(0, str(ROOT / "scripts"))
import aglib  # noqa: E402


def load_config() -> dict:
    if not CONFIG.is_file():
        raise SystemExit(f"缺少 {CONFIG}")
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("catalog/agents.json 必须是对象")
    return data


def stub_text(agent_id: str) -> str:
    return (
        "---\n"
        f"name: {ROUTER_ID}\n"
        "description: >-\n"
        "  在 agent-library 中按意图查找技能。任何任务开始前先调用它，拿到候选 skill 路径后再读正文；\n"
        "  不要枚举 catalog 全部技能，也不要把几千条描述塞进上下文。\n"
        "---\n\n"
        "# agent-library router\n\n"
        "先运行：\n\n"
        "```text\n"
        f'py -3 {ROOT / "router.py"} route --agent {agent_id} "<用户原话>"\n'
        "```\n\n"
        "- 返回 `intent`、`slots` 和 top 候选 skill；按第一候选的 `path` 读取 `catalog/<id>/SKILL.md`。\n"
        "- 如果候选冲突或为空，先问用户，不要猜。\n"
        f'- 要查看某个 skill 的路径：`py -3 {ROOT / "router.py"} load --agent {agent_id} <id>`。\n'
        "## 常驻约束\n"
        "- 不要为了省事直接改单个补丁；先按 router 选中的诊断/实现 skill 走。\n"
        "- 任务进行中不要因为 skill 不好用停下来问；先完成交付，把摩擦记到 state/friction.jsonl，\n"
        "  收工后再统一改。\n"
        "- 任务搞定后主动问一次：刚才哪个 skill/流程让你临时找了步骤？要不要现在补成脚本+eval？\n"
        "  用户说不用就停；不要静默改共享正文，也不要在中途停下来问。\n"
    )


def backup(target: Path, agent_id: str) -> None:
    if not target.is_file():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ROOT / "state" / "backups" / f"router-{agent_id}-{stamp}" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dest)
    print(f"  备份：{dest}")


def apply_agent(agent_id: str, info: dict, clean_legacy: bool) -> None:
    root = Path(str(info["skillRoot"])).expanduser()
    target = root / ROUTER_ID / "SKILL.md"
    text = stub_text(agent_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        print(f"{agent_id}: router 已是最新")
    else:
        backup(target, agent_id)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"{agent_id}: 写入 {target}")
    if clean_legacy and root.is_dir():
        catalog_ids = {item["id"] for item, _folder in aglib.catalog_items(CATALOG)}
        removed = 0
        for child in root.iterdir():
            if child.name in {ROUTER_ID, ".disable_to_model_invocation_migration.json"}:
                continue
            if child.is_dir() and child.name in catalog_ids:
                shutil.rmtree(child)
                removed += 1
        if removed:
            print(f"{agent_id}: 清理旧副本 {removed} 个（源仍在 catalog）")


def verify_agent(agent_id: str, info: dict) -> int:
    root = Path(str(info["skillRoot"])).expanduser()
    target = root / ROUTER_ID / "SKILL.md"
    text = stub_text(agent_id)
    if not target.is_file():
        print(f"ERROR {agent_id}: 缺 {target}")
        return 1
    if target.read_text(encoding="utf-8") != text:
        print(f"ERROR {agent_id}: router stub 与当前版本不一致")
        return 1
    print(f"OK {agent_id}: {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 agent-library router stub")
    parser.add_argument("--agent", action="append", default=[], help="agent id，可重复")
    parser.add_argument("--all", action="store_true", help="catalog/agents.json 里的全部 Agent")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加只打印")
    parser.add_argument("--verify", action="store_true", help="只校验")
    parser.add_argument("--clean-legacy", action="store_true", help="删除 Agent 目录里与 catalog 同名的旧副本")
    args = parser.parse_args(argv)
    config = load_config()
    agents = config.get("agents") or {}
    ids = sorted(agents) if args.all else sorted(set(args.agent))
    if not ids:
        raise SystemExit("用 --agent <id> 或 --all")
    problems = 0
    for agent_id in ids:
        info = agents.get(agent_id)
        if not info:
            print(f"ERROR 未知 Agent：{agent_id}", file=sys.stderr)
            problems += 1
            continue
        if args.verify:
            problems += verify_agent(agent_id, info)
        elif args.apply:
            apply_agent(agent_id, info, args.clean_legacy)
        else:
            print(f"{agent_id} -> {info['skillRoot']}\\{ROUTER_ID}\\SKILL.md")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
