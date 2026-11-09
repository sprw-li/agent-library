# 意图路由器：不读 2000 条正文，不跑 embedding；先匹配 intent，再取候选 skill。
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / "catalog"
INDEX = CATALOG / "index.json"
INTENTS = CATALOG / "intents.json"
DOMAINS = CATALOG / "domains.json"

ALIASES = CATALOG / "aliases.json"


def load_aliases() -> dict:
    """别名 → 规范 id。缺文件就是空表；坏文件也不影响路由。"""
    if not ALIASES.is_file():
        return {}
    try:
        data = json.loads(ALIASES.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in (data.get("aliases") or {}).items()}


def load_domains() -> list[dict]:
    """粗粒度域门；文件缺失或坏掉就当没有，退化为全域打分。"""
    if not DOMAINS.is_file():
        return []
    try:
        data = json.loads(DOMAINS.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("domains") or []
sys.path.insert(0, str(ROOT / "scripts"))
import store  # noqa: E402


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"缺少 {path}，先跑 py -3 scripts/store.py")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_index() -> dict:
    """catalog 变了就增量重建索引；不依赖前端或后台。"""
    if INDEX.is_file():
        index = load_json(INDEX)
        if index.get("catalogSignature") == store.catalog_signature(CATALOG):
            return index
        index = store.build_index(ROOT, previous=index)
    else:
        index = store.build_index(ROOT)
    store.write_index(ROOT, index)
    return index


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def tokens(text: str) -> set[str]:
    text = normalize(text)
    found: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fa5]+", text):
        if re.fullmatch(r"[\u4e00-\u9fa5]+", word):
            found.add(word)
            found.update(word[i : i + 2] for i in range(max(0, len(word) - 1)))
        else:
            found.add(word)
    return found


_ASCII_KEYWORD = re.compile(r"^[a-z0-9_]+$")


def keyword_hit(keyword: str, query: str, query_tokens: set[str]) -> bool:
    """关键词是否命中。

    中文没有空格，短语只能按子串找；英文缩写则必须按完整词元找，
    否则 CDX 会命中 CDXML、PPT 会命中 PPTX，凭空给出不存在的关系。
    """
    kw = normalize(str(keyword))
    if not kw:
        return False
    if _ASCII_KEYWORD.fullmatch(kw):
        return kw in query_tokens
    return kw in query


# ---- 词区分度：出现得越少的词，越能说明用户想问什么 ----
_DF_CACHE: dict[str, dict] = {}


def entry_corpus(entry: dict) -> str:
    return " ".join(
        [
            str(entry.get("id") or ""),
            str(entry.get("summary") or ""),
            " ".join(entry.get("keywords") or []),
            " ".join(entry.get("examples") or []),
            " ".join(entry.get("when") or []),
            str(entry.get("category") or ""),
        ]
    )


def token_frequency(index: dict, agent: str | None) -> tuple[dict[str, int], int]:
    """整个 catalog 的词频与条目总数，用来给"到处都有"的词降权。"""
    key = agent or "*"
    cached = _DF_CACHE.get(key)
    if cached:
        return cached["df"], cached["n"]
    df: dict[str, int] = {}
    n = 0
    for entry in index.get("entries") or []:
        n += 1
        for token in tokens(entry_corpus(entry)):
            df[token] = df.get(token, 0) + 1
    _DF_CACHE[key] = {"df": df, "n": n}
    return df, n


def token_weight(token: str, df: dict[str, int], n: int) -> int:
    """0 = 太常见没有区分度，1 = 中等，3 = 稀有（强信号）。"""
    count = df.get(token, 0)
    if count == 0:
        return 0
    if count >= max(5, n * 0.2):
        return 0
    if count >= 3:
        return 1
    return 3


def intent_is_confident(score: int, hits: list[str]) -> bool:
    """intent 算不算"真命中"。

    仅仅撞上一个关键词（比如话里提到 Cursor）不足以判定意图，那样会自信地报错；
    必须有一个强正则命中、或例句精确命中、或至少两个独立关键词同时命中。
    """
    if score < 5:
        return False
    if any(h.startswith(("pattern:", "example-exact:")) for h in hits):
        return True
    return sum(1 for h in hits if h.startswith("keyword:")) >= 2


_REGEX_NOISE = re.compile(r"[\\^$.|?*+()\[\]{}]")


def pattern_core(pattern: str) -> str:
    """把正则里的转义和元字符去掉，留下可以判重的字面核心。"""
    return _REGEX_NOISE.sub("", pattern).strip().lower()


