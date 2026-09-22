#!/usr/bin/env python3
"""Local HTTP bridge from Codex-clickable 408 links to Obsidian notes."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from question_visual_links_408 import BRIDGE_PORT, FORMAL


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_obsidian_bridge_snapshot import (  # noqa: E402
    DEFAULT_SNAPSHOT,
    DEFAULT_VAULT,
    PROJECT_ROOT,
    build_snapshot,
    obsidian_url,
    write_snapshot,
)


OBSIDIAN_CLI_CANDIDATES = [
    "/opt/homebrew/bin/obsidian",
    "/usr/local/bin/obsidian",
]


def normalize(value: str) -> str:
    return value.strip().lower()


class BridgeState:
    def __init__(self, repo: Path, snapshot_path: Path, vault: str):
        self.repo = repo
        self.snapshot_path = snapshot_path
        self.vault = vault
        self.snapshot_signature: tuple[int, int] | None = None
        self.refresh(write_if_missing=False)

    def file_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.snapshot_path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def install_snapshot(self, data: dict) -> None:
        self.data = data
        self.records = data.get("records", [])
        self.by_key: dict[str, dict] = {}
        for record in self.records:
            keys = [
                record.get("node_id"),
                record.get("formal_id"),
                record.get("wrongnet_id"),
                record.get("visual_id"),
                record.get("source_id"),
                record.get("title"),
                record.get("target_note"),
                record.get("detail_note"),
                record.get("safe_note"),
            ]
            for key in keys:
                if key:
                    self.by_key[normalize(str(key))] = record

    def refresh(self, write_if_missing: bool = True, force_rebuild: bool = False) -> None:
        if self.snapshot_path.exists() and not force_rebuild:
            data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        else:
            data = build_snapshot(self.repo, self.vault)
            if write_if_missing:
                write_snapshot(self.snapshot_path, data)
        self.snapshot_signature = self.file_signature()
        self.install_snapshot(data)

    def reload_if_changed(self) -> None:
        signature = self.file_signature()
        if signature and signature != self.snapshot_signature:
            self.refresh(write_if_missing=False)

    def rebuild_and_find(self, target: str) -> dict | None:
        self.refresh(write_if_missing=True, force_rebuild=True)
        return self.find_record(target, allow_rebuild=False)

    def find_record(self, target: str, allow_rebuild: bool = True) -> dict | None:
        self.reload_if_changed()
        key = normalize(unquote(target))
        if key in self.by_key:
            return self.by_key[key]
        if FORMAL.fullmatch(unquote(target).upper()):
            return self.rebuild_and_find(target) if allow_rebuild else None
        for record in self.records:
            notes = [
                normalize(str(record.get("target_note", ""))),
                normalize(str(record.get("detail_note", ""))),
                normalize(str(record.get("safe_note", ""))),
            ]
            if key and any(key in note for note in notes):
                return record
        if allow_rebuild and key:
            return self.rebuild_and_find(target)
        return None

    def search(self, query: str, limit: int = 50, allow_rebuild: bool = True) -> list[dict]:
        self.reload_if_changed()
        q = normalize(query)
        if not q:
            return self.records[:limit]
        hits: list[dict] = []
        for record in self.records:
            haystack = "\n".join(
                str(record.get(field, ""))
                for field in (
                    "node_id",
                    "formal_id",
                    "source_id",
                    "year",
                    "subject",
                    "main_knowledge",
                    "core_point",
                    "title",
                    "target_vault",
                    "target_note",
                    "detail_note",
                    "safe_note",
                    "link_status",
                    "search_text",
                )
            )
            if q in normalize(haystack):
                hits.append(record)
                if len(hits) >= limit:
                    break
        if not hits and q and allow_rebuild:
            self.refresh(write_if_missing=True, force_rebuild=True)
            return self.search(query, limit=limit, allow_rebuild=False) if self.records else []
        return hits

    def concept_record(self, code: str) -> dict | None:
        if not re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", code):
            return None
        relative = f"知识点/学习路径/{code}.md"
        note = self.repo / relative
        if not note.is_file() or note.is_symlink() or not note.resolve().is_relative_to(self.repo.resolve()):
            return None
        return {"kind": "knowledge_point", "node_id": code, "target_vault": self.vault,
                "target_note": relative, "obsidian_url": obsidian_url(self.vault, relative),
                "open_url": f"http://127.0.0.1:{BRIDGE_PORT}/concept/{code}"}

    def launch(self, record: dict) -> dict:
        target_vault = str(record.get("target_vault") or self.vault)
        target_note = str(record.get("target_note") or record.get("detail_note") or record.get("safe_note") or "")
        node_id = str(record.get("node_id") or record.get("formal_id") or "")
        url = str(record.get("obsidian_url") or obsidian_url(target_vault, target_note))
        attempts: list[dict] = []
        details = Path(self.data.get("details_root", ""))
        note = details / target_note
        if record.get("kind") == "knowledge_point":
            expected = self.concept_record(node_id)
            if expected is None or record != expected:
                return {"ok": False, "reason": "knowledge_page_unavailable", "attempts": []}
        elif (record.get("visual_ready") is not True
                or target_vault != self.data.get("details_vault")
                or target_note != f"cards/{node_id}.md"
                or not note.is_file() or note.is_symlink()
                or not note.resolve().is_relative_to(details.resolve())):
            return {"ok": False, "reason": "visual_card_unavailable", "attempts": []}
        command = ["/usr/bin/open", url]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "reason": type(exc).__name__, "attempts": []}
        attempts.append(
            {
                "method": "mac-open-obsidian-url",
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
        return {"ok": result.returncode == 0, "method": "mac-open-obsidian-url", "url": url, "attempts": attempts}


def html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #101214; color: #edf1f4; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 28px 20px 56px; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ margin: 0 0 16px; color: #9ca7b1; }}
    a {{ color: #7dd3fc; text-decoration: none; }}
    form {{ display: flex; gap: 8px; margin: 18px 0 20px; }}
    input {{ flex: 1; min-width: 0; border: 1px solid #2b333b; background: #171b1f; color: #edf1f4; padding: 10px 12px; border-radius: 6px; }}
    button, .button {{ border: 1px solid #2b333b; border-radius: 6px; padding: 9px 12px; background: #20262c; color: #edf1f4; cursor: pointer; }}
    .card {{ border: 1px solid #2b333b; border-radius: 8px; background: #171b1f; padding: 13px 14px; margin: 10px 0; }}
    .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    .muted {{ color: #9ca7b1; }}
    code {{ background: #20262c; padding: 2px 5px; border-radius: 4px; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>""".encode("utf-8")


