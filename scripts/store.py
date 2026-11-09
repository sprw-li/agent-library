# 生成 catalog/index.json：唯一真源的存储索引，不复制正文/资源。
# 新 skill 只要放进 catalog/<id>/SKILL.md，这里会自动补 manifest、分类、关键词和 intent。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import aglib

CATEGORY_PREFIXES = (
    ("document-", "documents"),
    ("doc-", "documents"),
    ("research-", "research"),
    ("statistical-", "data"),
    ("skill-", "meta"),
    ("agent-", "meta"),
)
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "use", "when", "user", "skill",
    "使用", "用户", "一个", "这个", "可以", "需要", "进行", "以及", "或者", "如果", "不要",
}


def category_of(item: dict) -> str:
    if item.get("category"):
        return str(item["category"])
    item_id = str(item.get("id") or "")
    for prefix, category in CATEGORY_PREFIXES:
        if item_id.startswith(prefix):
            return category
    return "general"


def frontmatter(text: str) -> dict[str, str]:
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    data: dict[str, str] = {}
    lines = text[3:end].splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in {">", ">-", "|", "|-"}:
            folded: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                if lines[index].strip():
                    folded.append(lines[index].strip())
                index += 1
            data[key] = " ".join(folded)
            continue
        data[key] = value.strip("\"'")
        index += 1
    return data


def ensure_manifests(catalog: Path) -> list[str]:
    created: list[str] = []
    if not catalog.is_dir():
        return created
    for folder in sorted(p for p in catalog.iterdir() if p.is_dir()):
        if folder.name in aglib.SKIP_DIRS:
            continue
        manifest = folder / "manifest.json"
        if manifest.exists():
            continue
        skill, rule = folder / "SKILL.md", folder / "RULE.mdc"
        if skill.is_file():
            kind, body = "skill", skill
        elif rule.is_file():
            kind, body = "rule", rule
        else:
            continue
        if not KEBAB.fullmatch(folder.name):
            continue
        meta = frontmatter(body.read_text(encoding="utf-8", errors="replace"))
        description = str(meta.get("description") or "").strip() or folder.name
        mode = "resident" if kind == "rule" and str(meta.get("alwaysApply") or "").lower() == "true" else "on-demand"
        data = {
            "schemaVersion": 1,
            "id": folder.name,
            "kind": kind,
            "mode": mode,
            "when": [description],
            "whenNot": ["任务与上面描述不匹配时"],
            "requires": ["filesystem-read"],
            "merge": "shared",
            "notes": {"autoManifest": True},
        }
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        created.append(folder.name)
    return created


def tokens(text: str) -> set[str]:
    text = re.sub(r"\s+", " ", text.strip().lower())
    found: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fa5]+", text):
        if re.fullmatch(r"[\u4e00-\u9fa5]+", word):
            found.add(word)
            found.update(word[i : i + 2] for i in range(max(0, len(word) - 1)))
        elif len(word) > 1 and word not in STOPWORDS:
            found.add(word)
    return found