def pattern_is_strong(pattern: str) -> bool:
    """判断一个 intent pattern 是否足够具体。

    intents.json 里的 pattern 大多是从技能英文摘要自动拆出来的裸词（cursor/save/
    versions），单独命中毫无意义；只有短语、中文词、或长专有 id 才算强信号。
    """
    core = pattern_core(pattern)
    if not core:
        return False
    if re.fullmatch(r"[\u4e00-\u9fa5]+", core):
        return len(core) >= 2
    if re.fullmatch(r"[a-z0-9_]+", core):
        return len(core) >= 8
    return True


def entry_affinity(query: str, query_tokens: set[str], entry: dict) -> int:
    """条目自身对原话的证据分：用于同一 intent 内未显式排序的候选。

    intents.json 的 skills 顺序表达人工优先级；没有列进去、靠 manifest intents
    自动归属的条目，按这里的证据分排序，避免退化成 id 字典序。
    """
    score = 0
    entry_id = str(entry.get("id") or "").lower()
    if entry_id and entry_id in query:
        score += 8
    for example in entry.get("examples") or []:
        if normalize(str(example)) == query:
            score += 10
            continue
        score += 2 * len(query_tokens & tokens(str(example)))
    for keyword in entry.get("keywords") or []:
        if keyword_hit(keyword, query, query_tokens):
            score += 3
    evidence = " ".join([str(entry.get("summary") or ""), " ".join(entry.get("when") or [])])
    score += len(query_tokens & tokens(evidence))
    return score


SLOT_SUFFIX = re.compile(r"的?(?:结构式|结构图|结构|分子式|化学式|cdxml|cdx|图片)$", re.IGNORECASE)
SLOT_TAIL = re.compile(r"(?:画|绘制|生成|导出)?(?:出来|一下|来|成|好)$")
SLOT_MODAL = re.compile(r"(?:能|可以|要)$")
SLOT_PREFIX = re.compile(r"^(?:帮我|请|给我|把|将|一下|一幅|一张|一个|个|张|画|绘制|生成|导出|的)+")
# slot 是实体槽（化学名等），不是动作或产出物：剥掉动词/连接词的头，拒绝结构性名词的尾。
SLOT_CONNECT = re.compile(r"^(?:成|为|到|作|做|出|好|成个|一个|一张|一下|下)+")
SLOT_STRUCT_TAIL = re.compile(
    r"(?:图|管线|流程|方案|报告|文档|表格|步骤|架构|关系|系统|设计|模式|接口|协议|"
    r"字段|文件|目录|函数|模块|组件|服务|代码|仓库|项目)$"
)
SLOT_MAX_LEN = 24
SLOT_STOP = {
    "图",
    "图表",
    "趋势图",
    "柱状图",
    "折线图",
    "饼图",
    "散点图",
    "流程图",
    "示意图",
    "结构式",
    "结构",
    "分子式",
    "化学式",
    "图片",
    "这个分子",
    "该分子",
    "此分子",
    "这些分子",
    "这批分子",
    "这个",
    "该",
    "此",
    "路线",
    "合成路线",
}


def clean_slot_value(raw: str) -> str:
    """把“一下阿司匹林的结构式”这类话术清成实体“阿司匹林”。"""
    value = raw.strip().strip("，。；;、 ")
    value = SLOT_PREFIX.sub("", value)
    value = SLOT_SUFFIX.sub("", value).strip()
    value = SLOT_MODAL.sub("", value).strip()
    value = SLOT_TAIL.sub("", value).strip()
    value = SLOT_CONNECT.sub("", value).strip()
    return value.strip("的 ")


def extract_slot(pattern: str, query: str) -> str:
    """按 slot 正则取第一个像实体的分组；分组可能来自“把X画成…”“画一下X”等分支。"""
    for match in re.finditer(pattern, query, re.IGNORECASE):
        for group in match.groups():
            if not group:
                continue
            value = clean_slot_value(group)
            if len(value) < 2 or len(value) > SLOT_MAX_LEN:
                continue
            if value in SLOT_STOP or SLOT_STRUCT_TAIL.search(value):
                continue
            return value
    return ""


def agent_context(agent: str | None) -> tuple[dict, set[str]]:
    if not agent:
        return {}, set()
    path = CATALOG / "agents.json"
    if not path.is_file():
        return {}, set()
    config = json.loads(path.read_text(encoding="utf-8"))
    info = (config.get("agents") or {}).get(agent) or {}
    role = info.get("role")
    grants = set(((config.get("roles") or {}).get(role) or {}).get("grants") or [])
    return info, grants


