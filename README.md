# agent-library（示例骨架）

**单一来源**的技能 / 规则库：所有 Agent 都从同一份 `catalog/` 按意图路由取技能，不再把 `SKILL.md`
复制到各自的 skills 目录。这个仓库是**可运行的示例骨架**，只带四个示例条目（含 agentlib 自己的
router 与规则），完整技能库是私有的、不在本仓库里。

## 里面有什么

```text
catalog/
├─ agents.json            # 登记各 Agent 的 skills 根目录与角色（示例路径，请改成自己的）
├─ intents.json           # 意图表：patterns / keywords / excludes / examples -> skills
├─ index.json             # 运行 store.py 生成（.gitignore 忽略）
├─ index.fts.db           # 运行 store.py 生成（.gitignore 忽略）
├─ agentlib-router/       # 示例条目（skill, meta）：按意图找技能的 router stub
│  ├─ SKILL.md
│  └─ manifest.json
├─ write-in-aglib/        # 示例条目（rule, meta）：技能/规则只写 catalog，再投影
│  ├─ RULE.mdc
│  └─ manifest.json
├─ md-table-png/          # 示例条目（skill）：中文 Markdown/HTML 表格导出 PNG
│  ├─ SKILL.md
│  └─ manifest.json
└─ standard-curve/        # 示例条目（skill）：HPLC/UV 标准曲线、LOD/LOQ、浓度反算
   ├─ SKILL.md
   └─ manifest.json
router.py                 # 一句话 -> intent -> 候选 skill（route/search/load）
view.py                   # 给每个 Agent 生成唯一的 router stub（不复制正文）
scripts/
├─ aglib.py               # 库的常量与条目读取
├─ store.py               # 校验并生成 catalog/index.json（含 FTS 索引）
├─ manage_ui.py           # 本地管理前端（浏览条目、意图、Agent 视图）
└─ manage_ui.html
```

条目有两种 `kind`：`skill`（正文 `SKILL.md` + `manifest.json`）和 `rule`（正文 `RULE.mdc` +
`manifest.json`）。条目只存在于 `catalog/`，Agent 侧只放一个固定的 `agentlib-router` stub
（内容与本仓库 `catalog/agentlib-router/SKILL.md` 一致，由 `view.py` 投影）。

## 快速开始

Python 3.9+，无第三方依赖。

```powershell
# 1) 建索引（会写 catalog/index.json 与 catalog/index.fts.db，这两个不进 git）
python scripts/store.py --store .

# 2) 路由：一句话命中哪个 intent / skill
python router.py route --agent dsh "把这张表导出成 PNG 图片"
python router.py route --agent dsh "帮我做一条 HPLC 标准曲线算 LOD 和 LOQ"

# 3) 查某个条目的正文路径
python router.py load --agent dsh md-table-png

# 4) 管理前端（浏览器打开 http://127.0.0.1:8765）
python scripts/manage_ui.py --store .
```

库根目录默认就是本仓库目录（`scripts/aglib.py` 按自身位置推导），也可以用 `AGLIB_STORE`
或 `--store` 指向别处：

```powershell
$env:AGLIB_STORE = 'D:\my-agent-library'
python router.py route --agent dsh "..."
```

## 目录与文件约定

`catalog/<id>/manifest.json` 的关键字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 条目 id（kebab-case），与目录名一致 |
| `kind` | `skill`（`SKILL.md`）或 `rule`（`RULE.mdc`） |
| `mode` | `on-demand`（按需加载）或 `resident`（常驻规则） |
| `when` / `whenNot` | 何时 / 何时不触发（人话，给路由与阅读者看） |
| `category` | 分类，供管理前端分组 |
| `intents` | 关联的意图 id（写进 `catalog/intents.json` 的 `skills` 列表） |
| `keywords` | 兜底检索关键词 |
| `examples` | 用户可能怎么说的样例 |
| `requires` | 能力声明：`filesystem-read` / `filesystem-write` / `shell` / `network` |
| `merge` | 合并策略（共享正文一般 `shared`） |

`catalog/intents.json`：

```jsonc
{
  "schemaVersion": 1,
  "intents": [
    {
      "id": "chemistry-data",
      "category": "science",
      "description": "化学数据分析：HPLC 标准曲线、Jupyter",
      "patterns": ["(标准曲线|calibration curve|hplc|...)"],
      "keywords": ["标准曲线", "HPLC"],
      "excludes": [],
      "examples": ["帮我做 HPLC 标准曲线"],
      "skills": ["standard-curve"]        // 一个 intent 可以挂多个 skill
    }
  ],
  "categories": { "science": ["chemistry-data"] }
}
```

`catalog/agents.json` 登记 Agent 与角色；`view.py` 按它生成 stub：

```powershell
python view.py --all                 # 只打印将写入的内容
python view.py --all --apply         # 真正写入各 Agent 的 skills 目录
python view.py --all --verify        # 校验 stub 是否最新
```

## 加一个新条目

1. 在 `catalog/<id>/` 放 `SKILL.md`（或 `RULE.mdc`）+ `manifest.json`，可参考两个示例；
2. 在 `catalog/intents.json` 加（或复用）一个 intent，把 `<id>` 放进它的 `skills`；
3. `python scripts/store.py --store . --check` 校验引用，再 `python scripts/store.py --store .` 刷新索引；
4. （可选）`python view.py --all --apply` 更新各 Agent 的 router stub。

也可以用配套的创建脚本
[`agent-library-skill-create`](https://github.com/sprw-li/agent-library-skill-create)：一条命令同时写
`SKILL.md`、`manifest.json`、intent 并刷索引。

## 不提交什么

`_trash/`、`secrets/`、`state/`、`catalog/index.json`、`catalog/index.fts.db`、`__pycache__/` 都在
`.gitignore` 里——索引是生成物，运行态与密钥不该进库。

## License

MIT，见 [LICENSE](./LICENSE)。