def load_intents(store: Path) -> dict:
    path = store / "catalog" / "intents.json"
    if not path.is_file():
        return {"intents": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        aglib.fail("catalog/intents.json 必须是对象")
    return data


def auto_intents(text: str, seeds: list[tuple[str, set[str]]]) -> list[str]:
    text_tokens = tokens(text)
    scored = []
    for intent_id, seed_tokens in seeds:
        overlap = text_tokens & seed_tokens
        if len(overlap) >= 2:
            scored.append((len(overlap), intent_id))
    scored.sort(reverse=True)
    return [name for _score, name in scored[:2]]


def auto_keywords(text: str, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for token in tokens(text):
        if token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda key: (-counts[key], -len(key), key))
    return ranked[:limit]


KNOWN_FILES = {"manifest.json", "SKILL.md", "RULE.mdc"}


def scan_catalog(catalog: Path) -> list[tuple[Path, str, dict[str, os.stat_result]]]:
    """一次 os.walk 取出所有条目目录和索引相关文件的 stat。

    manifest.json 决定目录是不是条目；正文/变体/ignore 都在条目目录一层，
    所以不需要 stat 整棵 assets/vendor 树。签名和 stamp 共用这一份扫描结果，
    避免 build_index 里三次遍历同一个 catalog。
    """
    scanned: list[tuple[Path, str, dict[str, os.stat_result]]] = []
    for current, dirs, files in os.walk(catalog):
        dirs[:] = sorted(d for d in dirs if d not in aglib.SKIP_DIRS)
        if "manifest.json" not in files:
            continue
        folder = Path(current)
        rel = folder.relative_to(catalog).as_posix()
        matched: dict[str, os.stat_result] = {}
        for name in sorted(files):
            if name not in KNOWN_FILES and not name.startswith("SKILL."):
                continue
            matched[name] = (folder / name).stat()
        scanned.append((folder, rel, matched))
    return scanned


def signature_of(scanned: list[tuple[Path, str, dict[str, os.stat_result]]]) -> str:
    digest = hashlib.sha256()
    for _folder, rel, matched in scanned:
        for name, stat in matched.items():
            digest.update(f"{rel}/{name}:{stat.st_size}:{stat.st_mtime_ns};".encode("utf-8"))
    return digest.hexdigest()


def catalog_signature(catalog: Path) -> str:
    """条目正文/变体/ignore 的变化指纹；不含 assets，不 stat 整棵子树。"""
    return signature_of(scan_catalog(catalog))


def intents_signature(store: Path) -> str:
    path = store / "catalog" / "intents.json"
    if not path.is_file():
        return ""
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def stamp_from(matched: dict[str, os.stat_result]) -> str:
    digest = hashlib.sha256()
    # 和旧版 sorted(folder.iterdir()) 保持一致：Windows 的 Path 排序是大小写不敏感。
    for name in sorted(matched, key=str.lower):
        stat = matched[name]
        digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns};".encode("utf-8"))
    return digest.hexdigest()


def stamp_of(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        if path.name not in {"manifest.json", "SKILL.md", "RULE.mdc"} and not path.name.startswith("SKILL."):
            continue
        stat = path.stat()
        digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns};".encode("utf-8"))
    return digest.hexdigest()


def build_index(store: Path, previous: dict | None = None) -> dict:
    catalog = store / "catalog"
    ensure_manifests(catalog)
    intents_data = load_intents(store)
    intents = intents_data.get("intents") or []
    intent_by_skill: dict[str, list[str]] = {}
    for intent in intents:
        for skill_id in intent.get("skills") or []:
            intent_by_skill.setdefault(str(skill_id), []).append(str(intent.get("id")))
    intent_seeds = [
        (
            str(intent.get("id")),
            tokens(" ".join([str(intent.get("description") or "")] + [str(x) for x in intent.get("keywords") or []])),
        )
        for intent in intents
    ]
    current_intents = intents_signature(store)
    previous = previous or {}
    reuse = bool(
        previous.get("intentsSignature") == current_intents
        and previous.get("catalogSignature")
    )
    prev_entries = {entry["path"]: entry for entry in previous.get("entries") or []}
    prev_stamps = previous.get("stamps") or {}

    entries = []
    stamps: dict[str, str] = {}
    scanned = scan_catalog(catalog)
    for folder, rel, matched in scanned:
        entry_rel = f"catalog/{rel}"
        stamp = stamp_from(matched)
        stamps[entry_rel] = stamp
        if reuse and prev_stamps.get(entry_rel) == stamp and entry_rel in prev_entries:
            entries.append(prev_entries[entry_rel])
            continue
        try:
            item = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("kind") not in {"skill", "rule"}:
            continue
        skill_id = item["id"]
        body = folder / ("SKILL.md" if item.get("kind") == "skill" else "RULE.mdc")
        variants: dict[str, str] = {}
        ignored: list[str] = []
        for name in matched:
            if name == "SKILL.md":
                continue
            if name.startswith("SKILL.") and name.endswith(".md"):
                variants[name[6:-3]] = f"{entry_rel}/{name}"
            if name.startswith("SKILL.") and name.endswith(".ignore"):
                ignored.append(name[6:-7])
        description = " ".join(item.get("when") or []).strip()
        meta = {}
        if not description:
            meta = frontmatter(body.read_text(encoding="utf-8", errors="replace"))
            description = str(meta.get("description") or "").strip()
        assigned = list(item.get("intents") or intent_by_skill.get(skill_id) or [])
        if not assigned:
            assigned = auto_intents(" ".join([skill_id, description, " ".join(item.get("keywords") or [])]), intent_seeds)
        keywords = list(item.get("keywords") or [])
        if not keywords:
            keywords = auto_keywords(" ".join([skill_id, description]))
        entries.append(
            {
                "id": skill_id,
                "kind": item.get("kind"),
                "category": category_of(item),
                "tags": list(item.get("tags") or []),
                "agents": list(item.get("agents") or ["*"]),
                "ignore": sorted(ignored),
                "variants": variants,
                "intents": assigned,
                "keywords": keywords,
                "examples": list(item.get("examples") or []),
                "summary": description[:220],
                "when": list(item.get("when") or []),
                "whenNot": list(item.get("whenNot") or []),
                "requires": list(item.get("requires") or []),
                "path": entry_rel,
                "mtime": int(matched[body.name].st_mtime) if body.name in matched else int(body.stat().st_mtime),
            }
        )
    entries.sort(key=lambda entry: entry["id"])
    categories: dict[str, int] = {}
    for entry in entries:
        categories[entry["category"]] = categories.get(entry["category"], 0) + 1
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "catalogSignature": signature_of(scanned),
        "intentsSignature": current_intents,
        "stamps": stamps,
        "entryCount": len(entries),
        "categories": dict(sorted(categories.items())),
        "entries": entries,
    }


