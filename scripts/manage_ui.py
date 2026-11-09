# 本机窗口：查看、修改、删除共享库里的技能和规则。只监听 127.0.0.1。
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aglib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router  # noqa: E402

STORE = aglib.STORE
ENTRY = aglib.ENTRY
CATALOG = STORE / "catalog"
TRASH = STORE / "_trash"
PAGE = Path(__file__).with_name("manage_ui.html")
PORT = 8765
HOST = "127.0.0.1"
EDGE = Path(
    os.environ.get(
        "AGLIB_EDGE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
)
TOKEN = os.environ.get("AGLIB_UI_TOKEN") or secrets.token_urlsafe(24)
SECRET = aglib.SECRET
LOCK = threading.Lock()


def fail(message: str) -> None:
    raise ValueError(message)


def alert(message: str) -> None:
    ctypes_box = __import__("ctypes")
    ctypes_box.windll.user32.MessageBoxW(0, message, "技能库", 0x10)


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        fail(f"需要对象：{path}")
    return data


def reject_secrets(text: str) -> None:
    if SECRET.search(text):
        fail("内容里有密钥样式，没有保存")


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :]
    return text


def excerpt(text: str) -> str:
    body = strip_frontmatter(text)
    parts: list[str] = []
    for line in body.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or raw.startswith("```") or raw.startswith("|"):
            if parts:
                break
            continue
        s = raw.lstrip("> ").strip()
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        s = s.replace("**", "").replace("`", "")
        if not s:
            continue
        parts.append(s)
        if len(parts) >= 2 or len(" ".join(parts)) > 180:
            break
    return " ".join(parts)[:240]


def body_file(folder: Path, kind: str) -> Path:
    return folder / ("RULE.mdc" if kind == "rule" else "SKILL.md")


def read_head(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def catalog_dir(rel: str) -> Path:
    rel = rel.replace("\\", "/").strip("/")
    if not rel or any(part in {"", ".", ".."} for part in rel.split("/")):
        fail("路径不对")
    path = (CATALOG / rel).resolve()
    root = CATALOG.resolve()
    if path != root and root not in path.parents:
        fail("路径超出技能库")
    if not (path / "manifest.json").is_file():
        fail("找不到这条")
    return path


def iter_items():
    if not CATALOG.is_dir():
        return
    for manifest_path in sorted(CATALOG.rglob("manifest.json")):
        item = load_json(manifest_path)
        if item.get("kind") not in {"skill", "rule"}:
            continue
        if "id" not in item:
            continue
        folder = manifest_path.parent
        rel = folder.relative_to(CATALOG).as_posix()
        yield item, folder, rel


def list_items() -> list[dict]:
    rows = []
    for item, folder, rel in iter_items():
        when = [str(x).strip() for x in (item.get("when") or []) if str(x).strip()]
        purpose = " ".join(when)
        head = read_head(body_file(folder, item["kind"]))
        rows.append(
            {
                "rel": rel,
                "id": item["id"],
                "kind": item["kind"],
                "mode": item.get("mode") or "on-demand",
                "purpose": purpose,
                "excerpt": excerpt(head),
                "requires": list(item.get("requires") or []),
            }
        )
    rows.sort(key=lambda row: (0 if row["kind"] == "rule" else 1, row["id"].lower()))
    return rows


def agents_config() -> dict:
    path = CATALOG / "agents.json"
    if not path.is_file():
        return {}
    data = load_json(path)
    return data.get("agents") or {}


def agent_ids() -> list[str]:
    return sorted(agents_config())


def intents_path() -> Path:
    return CATALOG / "intents.json"


def load_intents() -> dict:
    path = intents_path()
    if not path.is_file():
        return {"schemaVersion": 1, "categories": {}, "intents": []}
    return load_json(path)


def save_intents(data: dict) -> str:
    reject_secrets(json.dumps(data, ensure_ascii=False))
    data.setdefault("schemaVersion", 1)
    data.setdefault("categories", {})
    data.setdefault("intents", [])
    intents_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return refresh_index()


def item_ids() -> set[str]:
    return {str(item["id"]) for item, _folder, _rel in iter_items()}


def item_detail(rel: str) -> dict:
    folder = catalog_dir(rel)
    item = load_json(folder / "manifest.json")
    if item.get("kind") not in {"skill", "rule"}:
        fail("这条不是技能或规则")
    path = body_file(folder, item["kind"])
    when = [str(x).strip() for x in (item.get("when") or []) if str(x).strip()]
    when_not = [str(x).strip() for x in (item.get("whenNot") or []) if str(x).strip()]
    overlays = {}
    for agent in agent_ids():
        overlays[agent] = {
            "variant": (folder / f"SKILL.{agent}.md").is_file(),
            "ignore": (folder / f"SKILL.{agent}.ignore").is_file(),
        }
    return {
        "rel": rel,
        "id": item["id"],
        "kind": item["kind"],
        "mode": item.get("mode") or "on-demand",
        "category": item.get("category") or "",
        "tags": "\n".join(str(x) for x in (item.get("tags") or [])),
        "intents": ", ".join(str(x) for x in (item.get("intents") or [])),
        "keywords": "\n".join(str(x) for x in (item.get("keywords") or [])),
        "examples": "\n".join(str(x) for x in (item.get("examples") or [])),
        "agents": list(item.get("agents") or ["*"]),
        "when": "\n".join(when),
        "whenNot": "\n".join(when_not),
        "requires": list(item.get("requires") or []),
        "overlays": overlays,
        "bodyName": path.name,
        "body": path.read_text(encoding="utf-8") if path.is_file() else "",
        "excerpt": excerpt(path.read_text(encoding="utf-8") if path.is_file() else ""),
    }


def lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def patch_rule_frontmatter(text: str, purpose: str, resident: bool) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    front = text[4:end]
    rest = text[end + 4 :]
    lines_fm = front.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines_fm):
        line = lines_fm[i]
        if line.startswith("description:"):
            rest_desc = line.split(":", 1)[1].strip()
            i += 1
            if rest_desc in {">", ">-", "|", "|-"}:
                while i < len(lines_fm) and lines_fm[i].startswith((" ", "\t")):
                    i += 1
            continue
        if line.startswith("alwaysApply:"):
            i += 1
            continue
        kept.append(line)
        i += 1
    summary = " ".join(purpose.split())
    description = "description: " + json.dumps(summary, ensure_ascii=False)
    always = "alwaysApply: " + ("true" if resident else "false")
    rebuilt = "\n".join([description, always, *kept]).strip("\n")
    return f"---\n{rebuilt}\n---{rest if rest.startswith('\n') else '\n' + rest}"