def visible(entry: dict, agent: str | None, grants: set[str]) -> bool:
    if not agent:
        return True
    if agent in (entry.get("ignore") or []):
        return False
    tags = entry.get("agents") or ["*"]
    if "*" not in tags and agent not in tags:
        return False
    if grants and not set(entry.get("requires") or []).issubset(grants):
        return False
    return True


def entry_file(entry: dict, agent: str | None) -> str:
    if agent and (entry.get("variants") or {}).get(agent):
        return str(entry["variants"][agent])
    name = "SKILL.md" if entry.get("kind") == "skill" else "RULE.mdc"
    return f"{entry['path']}/{name}"


def fts_search(query: str, limit: int = 20) -> list[dict]:
    """FTS5 兜底检索（SQLite 只是 JSON 索引的加速投影）。"""
    db = CATALOG / store.FTS_NAME
    if not db.is_file():
        return []
    tokens = store.fts_tokens(query)
    if not tokens:
        return []
    match = " OR ".join(f'"{token}"' for token in tokens[:24])
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT id, bm25(entries_fts) FROM entries_fts "
                "WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [{"id": str(row[0]), "rank": float(row[1])} for row in rows]


def fallback_candidates(
    query: str,
    query_tokens: set[str],
    index: dict,
    agent: str | None,
    grants: set[str],
    limit: int = 0,
) -> list[dict]:
    """没有 intent 命中时的文本兜底：优先 FTS5，退化为词重叠。"""
    by_id = {entry["id"]: entry for entry in index.get("entries") or []}
    fts_rows = fts_search(query, limit=limit or 20)
    if fts_rows:
        out = []
        for row in fts_rows:
            entry = by_id.get(row["id"])
            if entry is None or entry.get("kind") == "rule":
                continue
            if not visible(entry, agent, grants):
                continue
            out.append(
                {
                    "id": entry["id"],
                    "kind": entry.get("kind"),
                    "category": entry.get("category"),
                    "path": entry_file(entry, agent),
                    "summary": entry.get("summary"),
                    "score": max(1, int(round(-row["rank"] * 10))),
                    "reason": "fallback:fts",
                }
            )
        if out:
            return out[:limit] if limit else out

    candidates = []
    df, n = token_frequency(index, agent)
    for entry in index.get("entries") or []:
        if not visible(entry, agent, grants):
            continue
        if entry.get("kind") == "rule":
            continue
        corpus = " ".join(
            [
                str(entry.get("id") or ""),
                str(entry.get("summary") or ""),
                " ".join(entry.get("keywords") or []),
                " ".join(entry.get("examples") or []),
                " ".join(entry.get("when") or []),
                str(entry.get("category") or ""),
            ]
        )
        corpus_tokens = tokens(corpus)
        overlap = query_tokens & corpus_tokens
        if not overlap:
            continue
        score = sum(token_weight(token, df, n) for token in overlap)
        if str(entry.get("id") or "").lower() in query:
            score += 10
        if score < 2:
            continue
        candidates.append(
            {
                "id": entry["id"],
                "kind": entry.get("kind"),
                "category": entry.get("category"),
                "path": entry_file(entry, agent),
                "summary": entry.get("summary"),
                "score": score,
                "reason": "fallback:token-overlap",
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["id"]))
    return candidates[:limit] if limit else candidates


