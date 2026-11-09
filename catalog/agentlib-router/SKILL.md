---
name: agentlib-router
description: >-
  在 agent-library 中按意图查找技能。任何任务开始前先调用它，拿到候选 skill 路径后再读正文；
  不要枚举 catalog 全部技能，也不要把几千条描述塞进上下文。
---

# agent-library router

从**库根目录**运行（本文件所在仓库）：

```text
python router.py route --agent dsh "<用户原话>"
```

- 返回 `intent`、`slots` 和 top 候选 skill；按第一候选的 `path` 读取 `catalog/<id>/SKILL.md`。
- 如果候选冲突或为空，先问用户，不要猜。
- 要查看某个 skill 的路径：`python router.py load --agent dsh <id>`。
- 库不在当前目录时，用 `AGLIB_STORE` 或 `--store` 指定。

各 Agent 目录里的这份 stub 由 `view.py --all --apply` 生成，内容与本文件一致；不要手改各 Agent
目录里的副本，改 `catalog/agentlib-router/SKILL.md` 后重新投影。

## 常驻约束

- 不要为了省事直接改单个补丁；先按 router 选中的诊断/实现 skill 走。
- 任务进行中不要因为 skill 不好用停下来问；先完成交付，把摩擦记到 `state/friction.jsonl`，
  收工后再统一改。
- 任务搞定后主动问一次：刚才哪个 skill/流程让你临时找了步骤？要不要现在补成脚本+eval？
  用户说不用就停；不要静默改共享正文，也不要在中途停下来问。