def self_closing_page(node_id: str) -> bytes:
    escaped_node_id = html.escape(node_id)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Opening {escaped_node_id}</title>
  <script>
    (function closeBridgeTab() {{
      function tryClose() {{
        window.open("", "_self");
        window.close();
      }}
      tryClose();
      setTimeout(tryClose, 50);
      setTimeout(function () {{
        document.documentElement.innerHTML = "";
        document.title = "";
      }}, 120);
    }})();
  </script>
  <style>
    html, body {{ margin: 0; width: 100%; height: 100%; background: transparent; }}
  </style>
</head>
<body></body>
</html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    state: BridgeState

    def send_bytes(self, data: bytes, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_bytes(data, status=status, content_type="application/json; charset=utf-8")

    def send_self_closing(self, node_id: str) -> None:
        data = self_closing_page(node_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path != "/refresh":
            self.state.reload_if_changed()

        if path == "/health":
            self.send_json(
                {
                    "ok": True,
                    "system": "kaoyan-408",
                    "default_vault": self.state.vault,
                    "details_vault": self.state.data.get("details_vault", ""),
                    "records": len(self.state.records),
                    "detail_records": self.state.data.get("detail_record_count", "未记录"),
                    "safe_fallbacks": self.state.data.get("safe_fallback_count", "未记录"),
                    "generated_at": self.state.data.get("generated_at", "未记录"),
                    "snapshot": str(self.state.snapshot_path),
                }
            )
            return

        if path == "/refresh":
            self.state.refresh(write_if_missing=True, force_rebuild=True)
            self.send_json({"ok": True, "records": len(self.state.records), "snapshot": str(self.state.snapshot_path)})
            return

        if path.startswith(("/concept/", "/api/concept/", "/handoff/concept/")):
            code = unquote(path.rsplit("/", 1)[-1])
            record = self.state.concept_record(code)
            if record is None:
                self.send_json({"ok": False, "reason": "knowledge_page_unavailable"}, HTTPStatus.NOT_FOUND)
            elif path.startswith("/api/concept/"):
                self.send_json({"ok": True, "record": record})
            elif path.startswith("/handoff/concept/"):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", record["obsidian_url"])
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                launch = self.state.launch(record)
                if launch.get("ok"):
                    self.send_self_closing(code)
                else:
                    self.send_json({"ok": False, "launch": launch}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        if path.startswith("/handoff/"):
            target = unquote(path.removeprefix("/handoff/"))
            record = self.state.find_record(target, allow_rebuild=False)
            if not record or record.get("visual_ready") is not True:
                self.send_json({"ok": False, "reason": "visual_card_unavailable"}, HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", record["obsidian_url"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.startswith("/api/open/"):
            target = path.removeprefix("/api/open/")
            record = self.state.find_record(target)
            if not record:
                self.send_json({"ok": False, "error": "record not found", "target": unquote(target)}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True, "record": record, "launch": self.state.launch(record)})
            return

        if path.startswith("/open/") or path.startswith("/launch/"):
            target = path.split("/", 2)[2] if path.count("/") >= 2 else ""
            self.open_page(target, params)
            return

        if path in {"/open", "/launch"}:
            self.open_page(params.get("id", params.get("q", [""]))[0], params)
            return

        query = params.get("q", [""])[0]
        self.search_page(query)

    def open_page(self, target: str, params: dict[str, list[str]]) -> None:
        target = unquote(target)
        record = self.state.find_record(target)
        if not record:
            hits = self.state.search(target, limit=10)
            cards = "".join(render_record(record) for record in hits)
            body = f"""
            <h1>未找到 408 节点</h1>
            <p>目标：<code>{html.escape(target)}</code></p>
            <form method="get" action="/">
              <input name="q" value="{html.escape(target)}" autofocus>
              <button type="submit">搜索</button>
            </form>
            {cards or '<p>没有匹配记录。可先生成快照，或补详情库卡片/安全节点卡片。</p>'}
            """
            self.send_bytes(html_page("未找到 408 节点", body), HTTPStatus.NOT_FOUND)
            return

        launch = self.state.launch(record)
        show_status = params.get("status", params.get("debug", params.get("view", [""])))[0].lower() in {
            "1",
            "true",
            "yes",
        }
        if launch.get("ok") and not show_status:
            self.send_self_closing(str(record.get("node_id", target)))
            return

        status = "已请求 Obsidian 打开" if launch.get("ok") else "打开请求失败"
        body = f"""
        <h1>{html.escape(status)}</h1>
        <p>节点：<code>{html.escape(str(record.get("node_id", target)))}</code></p>
        <div class="card">
          <div><strong>{html.escape(str(record.get("title", "")))}</strong></div>
          <div class="muted">目标库：<code>{html.escape(str(record.get("target_vault", "")))}</code></div>
          <div class="muted">目标路径：<code>{html.escape(str(record.get("target_note", "")))}</code></div>
          <div class="muted">链接状态：{html.escape(str(record.get("link_status", "")))}</div>
          <div class="muted">打开方式：{html.escape(str(launch.get("method", "unknown")))}</div>
        </div>
        <p><a href="/">返回搜索</a></p>
        """
        self.send_bytes(html_page(status, body))

    def search_page(self, query: str) -> None:
        records = self.state.search(query)
        cards = "".join(render_record(record) for record in records)
        body = f"""
        <h1>408 Obsidian 跳转桥</h1>
        <p>点击节点会打开 <code>kaoyan-408-details</code> 中的可视化卡片；缺少题图时明确报告，不转到节点摘要页。</p>
        <form method="get" action="/">
          <input name="q" value="{html.escape(query)}" placeholder="输入 CO_2015_005、HCO_0001、知识点或核心考点">
          <button type="submit">搜索</button>
        </form>
        <p class="muted">记录数：{len(self.state.records)}；详情卡：{html.escape(str(self.state.data.get("detail_record_count", "未记录")))}；安全兜底：{html.escape(str(self.state.data.get("safe_fallback_count", "未记录")))}；刷新：<a href="/refresh">/refresh</a>；健康检查：<a href="/health">/health</a></p>
        {cards or '<p>没有匹配记录。</p>'}
        """
        self.send_bytes(html_page("408 Obsidian 跳转桥", body))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def render_record(record: dict) -> str:
    node_id = html.escape(str(record.get("node_id", "")))
    title = html.escape(str(record.get("title", node_id)))
    source_id = html.escape(str(record.get("source_id", "")))
    main_knowledge = html.escape(str(record.get("main_knowledge", "")))
    target_vault = html.escape(str(record.get("target_vault", "")))
    target_note = html.escape(str(record.get("target_note", "")))
    link_status = html.escape(str(record.get("link_status", "")))
    question_count = html.escape(str(record.get("question_asset_count", 0)))
    solution_count = html.escape(str(record.get("solution_asset_count", 0)))
    return f"""
    <div class="card">
      <div class="row">
        <div>
          <div><a href="/open/{node_id}"><strong>{node_id}</strong></a> {title}</div>
          <div class="muted">来源：{source_id}；主知识点：{main_knowledge}</div>
          <div class="muted">目标：<code>{target_vault}</code> / <code>{target_note}</code></div>
          <div class="muted">状态：{link_status}；题图：{question_count}；解析图：{solution_count}</div>
        </div>
        <a class="button" href="/open/{node_id}">打开</a>
      </div>
    </div>
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="408 repo root")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT), help="snapshot JSON path")
    parser.add_argument("--vault", default=DEFAULT_VAULT, help="Obsidian vault name")
    parser.add_argument("--host", default="127.0.0.1", help="listen host")
    parser.add_argument("--port", type=int, default=BRIDGE_PORT, help="listen port")
    args = parser.parse_args()

    Handler.state = BridgeState(Path(args.repo).resolve(), Path(args.snapshot).expanduser(), args.vault)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"408 Obsidian bridge: http://{args.host}:{args.port}/")
    print(f"records={len(Handler.state.records)} snapshot={Handler.state.snapshot_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