FTS_NAME = "index.fts.db"
_CJK = re.compile(r"[\u4e00-\u9fa5]+")


def fts_tokens(text: str) -> list[str]:
    """FTS5 的 unicode61 不切中文，这里先切好：中文按字符 bigram，其他按词。"""
    out: list[str] = []
    for word in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fa5]+", str(text).lower()):
        if _CJK.fullmatch(word):
            if len(word) == 1:
                out.append(word)
            else:
                out.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            out.append(word)
    return out


def entry_corpus(entry: dict) -> str:
    return " ".join(
        [
            str(entry.get("id") or ""),
            str(entry.get("summary") or ""),
            " ".join(entry.get("keywords") or []),
            " ".join(entry.get("when") or []),
            " ".join(entry.get("examples") or []),
            str(entry.get("category") or ""),
            str(entry.get("kind") or ""),
        ]
    )


def write_fts(store: Path, index: dict) -> Path:
    """把索引投影成 SQLite FTS5，用于兜底检索；JSON 仍是唯一真源。"""
    import sqlite3

    path = store / "catalog" / FTS_NAME
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE IF EXISTS entries_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE entries_fts USING fts5("
            "tokens, id UNINDEXED, kind UNINDEXED, category UNINDEXED, path UNINDEXED)"
        )
        rows = [
            (
                " ".join(fts_tokens(entry_corpus(entry))),
                str(entry.get("id")),
                str(entry.get("kind") or ""),
                str(entry.get("category") or ""),
                str(entry.get("path") or ""),
            )
            for entry in index.get("entries") or []
            if entry.get("kind") != "rule"
        ]
        conn.executemany(
            "INSERT INTO entries_fts(tokens, id, kind, category, path) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path

def write_index(store: Path, index: dict) -> Path:
    path = store / "catalog" / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    try:
        write_fts(store, index)
    except Exception as exc:  # FTS 是加速层，坏了不能挡住索引
        print(f"warning: fts 写入失败：{exc}", file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 catalog/index.json")
    parser.add_argument("--store", default=str(aglib.STORE), help="库根目录")
    parser.add_argument("--check", action="store_true", help="校验 intents 引用的 skill 是否存在")
    args = parser.parse_args(argv)
    store = Path(args.store).expanduser().resolve()
    index = build_index(store)
    write_index(store, index)
    print(f"entries={index['entryCount']} categories={index['categories']}")
    if args.check:
        known = {entry["id"] for entry in index["entries"]}
        missing = [
            skill_id
            for intent in load_intents(store).get("intents") or []
            for skill_id in intent.get("skills") or []
            if skill_id not in known
        ]
        if missing:
            print(f"missing skills: {sorted(set(missing))}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