def save_item(payload: dict) -> str:
    rel = str(payload.get("rel") or "")
    folder = catalog_dir(rel)
    item = load_json(folder / "manifest.json")
    if item.get("kind") not in {"skill", "rule"}:
        fail("这条不是技能或规则")
    mode = payload.get("mode")
    if mode not in {"on-demand", "resident"}:
        fail("调用方式只能是按需或常驻")
    when = lines(payload.get("when") or "")
    when_not = lines(payload.get("whenNot") or "")
    if not when:
        fail("用途不能空")
    category = str(payload.get("category") or "").strip()
    if category and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", category):
        fail("分类只能是小写 kebab-case")
    intent_ids = lines(payload.get("intents") or "")
    known_intents = {str(x.get("id")) for x in (load_intents().get("intents") or [])}
    unknown = [x for x in intent_ids if x not in known_intents]
    if unknown:
        fail("未知 intent: " + ", ".join(unknown))
    tags = lines(payload.get("tags") or "")
    keywords = lines(payload.get("keywords") or "")
    examples = lines(payload.get("examples") or "")
    requires = lines(payload.get("requires") or "")
    allowed_requires = {"filesystem-read", "filesystem-write", "shell", "network", "secrets"}
    if not set(requires).issubset(allowed_requires):
        fail("requires 只能写 " + ", ".join(sorted(allowed_requires)))
    agents = lines(payload.get("agents") or "") or ["*"]
    if "*" not in agents and not set(agents).issubset(set(agent_ids())):
        fail("agents 只能是 * 或 agents.json 里的 agent")
    body = str(payload.get("body") if payload.get("body") is not None else "")
    if item["kind"] == "rule":
        body = patch_rule_frontmatter(body, " ".join(when), mode == "resident")
    blob = "\n".join(when + when_not + tags + keywords + examples) + "\n" + body
    reject_secrets(blob)
    item["when"] = when
    item["whenNot"] = when_not
    item["mode"] = mode
    if category:
        item["category"] = category
    else:
        item.pop("category", None)
    if tags:
        item["tags"] = tags
    else:
        item.pop("tags", None)
    if intent_ids:
        item["intents"] = intent_ids
    else:
        item.pop("intents", None)
    if keywords:
        item["keywords"] = keywords
    else:
        item.pop("keywords", None)
    if examples:
        item["examples"] = examples
    else:
        item.pop("examples", None)
    item["requires"] = requires
    item["agents"] = agents
    path = body_file(folder, item["kind"])
    path.write_text(body if body.endswith("\n") or body == "" else body + "\n", encoding="utf-8")
    (folder / "manifest.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return refresh_index()


def delete_item(rel: str) -> str:
    folder = catalog_dir(rel)
    item = load_json(folder / "manifest.json")
    if item.get("kind") not in {"skill", "rule"}:
        fail("这条不是技能或规则")
    item_id = str(item.get("id") or "")
    if not item_id or any(part in item_id for part in ("/", "\\", "..")):
        fail("id 不能当路径用")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = TRASH / stamp / folder.relative_to(CATALOG)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    return refresh_index()


def overlay_get(rel: str, agent: str) -> dict:
    if agent not in agent_ids():
        fail("未知 agent")
    folder = catalog_dir(rel)
    variant = folder / f"SKILL.{agent}.md"
    ignored = folder / f"SKILL.{agent}.ignore"
    return {
        "rel": rel,
        "agent": agent,
        "variant": variant.is_file(),
        "ignore": ignored.is_file(),
        "body": variant.read_text(encoding="utf-8") if variant.is_file() else "",
    }


def save_overlay(payload: dict) -> str:
    rel = str(payload.get("rel") or "")
    agent = str(payload.get("agent") or "")
    if agent not in agent_ids():
        fail("未知 agent")
    folder = catalog_dir(rel)
    body = str(payload.get("body") or "")
    ignore = bool(payload.get("ignore"))
    reject_secrets(body)
    variant = folder / f"SKILL.{agent}.md"
    ignored = folder / f"SKILL.{agent}.ignore"
    if body.strip():
        variant.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    elif variant.exists():
        variant.unlink()
    if ignore:
        ignored.write_text("", encoding="utf-8")
    elif ignored.exists():
        ignored.unlink()
    return refresh_index()


def intents_data() -> dict:
    return load_intents()


def save_intent(payload: dict) -> str:
    original = str(payload.get("original_id") or "").strip()
    intent_id = str(payload.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", intent_id):
        fail("intent id 必须是小写 kebab-case")
    data = load_intents()
    items = data.get("intents") or []
    if original and original != intent_id and any(str(x.get("id")) == intent_id for x in items):
        fail("intent id 已存在")
    if not original and any(str(x.get("id")) == intent_id for x in items):
        fail("intent id 已存在")
    category = str(payload.get("category") or "meta").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", category):
        fail("分类只能是小写 kebab-case")
    patterns = lines(payload.get("patterns") or "")
    keywords = lines(payload.get("keywords") or "")
    excludes = lines(payload.get("excludes") or "")
    examples = lines(payload.get("examples") or "")
    skills = lines(payload.get("skills") or "")
    if not patterns and not keywords:
        fail("patterns/keywords 至少写一个")
    known = item_ids()
    unknown = [x for x in skills if x not in known]
    if unknown:
        fail("未知 skill: " + ", ".join(unknown))
    intent = {
        "id": intent_id,
        "category": category,
        "description": str(payload.get("description") or "").strip(),
        "patterns": patterns,
        "keywords": keywords,
        "excludes": excludes,
        "examples": examples,
        "skills": skills,
    }
    reject_secrets(json.dumps(intent, ensure_ascii=False))
    replaced = False
    for index, old in enumerate(items):
        if str(old.get("id")) == (original or intent_id):
            items[index] = intent
            replaced = True
            break
    if not replaced:
        items.append(intent)
    data["intents"] = items
    categories = data.setdefault("categories", {})
    for key, values in list(categories.items()):
        if isinstance(values, list) and original and original in values and original != intent_id:
            categories[key] = [intent_id if x == original else x for x in values]
    if category not in categories:
        categories[category] = []
    if intent_id not in categories[category]:
        categories[category].append(intent_id)
    return save_intents(data)


def delete_intent(intent_id: str) -> str:
    data = load_intents()
    items = [x for x in (data.get("intents") or []) if str(x.get("id")) != intent_id]
    if len(items) == len(data.get("intents") or []):
        fail("找不到 intent")
    data["intents"] = items
    for key, values in list((data.get("categories") or {}).items()):
        if isinstance(values, list):
            data["categories"][key] = [x for x in values if x != intent_id]
    return save_intents(data)


def configure(store: Path, entry: Path) -> None:
    global STORE, ENTRY, CATALOG, TRASH
    STORE = store.expanduser().resolve()
    ENTRY = entry.expanduser()
    CATALOG = STORE / "catalog"
    TRASH = STORE / "_trash"


def refresh_index() -> str:
    with LOCK:
        proc = subprocess.run(
            [sys.executable, str(STORE / "scripts" / "store.py"), "--store", str(STORE)],
            cwd=str(STORE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        fail(output or "索引刷新失败")
    return output


def server_is_ours() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=0.6) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("name") == "agent-library"
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return False


def open_window() -> None:
    url = f"http://{HOST}:{PORT}/"
    if EDGE.is_file():
        subprocess.Popen(
            [str(EDGE), f"--app={url}", "--window-size=1180,780"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    webbrowser.open(url)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data: dict, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        if not secrets.compare_digest(self.headers.get("X-AGLIB-Token") or "", TOKEN):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}:
            return False
        return True

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "未授权"}, 403)
        return False

    def read_json(self) -> dict:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            fail("只接受 application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            fail("正文太大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            fail("需要对象")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "name": "agent-library"})
                return
            if path in {"/api/items", "/api/item", "/api/route", "/api/intents", "/api/overlay", "/api/agents"} and not self.require_auth():
                return
            if path == "/api/agents":
                self.send_json({"agents": agent_ids()})
                return
            if path == "/api/intents":
                self.send_json(intents_data())
                return
            if path == "/api/overlay":
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                rel = agent = ""
                for part in query.split("&"):
                    if part.startswith("rel="):
                        rel = urllib.request.unquote(part[4:])
                    if part.startswith("agent="):
                        agent = urllib.request.unquote(part[6:])
                self.send_json(overlay_get(rel, agent))
                return
            if path == "/api/route":
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                q = ""
                agent = ""
                for part in query.split("&"):
                    if part.startswith("q="):
                        q = urllib.request.unquote(part[2:])
                    if part.startswith("agent="):
                        agent = urllib.request.unquote(part[6:])
                if not q.strip():
                    raise ValueError("缺少 q")
                self.send_json(router.route(q, agent=agent or None))
                return
            if path == "/api/items":
                self.send_json({"items": list_items()})
                return
            if path == "/api/item":
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                rel = ""
                for part in query.split("&"):
                    if part.startswith("rel="):
                        rel = urllib.request.unquote(part[4:])
                self.send_json(item_detail(rel))
                return
            if path in {"/", "/index.html"}:
                raw = PAGE.read_text(encoding="utf-8").replace("__AGLIB_TOKEN__", TOKEN).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.security_headers()
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.security_headers()
                self.end_headers()
                return
            self.send_json({"error": "没有这个地址"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except OSError as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if not self.require_auth():
                return
            payload = self.read_json()
            if path == "/api/save":
                note = save_item(payload)
                self.send_json({"ok": True, "project": note})
                return
            if path == "/api/delete":
                note = delete_item(str(payload.get("rel") or ""))
                self.send_json({"ok": True, "project": note})
                return
            if path == "/api/overlay/save":
                note = save_overlay(payload)
                self.send_json({"ok": True, "project": note})
                return
            if path == "/api/intent/save":
                note = save_intent(payload)
                self.send_json({"ok": True, "project": note})
                return
            if path == "/api/intent/delete":
                note = delete_intent(str(payload.get("id") or ""))
                self.send_json({"ok": True, "project": note})
                return
            self.send_json({"error": "没有这个地址"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 500)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本机技能库窗口")
    parser.add_argument("--store", default=str(aglib.STORE), help="库根目录")
    parser.add_argument("--entry", default=str(aglib.ENTRY), help="入口目录")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--no-open", action="store_true", help="只启动服务，不打开浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    global PORT
    args = parse_args(argv)
    PORT = args.port
    configure(Path(args.store), Path(args.entry))
    if server_is_ours():
        if not args.no_open:
            open_window()
        return
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        if args.no_open:
            print(f"端口 {PORT} 被占用：{exc}", file=sys.stderr)
            raise SystemExit(1)
        alert(f"端口 {PORT} 被占用，窗口没打开。\n{exc}")
        return
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    for _ in range(40):
        if server_is_ours():
            break
        time.sleep(0.05)
    if args.no_open:
        print(f"listening http://{HOST}:{PORT}/")
    else:
        open_window()
    worker.join()


if __name__ == "__main__":
    main()