def route(text: str, top: int = 5, agent: str | None = None) -> dict:
    index = ensure_index()
    _info, grants = agent_context(agent)
    intents = load_json(INTENTS).get("intents") or []
    entries = {entry["id"]: entry for entry in index.get("entries") or []}
    query = normalize(text)
    query_tokens = tokens(query)
    aliases = load_aliases()
    domains = load_domains()
    domain_report: list[dict] = []
    allowed_categories: set[str] | None = None
    if domains:
        scored_domains = []
        for domain in domains:
            hits = [kw for kw in domain.get("keywords") or [] if normalize(str(kw)) in query]
            if hits:
                scored_domains.append((len(hits), domain, hits))
        if scored_domains:
            scored_domains.sort(key=lambda item: (-item[0], str(item[1].get("id"))))
            domain_report = [
                {"id": item[1].get("id"), "score": item[0], "hits": item[2][:4]}
                for item in scored_domains[:4]
            ]
            allowed_categories = set()
            for _score, domain, _hits in scored_domains[:2]:
                allowed_categories.update(domain.get("categories") or [])

    def score_intents(only: set[str] | None) -> tuple[list, list[dict]]:
        scored = []
        dropped: list[dict] = []
        for intent in intents:
            outside = only is not None and str(intent.get("category") or "") not in only
            excluded = False
            exclude_pattern = ""
            for pattern in intent.get("excludes") or []:
                if re.search(pattern, query, re.IGNORECASE):
                    excluded = True
                    exclude_pattern = pattern
                    break
            if excluded:
                dropped.append(
                    {"intent": intent.get("id"), "score": 0, "reason": f"excluded:{exclude_pattern}"}
                )
                continue
            score = 0
            hits = []
            counted: set[str] = set()
            for pattern in intent.get("patterns") or []:
                if not re.search(pattern, query, re.IGNORECASE):
                    continue
                core = pattern_core(pattern)
                weight = 5 if pattern_is_strong(pattern) else 2
                score += weight
                counted.add(core)
                hits.append(f"{'pattern' if weight == 5 else 'weak-pattern'}:{pattern}")
            for keyword in intent.get("keywords") or []:
                kw = normalize(str(keyword))
                if kw in counted or not keyword_hit(keyword, query, query_tokens):
                    continue
                score += 2
                counted.add(kw)
                hits.append(f"keyword:{keyword}")
            for example in intent.get("examples") or []:
                if normalize(example) == query:
                    score += 10
                    hits.append(f"example-exact:{example}")
                    continue
                overlap = query_tokens & tokens(example)
                if overlap:
                    score += min(3, len(overlap))
                    hits.append(f"example:{example}")
            if score <= 0:
                continue
            if outside:
                # 域外不排除，只罚分：域门是排序先验，不是硬过滤，避免漏词/新域把真意图直接丢掉。
                score = max(0, score - 3)
                hits.append("domain-penalty")
                if score <= 0:
                    dropped.append({"intent": intent.get("id"), "score": 0, "reason": "domain-penalty"})
                    continue
            scored.append((score, intent, hits))
        return scored, dropped

    scored_intents, rejected = score_intents(allowed_categories)
    if allowed_categories is not None and not scored_intents:
        # 域门太紧会漏：退回全域再打一次分，并留下痕迹，方便发现域关键词写错。
        scored_intents, rejected = score_intents(None)
        domain_report.append({"id": "(gated-empty:fallback-all)", "score": 0, "hits": []})

    scored_intents.sort(key=lambda item: (-item[0], item[1].get("id") or ""))
    threshold = max(5, scored_intents[0][0] * 0.5) if scored_intents else 0
    above = [item for item in scored_intents if item[0] >= threshold][:3]
    # 置信门：撞上一个关键词就报意图，比不报更糟。宁可回退到文本兜底。
    selected_intents = [item for item in above if intent_is_confident(item[0], item[2])][:3]
    kept_ids = {str(item[1].get("id")) for item in selected_intents}
    for score, intent, hits in scored_intents:
        intent_id = str(intent.get("id"))
        if intent_id in kept_ids:
            continue
        if score < threshold:
            reason = "below-threshold"
        elif not intent_is_confident(score, hits):
            reason = "low-confidence"
        else:
            reason = "rank-cap"  # 过阈值也过置信门，只是只取前三
        rejected.append({"intent": intent_id, "score": score, "reason": reason})
    slots: dict[str, str] = {}
    for _score, intent, _hits in selected_intents[:1]:
        for name, pattern in (intent.get("slots") or {}).items():
            value = extract_slot(pattern, query)
            if value:
                slots.setdefault(name, value)
    intent_entries: dict[str, list[str]] = {}
    for entry in index.get("entries") or []:
        for intent_id in entry.get("intents") or []:
            intent_entries.setdefault(str(intent_id), []).append(entry["id"])
    ranked_intents: dict[str, list[str]] = {}
    candidates = []
    rules: list[dict] = []
    seen: set[str] = set()
    for score, intent, hits in selected_intents:
        intent_id = str(intent.get("id"))
        if intent_id not in ranked_intents:
            ranked_intents[intent_id] = sorted(
                intent_entries.get(intent_id) or [],
                key=lambda sid: (-entry_affinity(query, query_tokens, entries[sid]), sid)
                if sid in entries
                else (-1, sid),
            )
        skill_ids = list(intent.get("skills") or []) + ranked_intents[intent_id]
        for skill_id in skill_ids:
            if skill_id in seen or skill_id not in entries:
                continue
            entry = entries[skill_id]
            if not visible(entry, agent, grants):
                continue
            alias_of = None
            canonical = aliases.get(skill_id)
            if canonical and canonical in entries and visible(entries[canonical], agent, grants):
                # 近义 skill 只读规范条目，别让 agent 同时读两份
                alias_of, entry, skill_id = skill_id, entries[canonical], canonical
            if skill_id in seen:
                continue
            seen.add(skill_id)
            item = {
                "id": skill_id,
                "kind": entry.get("kind"),
                "category": entry.get("category"),
                "path": entry_file(entry, agent),
                "summary": entry.get("summary"),
                "score": score,
                "reason": f"intent={intent.get('id')}",
                "hits": list(hits),
                "why": f"{intent.get('id')}: " + " + ".join(hits[:4]),
            }
            if alias_of:
                item["alias_of"] = alias_of
            # 规则不跟 skill 抢位置：候选里 skill 在前、rule 在后，另外单独给一份 rules 视图。
            item["kind"] = entry.get("kind")
            (rules if entry.get("kind") == "rule" else candidates).append(item)
    candidates.sort(key=lambda item: 0 if item.get("kind") != "rule" else 1)
    if not candidates and not selected_intents:
        candidates = fallback_candidates(query, query_tokens, index, agent, grants, limit=top)
    candidates = candidates + [r for r in rules if r["id"] not in {c["id"] for c in candidates}]
    if aliases:
        # 兜底路径没有 intent 分支，这里统一再折一次：近义条目只留规范条目。
        folded: list[dict] = []
        ids: set[str] = set()
        for item in candidates:
            canonical = aliases.get(str(item.get("id")))
            if canonical and canonical in entries:
                item = dict(item)
                item["alias_of"] = item.get("alias_of") or item["id"]
                item["id"] = canonical
                item["path"] = entry_file(entries[canonical], agent)
            if item["id"] in ids:
                continue
            ids.add(item["id"])
            folded.append(item)
        candidates = folded
    candidates = candidates[:top]
    rules = [item for item in candidates if item.get("kind") == "rule"][:top]
    top_intent = selected_intents[0][1] if selected_intents else {}
    return {
        "query": text,
        "intent": top_intent.get("id"),
        "intentCategory": top_intent.get("category"),
        "intentCandidates": [item[1].get("id") for item in selected_intents],
        "domains": domain_report,
        "threshold": threshold,
        "slots": slots,
        "candidates": candidates,
        "rules": rules,
        "rejected": rejected[:10],
        "fallback": bool(candidates and candidates[0].get("reason", "").startswith("fallback")),
    }


def print_human(result: dict) -> None:
    print(f"query: {result['query']}")
    print(f"intent: {result['intent'] or '(none)'}")
    if result.get("intentCategory"):
        print(f"intent category: {result['intentCategory']}")
    if result.get("intentCandidates"):
        print(f"intent candidates: {', '.join(result['intentCandidates'])}")
    if result.get("slots"):
        print("slots: " + ", ".join(f"{key}={value}" for key, value in result["slots"].items()))
    if not result["candidates"]:
        print("no candidates; ask the user or add an intent")
        return
    for index, item in enumerate(result["candidates"], 1):
        print(f"{index}. {item['id']}  [{item['kind']}/{item['category']}]  score={item['score']}  {item['reason']}")
        print(f"   {item['path']}")
        if item.get("why"):
            print(f"   why: {item['why']}")
        if item.get("summary"):
            print(f"   {item['summary'][:160]}")
    if result.get("rules"):
        print("rules (不占候选): " + ", ".join(item["id"] for item in result["rules"]))
    if result.get("rejected"):
        shown = ", ".join(f"{item['intent']}={item['reason']}" for item in result["rejected"][:5])
        print(f"rejected: {shown}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-library 意图路由")
    parser.add_argument("command", choices=["route", "search", "load"], help="route=意图路由；search=兜底文本检索；load=打印路径")
    parser.add_argument("query", help="用户原话或 skill id")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--top", type=int, default=5, help="候选数量")
    parser.add_argument("--agent", default=None, help="按访问员过滤（agent id）")
    parser.add_argument("--no-log", action="store_true", help="不写 state/usage.jsonl")
    args = parser.parse_args(argv)
    if args.command == "load":
        index = ensure_index()
        entries = {entry["id"]: entry for entry in index.get("entries") or []}
        entry = entries.get(args.query)
        if not entry:
            alias = load_aliases().get(args.query)
            entry = entries.get(alias) if alias else None
        if not entry:
            print(f"找不到 skill: {args.query}", file=sys.stderr)
            return 1
        print(entry_file(entry, args.agent))
        return 0
    result = route(args.query, args.top, args.agent)
    result["agent"] = args.agent
    if not args.no_log:
        # 记录发生在入口层，route() 本身仍然只读。
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import usage  # noqa: WPS433

            usage.log(
                {
                    "src": "cli",
                    "event": "route",
                    "query": args.query,
                    "agent": args.agent,
                    "intent": result.get("intent"),
                    "top": (result["candidates"][0]["id"] if result.get("candidates") else None),
                }
            )
        except Exception:
            pass
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


