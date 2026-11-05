#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 订阅源挑选与管理脚本 (switch.py)
- 数据源: rss-lists.md
- 订阅导出: rss-subscriptions.opml
- 静态挑选页: rss-picker.html
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
MD_FILE = BASE_DIR / "rss-lists.md"
OPML_FILE = BASE_DIR / "rss-subscriptions.opml"
HTML_FILE = BASE_DIR / "rss-picker.html"

# 内置特殊 Feed URL -> Blog URL 兜底映射（用于无法仅从 feed_url 字面规则推导的源）
KNOWN_BLOG_URLS: Dict[str, str] = {
    "https://cloudblog.withgoogle.com/rss/": "https://cloud.google.com/blog/",
    "https://www.microsoft.com/en-us/research/feed/": "https://www.microsoft.com/en-us/research/blog/",
    "https://kubernetes.io/feed.xml": "https://kubernetes.io/blog/",
    "https://www.docker.com/feed/": "https://www.docker.com/blog/",
    "https://www.confluent.io/rss.xml": "https://www.confluent.io/blog/",
    "https://www.scylladb.com/feed/": "https://www.scylladb.com/blog/",
    "https://lambda.com/blog/rss.xml": "https://lambda.ai/blog",
    "https://obsidian.md/feed.xml": "https://obsidian.md/blog",
    "https://feeds.feedburner.com/martinkl?format=xml": "https://martin.kleppmann.com",
}


def derive_blog_url(xml_url: str, opml_url_map: Optional[Dict[str, str]] = None) -> str:
    """
    在内存中从 xml_url 或已有 OPML 映射自动推导博客主页地址 (blog_url / htmlUrl)，
    不修改 rss-lists.md 原有格式。
    """
    url = xml_url.strip()
    if not url:
        return ""

    # 1. 优先使用现有 OPML 中已记录的 htmlUrl
    if opml_url_map and url in opml_url_map and opml_url_map[url]:
        return opml_url_map[url]

    # 2. 内置特殊映射兜底
    if url in KNOWN_BLOG_URLS:
        return KNOWN_BLOG_URLS[url]

    # 3. GitHub Releases Atom: https://github.com/owner/repo/releases.atom -> https://github.com/owner/repo
    gh_match = re.match(r"^(https?://github\.com/[^/]+/[^/]+)/releases\.atom$", url, re.I)
    if gh_match:
        return gh_match.group(1)

    # 4. Medium Feed: https://medium.com/feed/xxx -> https://medium.com/xxx
    medium_match = re.match(r"^(https?://medium\.com)/feed/(.+)$", url, re.I)
    if medium_match:
        return f"{medium_match.group(1)}/{medium_match.group(2)}"

    # 5. 常见 Feed 后缀剥离规则
    suffix_patterns = [
        (r"/blog/feeds/latest/?$", "/blog"),
        (r"/atom/everything/?$", ""),
        (r"/blog/rss\.xml$", "/blog/"),
        (r"/blog/feed\.xml$", "/blog"),
        (r"/blog/index\.xml$", "/blog/"),
        (r"/blog/atom\.xml$", "/blog/"),
        (r"/blog/feed/?$", "/blog/"),
        (r"/blog/rss/?$", "/blog/"),
        (r"/blog\.rss$", "/blog"),
        (r"/rss_feed\.xml$", ""),
        (r"/feeds?/[^/]+\.xml$", ""),
        (r"/feed\.xml$", ""),
        (r"/rss\.xml$", ""),
        (r"/atom\.xml$", ""),
        (r"/index\.xml$", ""),
        (r"/posts\.atom$", ""),
        (r"/feed\.atom$", ""),
        (r"/feed/?$", ""),
        (r"/rss/?$", ""),
    ]
    for pattern, replacement in suffix_patterns:
        new_url, count = re.subn(pattern, replacement, url, flags=re.I)
        if count > 0 and new_url:
            return new_url

    # 6. 默认回退到站点根域名
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


def check_single_url(url: str, timeout: float = 6.0) -> Dict:
    """
    真实检测单个 URL（网站主页或 RSS Feed）的可达性。
    返回 {"ok": bool, "code": int | str}
    """
    target = (url or "").strip()
    if not target:
        return {"ok": False, "code": "EMPTY"}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Range": "bytes=0-1024",
    }
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(target, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = getattr(resp, "status", 200) or 200
            return {"ok": 200 <= code < 400, "code": code}
    except urllib.error.HTTPError as err:
        # 206 Partial Content 视为可达
        if err.code == 206:
            return {"ok": True, "code": 200}
        return {"ok": False, "code": err.code}
    except Exception:
        return {"ok": False, "code": "ERR"}


def check_urls_batch(urls: List[str], max_workers: int = 32) -> Dict[str, Dict]:
    """并发检测一组 URL 的可达性"""
    unique_urls = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    if not unique_urls:
        return {}
    results: Dict[str, Dict] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique_urls))) as pool:
        future_map = {pool.submit(check_single_url, u): u for u in unique_urls}
        for fut, u in future_map.items():
            try:
                results[u] = fut.result()
            except Exception:
                results[u] = {"ok": False, "code": "ERR"}
    return results


def parse_opml(opml_path: Path = OPML_FILE) -> Tuple[str, Dict[str, str], set]:
    """
    解析现有的 rss-subscriptions.opml，返回:
    - opml_title: OPML 标题
    - url_map: {xmlUrl: htmlUrl}
    - subscribed_urls: 已在 OPML 中订阅的 xmlUrl 集合
    """
    default_title = "FeedFlow 订阅（Infra / Lifehacking / 技术博客）"
    url_map: Dict[str, str] = {}
    subscribed_urls: set = set()

    if not opml_path.exists():
        return default_title, url_map, subscribed_urls

    try:
        tree = ET.parse(opml_path)
        root = tree.getroot()
        head_title = root.find("./head/title")
        if head_title is not None and head_title.text:
            default_title = head_title.text.strip()

        for outline in root.findall(".//outline[@xmlUrl]"):
            xml_url = (outline.attrib.get("xmlUrl") or "").strip()
            html_url = (outline.attrib.get("htmlUrl") or "").strip()
            if xml_url:
                subscribed_urls.add(xml_url)
                if html_url:
                    url_map[xml_url] = html_url
    except Exception as exc:
        print(f"[WARN] 解析 OPML 失败: {exc}")

    return default_title, url_map, subscribed_urls


def parse_markdown(md_path: Path = MD_FILE, opml_path: Path = OPML_FILE) -> Dict:
    """
    解析 rss-lists.md 并结合 rss-subscriptions.opml 生成完整内存状态结构。
    保持对 rss-lists.md 原格式的完全兼容。
    """
    opml_title, opml_url_map, subscribed_urls = parse_opml(opml_path)

    doc_title = "FeedFlow 订阅清单（2026-11-05 全部实测验证）"
    groups: List[Dict] = []

    if not md_path.exists():
        return {
            "doc_title": doc_title,
            "opml_title": opml_title,
            "groups": groups,
        }

    content = md_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    current_group: Optional[Dict] = None
    pending_item: Optional[Dict] = None
    item_seq = 1

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 一级标题
        if line.startswith("# ") and not line.startswith("## "):
            doc_title = line[2:].strip()
            continue

        # 二级标题（分组）：例如 ## Infra（45） 或 ## 老牌权威 (12)
        if line.startswith("## "):
            if pending_item and current_group is not None:
                current_group["items"].append(pending_item)
                pending_item = None

            header_text = line[3:].strip()
            # 去掉末尾形如 （45） 或 (45) 的计数展示，保存纯分组名
            group_name = re.sub(r"\s*[（(]\d+[）)]\s*$", "", header_text).strip()
            current_group = {
                "id": f"group_{len(groups) + 1}",
                "name": group_name,
                "items": [],
            }
            groups.append(current_group)
            continue

        # 列表项第一行：- **Title** — Description
        item_match = re.match(r"^-\s+\*\*(.+?)\*\*\s*(?:[—\-–]+\s*(.*))?$", line)
        if item_match:
            if pending_item and current_group is not None:
                current_group["items"].append(pending_item)

            if current_group is None:
                current_group = {
                    "id": f"group_{len(groups) + 1}",
                    "name": "默认分组",
                    "items": [],
                }
                groups.append(current_group)

            title = item_match.group(1).strip()
            desc = (item_match.group(2) or "").strip()
            pending_item = {
                "id": f"item_{item_seq}",
                "title": title,
                "desc": desc,
                "xmlUrl": "",
                "htmlUrl": "",
                "checked": False,
            }
            item_seq += 1
            continue

        # 列表项第二行：缩进的 URL
        if pending_item is not None and re.match(r"^https?://\S+$", line, re.I):
            xml_url = line
            html_url = derive_blog_url(xml_url, opml_url_map)
            # 如果 OPML 存在订阅记录，则对照 OPML 勾选；若 OPML 为空则默认全选
            is_checked = (xml_url in subscribed_urls) if subscribed_urls else True
            pending_item["xmlUrl"] = xml_url
            pending_item["htmlUrl"] = html_url
            pending_item["checked"] = is_checked
            if current_group is not None:
                current_group["items"].append(pending_item)
            pending_item = None

    if pending_item and current_group is not None:
        current_group["items"].append(pending_item)

    return {
        "doc_title": doc_title,
        "opml_title": opml_title,
        "groups": groups,
    }


def serialize_markdown(state: Dict) -> str:
    """
    将内存状态序列化回 rss-lists.md，严格保持原有 Markdown 格式：
    - 一级标题后空两行
    - 二级分组标题自动计算最新数量：## 分组名（数量）
    - 每个条目格式：
      - **标题** — 描述  
        https://feed.url
    """
    doc_title = (state.get("doc_title") or "FeedFlow 订阅清单").strip()
    groups = state.get("groups") or []

    out_lines: List[str] = [f"# {doc_title}", "", ""]

    for idx, group in enumerate(groups):
        group_name = (group.get("name") or "未命名分组").strip()
        items = group.get("items") or []
        out_lines.append(f"## {group_name}（{len(items)}）")
        out_lines.append("")

        for item in items:
            title = (item.get("title") or "").strip()
            desc = (item.get("desc") or "").strip()
            xml_url = (item.get("xmlUrl") or "").strip()
            if desc:
                out_lines.append(f"- **{title}** — {desc}  ")
            else:
                out_lines.append(f"- **{title}**  ")
            if xml_url:
                out_lines.append(f"  {xml_url}")

        if idx < len(groups) - 1:
            out_lines.append("")

    return "\n".join(out_lines) + "\n"


def save_markdown(state: Dict, md_path: Path = MD_FILE) -> None:
    """将当前分组与订阅源同步写回 rss-lists.md"""
    md_text = serialize_markdown(state)
    md_path.write_text(md_text, encoding="utf-8")


def _escape_xml_attr(val: str) -> str:
    """严格按照 OPML/XML 属性规范转义特殊字符"""
    return (
        val.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def serialize_opml(state: Dict) -> str:
    """
    将已勾选 (checked=True) 的订阅源按当前分组生成 OPML 2.0 内容。
    自动同步更新 <head><title> 中的分组名列表。
    """
    _, opml_url_map, _ = parse_opml(OPML_FILE)
    groups = state.get("groups") or []

    active_group_names = [
        (g.get("name") or "").strip()
        for g in groups
        if any(item.get("checked", False) for item in (g.get("items") or []))
    ]
    if active_group_names:
        head_title = f"FeedFlow 订阅（{' / '.join(active_group_names)}）"
    else:
        head_title = state.get("opml_title") or "FeedFlow 订阅"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        f"  <head><title>{html.escape(head_title, quote=False)}</title></head>",
        "  <body>",
    ]

    for group in groups:
        group_name = (group.get("name") or "未命名分组").strip()
        checked_items = [
            item
            for item in (group.get("items") or [])
            if item.get("checked", False) and (item.get("xmlUrl") or "").strip()
        ]
        if not checked_items:
            continue

        g_attr = _escape_xml_attr(group_name)
        lines.append(f'    <outline text="{g_attr}" title="{g_attr}">')
        for item in checked_items:
            title = (item.get("title") or "").strip()
            xml_url = (item.get("xmlUrl") or "").strip()
            html_url = (item.get("htmlUrl") or "").strip() or derive_blog_url(
                xml_url, opml_url_map
            )
            t_attr = _escape_xml_attr(title)
            x_attr = _escape_xml_attr(xml_url)
            h_attr = _escape_xml_attr(html_url)
            lines.append(
                f'      <outline type="rss" text="{t_attr}" title="{t_attr}" xmlUrl="{x_attr}" htmlUrl="{h_attr}"/>'
            )
        lines.append("    </outline>")

    lines.append("  </body>")
    lines.append("</opml>")
    return "\n".join(lines) + "\n"


def save_opml(state: Dict, opml_path: Path = OPML_FILE) -> None:
    """将勾选的订阅源直接写入更新 rss-subscriptions.opml"""
    opml_text = serialize_opml(state)
    opml_path.write_text(opml_text, encoding="utf-8")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>RSS 订阅源挑选与分组管理</title>
  <style>
    :root {
      --bg-page: #121316;
      --bg-header: #16181c;
      --bg-card: #1b1d21;
      --bg-card-checked: #1d2229;
      --border-card: #2c3038;
      --border-checked: rgba(59, 130, 246, 0.5);
      --text-primary: #f1f3f5;
      --text-secondary: #9aa0a6;
      --text-muted: #6e7681;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --green-bg: rgba(16, 185, 129, 0.14);
      --green-text: #10b981;
      --green-border: rgba(16, 185, 129, 0.3);
      --amber-bg: rgba(217, 119, 6, 0.16);
      --amber-text: #f59e0b;
      --amber-border: rgba(217, 119, 6, 0.32);
      --gray-bg: rgba(156, 163, 175, 0.14);
      --gray-text: #9ca3af;
      --gray-border: rgba(156, 163, 175, 0.26);
      --blue-bg: rgba(59, 130, 246, 0.14);
      --blue-text: #60a5fa;
      --blue-border: rgba(59, 130, 246, 0.28);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-page);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.45;
      padding-bottom: 48px;
    }

    /* 顶部简洁工具栏 */
    .top-bar {
      position: sticky;
      top: 0;
      z-index: 100;
      background: var(--bg-header);
      border-bottom: 1px solid var(--border-card);
      padding: 12px 24px;
    }

    .top-bar-inner {
      max-width: 1800px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }

    .title-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .page-title {
      font-size: 17px;
      font-weight: 700;
    }

    .sync-status {
      font-size: 12px;
      padding: 2px 9px;
      border-radius: 999px;
      background: var(--green-bg);
      color: var(--green-text);
      border: 1px solid var(--green-border);
    }

    .sync-status.saving {
      background: var(--amber-bg);
      color: var(--amber-text);
      border-color: var(--amber-border);
    }

    .sync-status.offline {
      background: var(--gray-bg);
      color: var(--gray-text);
      border-color: var(--gray-border);
    }

    .top-controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .search-input {
      width: 240px;
      background: #121316;
      border: 1px solid var(--border-card);
      color: var(--text-primary);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 13px;
      outline: none;
    }

    .search-input:focus {
      border-color: var(--accent);
    }

    .btn {
      background: #252830;
      color: var(--text-primary);
      border: 1px solid #363b47;
      padding: 6px 11px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
    }

    .btn:hover {
      background: #2f333d;
    }

    .btn-primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 600;
    }

    .btn-primary:hover {
      background: var(--accent-hover);
    }

    .btn-sm {
      padding: 3px 8px;
      font-size: 12px;
    }

    .btn-text {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 2px 6px;
      font-size: 12px;
      border-radius: 4px;
      cursor: pointer;
    }

    .btn-text:hover {
      color: var(--text-primary);
      background: #252830;
    }

    .btn-text.danger:hover {
      color: #f87171;
    }

    /* 主容器：宽屏多列排布 */
    .container {
      max-width: 1800px;
      margin: 20px auto 0;
      padding: 0 24px;
    }

    .group-section {
      margin-bottom: 28px;
    }

    .group-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
      padding-bottom: 6px;
      border-bottom: 1px solid #23262d;
      gap: 10px;
      flex-wrap: wrap;
    }

    .group-title-wrap {
      display: flex;
      align-items: baseline;
      gap: 8px;
    }

    .group-title {
      font-size: 17px;
      font-weight: 700;
    }

    .group-count {
      font-size: 13px;
      color: var(--text-secondary);
    }

    .group-actions {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    /* 网格多列排布，消除左右空白 */
    .group-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
      gap: 10px;
    }

    .empty-group-hint {
      grid-column: 1 / -1;
      padding: 16px;
      text-align: center;
      border: 1px dashed var(--border-card);
      border-radius: 8px;
      color: var(--text-muted);
      font-size: 13px;
    }

    /* 博客卡片：所有信息直接常驻展示，无悬浮展开/位移 */
    .feed-card {
      background: var(--bg-card);
      border: 1px solid var(--border-card);
      border-radius: 8px;
      padding: 12px 14px;
      display: flex;
      align-items: flex-start;
      gap: 11px;
    }

    .feed-card.checked {
      background: var(--bg-card-checked);
      border-color: var(--border-checked);
    }

    .feed-checkbox {
      width: 17px;
      height: 17px;
      margin-top: 3px;
      accent-color: var(--accent);
      cursor: pointer;
      flex-shrink: 0;
    }

    .card-body {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 5px;
    }

    .card-header-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .card-title-group {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      min-width: 0;
    }

    .feed-name {
      font-size: 15px;
      font-weight: 700;
      color: var(--text-primary);
      cursor: pointer;
    }

    .card-right-ops {
      display: flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
    }

    .group-select {
      background: #141619;
      color: var(--text-secondary);
      border: 1px solid #323741;
      border-radius: 5px;
      padding: 2px 6px;
      font-size: 12px;
      cursor: pointer;
      outline: none;
    }

    .group-select:hover {
      border-color: #4b5262;
      color: var(--text-primary);
    }

    .feed-desc {
      font-size: 13px;
      color: var(--text-secondary);
      word-break: break-word;
    }

    /* 常驻展示的 Blog URL 与 RSS URL 及可达性状态（点击链接直接打开新标签页） */
    .feed-urls {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 12px;
      margin-top: 2px;
      padding-top: 6px;
      border-top: 1px dashed #262a32;
    }

    .url-row {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }

    .url-label {
      color: var(--text-muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      width: 34px;
      flex-shrink: 0;
    }

    .reach-badge {
      font-size: 11px;
      padding: 0 6px;
      border-radius: 4px;
      flex-shrink: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: var(--gray-bg);
      color: var(--gray-text);
      border: 1px solid var(--gray-border);
    }

    .reach-badge.ok {
      background: var(--green-bg);
      color: var(--green-text);
      border-color: var(--green-border);
    }

    .reach-badge.fail {
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border-color: rgba(239, 68, 68, 0.35);
    }

    .url-link {
      color: #60a5fa;
      text-decoration: none;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      display: block;
    }

    .url-link:hover {
      text-decoration: underline;
      color: #93c5fd;
    }

    /* 弹窗 */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.65);
      z-index: 200;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }

    .modal-backdrop.open {
      display: flex;
    }

    .modal {
      background: #1b1d21;
      border: 1px solid #343944;
      border-radius: 10px;
      width: 100%;
      max-width: 460px;
      padding: 20px;
    }

    .modal-title {
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 14px;
    }

    .form-group {
      margin-bottom: 12px;
    }

    .form-label {
      display: block;
      font-size: 12px;
      color: var(--text-secondary);
      margin-bottom: 5px;
    }

    .form-input {
      width: 100%;
      background: #121316;
      border: 1px solid #2e333d;
      color: var(--text-primary);
      padding: 7px 10px;
      border-radius: 6px;
      font-size: 13px;
      outline: none;
    }

    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 16px;
    }

    .toast-container {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 300;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .toast {
      background: #232730;
      border: 1px solid #3b4252;
      color: var(--text-primary);
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 13px;
    }
  </style>
</head>
<body>

  <header class="top-bar">
    <div class="top-bar-inner">
      <div class="title-area">
        <h1 class="page-title" id="docTitleDisplay">RSS 订阅源挑选</h1>
        <span class="sync-status" id="syncStatusBadge">已同步 rss-lists.md</span>
      </div>
      <div class="top-controls">
        <input
          type="text"
          id="searchInput"
          class="search-input"
          placeholder="搜索名称 / 描述 / URL..."
          oninput="renderAll()"
        />
        <button class="btn" id="checkReachBtn" onclick="checkAllReachability()">检测可达性</button>
        <button class="btn" onclick="openNewGroupModal()">+ 新建分组</button>
        <button class="btn" onclick="openFeedModal('create')">+ 新增源</button>
        <button class="btn" onclick="toggleAllVisible(true)">全选</button>
        <button class="btn" onclick="toggleAllVisible(false)">全不选</button>
        <button class="btn btn-primary" onclick="generateOpml()">
          生成 OPML (<span id="topCheckedCount">0</span>)
        </button>
      </div>
    </div>
  </header>

  <main class="container" id="groupsContainer"></main>

  <!-- 新增/编辑订阅源弹窗 -->
  <div class="modal-backdrop" id="feedModal">
    <div class="modal">
      <div class="modal-title" id="feedModalTitle">编辑订阅源</div>
      <input type="hidden" id="feedModalMode" value="edit" />
      <input type="hidden" id="feedModalGroupId" value="" />
      <input type="hidden" id="feedModalItemId" value="" />

      <div class="form-group">
        <label class="form-label">分组</label>
        <select id="feedGroupInput" class="form-input"></select>
      </div>
      <div class="form-group">
        <label class="form-label">博客名称 *</label>
        <input type="text" id="feedTitleInput" class="form-input" />
      </div>
      <div class="form-group">
        <label class="form-label">博客描述</label>
        <input type="text" id="feedDescInput" class="form-input" />
      </div>
      <div class="form-group">
        <label class="form-label">RSS URL *</label>
        <input type="text" id="feedXmlUrlInput" class="form-input" oninput="previewDerivedBlogUrl()" />
      </div>
      <div class="form-group">
        <label class="form-label">Blog URL（自动推导，可修改）</label>
        <input type="text" id="feedHtmlUrlInput" class="form-input" />
      </div>

      <div class="modal-actions">
        <button class="btn" onclick="closeModal('feedModal')">取消</button>
        <button class="btn btn-primary" onclick="saveFeedModal()">保存</button>
      </div>
    </div>
  </div>

  <!-- 新建/重命名分组弹窗 -->
  <div class="modal-backdrop" id="groupModal">
    <div class="modal">
      <div class="modal-title" id="groupModalTitle">新建分组</div>
      <input type="hidden" id="groupModalMode" value="create" />
      <input type="hidden" id="groupModalId" value="" />

      <div class="form-group">
        <label class="form-label">分组名称 *</label>
        <input type="text" id="groupNameInput" class="form-input" />
      </div>

      <div class="modal-actions">
        <button class="btn" onclick="closeModal('groupModal')">取消</button>
        <button class="btn btn-primary" onclick="saveGroupModal()">确定</button>
      </div>
    </div>
  </div>

  <div class="toast-container" id="toastContainer"></div>

  <script>
    let appState = __INITIAL_STATE_JSON__;
    let isServerConnected = window.location.protocol.startsWith('http');

    const KNOWN_BLOG_URLS = {
      "https://cloudblog.withgoogle.com/rss/": "https://cloud.google.com/blog/",
      "https://www.microsoft.com/en-us/research/feed/": "https://www.microsoft.com/en-us/research/blog/",
      "https://kubernetes.io/feed.xml": "https://kubernetes.io/blog/",
      "https://www.docker.com/feed/": "https://www.docker.com/blog/",
      "https://www.confluent.io/rss.xml": "https://www.confluent.io/blog/",
      "https://www.scylladb.com/feed/": "https://www.scylladb.com/blog/",
      "https://lambda.com/blog/rss.xml": "https://lambda.ai/blog",
      "https://obsidian.md/feed.xml": "https://obsidian.md/blog",
      "https://feeds.feedburner.com/martinkl?format=xml": "https://martin.kleppmann.com"
    };

    function deriveBlogUrlClient(xmlUrl) {
      const url = (xmlUrl || '').trim();
      if (!url) return '';
      if (KNOWN_BLOG_URLS[url]) return KNOWN_BLOG_URLS[url];

      const ghMatch = url.match(/^(https?:\/\/github\.com\/[^/]+\/[^/]+)\/releases\.atom$/i);
      if (ghMatch) return ghMatch[1];

      const medMatch = url.match(/^(https?:\/\/medium\.com)\/feed\/(.+)$/i);
      if (medMatch) return `${medMatch[1]}/${medMatch[2]}`;

      const rules = [
        [/\/blog\/feeds\/latest\/?$/i, '/blog'],
        [/\/atom\/everything\/?$/i, ''],
        [/\/blog\/rss\.xml$/i, '/blog/'],
        [/\/blog\/feed\.xml$/i, '/blog'],
        [/\/blog\/index\.xml$/i, '/blog/'],
        [/\/blog\/atom\.xml$/i, '/blog/'],
        [/\/blog\/feed\/?$/i, '/blog/'],
        [/\/blog\/rss\/?$/i, '/blog/'],
        [/\/blog\.rss$/i, '/blog'],
        [/\/rss_feed\.xml$/i, ''],
        [/\/feeds?\/[^/]+\.xml$/i, ''],
        [/\/feed\.xml$/i, ''],
        [/\/rss\.xml$/i, ''],
        [/\/atom\.xml$/i, ''],
        [/\/index\.xml$/i, ''],
        [/\/posts\.atom$/i, ''],
        [/\/feed\.atom$/i, ''],
        [/\/feed\/?$/i, ''],
        [/\/rss\/?$/i, '']
      ];
      for (const [regex, repl] of rules) {
        if (regex.test(url)) {
          return url.replace(regex, repl);
        }
      }
      try {
        const parsed = new URL(url);
        return `${parsed.protocol}//${parsed.host}`;
      } catch (e) {
        return url;
      }
    }

    // 记录每个 URL 的实时可达性检测结果: { [url]: { state: 'checking' | 'ok' | 'fail', code: 200 | 'ERR' } }
    const urlReachMap = {};

    function showToast(msg) {
      const container = document.getElementById('toastContainer');
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = msg;
      container.appendChild(el);
      setTimeout(() => el.remove(), 2200);
    }

    function updateSyncBadge(state, text) {
      const badge = document.getElementById('syncStatusBadge');
      badge.className = 'sync-status ' + (state === 'saving' ? 'saving' : state === 'offline' ? 'offline' : '');
      badge.textContent = text;
    }

    function getReachBadgeHtml(url) {
      const info = urlReachMap[url];
      if (!info) {
        return `<span class="reach-badge" data-reach-url="${escapeHtml(url)}">待检测</span>`;
      }
      if (info.state === 'checking') {
        return `<span class="reach-badge" data-reach-url="${escapeHtml(url)}">检测中</span>`;
      }
      if (info.state === 'ok') {
        return `<span class="reach-badge ok" data-reach-url="${escapeHtml(url)}">可达</span>`;
      }
      return `<span class="reach-badge fail" data-reach-url="${escapeHtml(url)}">不可达</span>`;
    }

    function updateUrlReachDom(url) {
      const info = urlReachMap[url];
      if (!info) return;
      document.querySelectorAll('.reach-badge').forEach(el => {
        if (el.getAttribute('data-reach-url') === url) {
          el.className = 'reach-badge' + (info.state === 'ok' ? ' ok' : info.state === 'fail' ? ' fail' : '');
          el.textContent = info.state === 'checking' ? '检测中' : info.state === 'ok' ? '可达' : '不可达';
        }
      });
    }

    async function checkAllReachability() {
      if (!isServerConnected) {
        showToast('请通过 py switch.py 启动服务以检测 URL 与 Feed 可达性');
        return;
      }
      const btn = document.getElementById('checkReachBtn');
      if (btn) btn.disabled = true;

      const allUrls = [];
      appState.groups.forEach(g => {
        g.items.forEach(item => {
          const blogUrl = item.htmlUrl || deriveBlogUrlClient(item.xmlUrl);
          if (blogUrl) allUrls.push(blogUrl);
          if (item.xmlUrl) allUrls.push(item.xmlUrl);
        });
      });
      const uniqueUrls = Array.from(new Set(allUrls));
      uniqueUrls.forEach(u => {
        urlReachMap[u] = { state: 'checking' };
        updateUrlReachDom(u);
      });

      const batchSize = 24;
      for (let i = 0; i < uniqueUrls.length; i += batchSize) {
        const batch = uniqueUrls.slice(i, i + batchSize);
        if (btn) btn.textContent = `检测中 (${Math.min(i + batch.length, uniqueUrls.length)}/${uniqueUrls.length})`;
        try {
          const res = await fetch('/api/check-urls', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: batch })
          });
          if (res.ok) {
            const data = await res.json();
            const results = data.results || {};
            Object.keys(results).forEach(u => {
              const r = results[u];
              urlReachMap[u] = { state: r.ok ? 'ok' : 'fail', code: r.code };
              updateUrlReachDom(u);
            });
          }
        } catch (err) {
          console.error(err);
        }
      }

      if (btn) {
        btn.disabled = false;
        btn.textContent = '检测可达性';
      }
      showToast('已完成全部 Blog URL 与 RSS 可达性检测');
    }

    async function syncMarkdownToServer(reasonMsg) {
      for (const g of appState.groups) {
        for (const item of g.items) {
          if (!item.htmlUrl) item.htmlUrl = deriveBlogUrlClient(item.xmlUrl);
        }
      }

      if (!isServerConnected) {
        updateSyncBadge('offline', '离线模式');
        return;
      }

      try {
        updateSyncBadge('saving', '同步中...');
        const res = await fetch('/api/sync-md', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(appState)
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        updateSyncBadge('ok', '已同步 rss-lists.md');
        if (reasonMsg) showToast(reasonMsg);
      } catch (err) {
        isServerConnected = false;
        updateSyncBadge('offline', '离线模式');
      }
    }

    async function generateOpml() {
      if (isServerConnected) {
        try {
          const res = await fetch('/api/generate-opml', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(appState)
          });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const data = await res.json();
          showToast(`已生成 rss-subscriptions.opml（${data.checked_count} 个源）`);
          return;
        } catch (err) {
          console.warn(err);
        }
      }
      const activeGroups = appState.groups.filter(g => g.items.some(i => i.checked));
      const headTitle = activeGroups.length > 0
        ? `FeedFlow 订阅（${activeGroups.map(g => g.name).join(' / ')}）`
        : (appState.opml_title || 'FeedFlow 订阅');
      const lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        `  <head><title>${escapeHtml(headTitle)}</title></head>`,
        '  <body>'
      ];
      activeGroups.forEach(g => {
        const gAttr = escapeXmlAttr(g.name);
        lines.push(`    <outline text="${gAttr}" title="${gAttr}">`);
        g.items.filter(i => i.checked && i.xmlUrl).forEach(item => {
          const tAttr = escapeXmlAttr(item.title);
          const xAttr = escapeXmlAttr(item.xmlUrl);
          const hAttr = escapeXmlAttr(item.htmlUrl || deriveBlogUrlClient(item.xmlUrl));
          lines.push(`      <outline type="rss" text="${tAttr}" title="${tAttr}" xmlUrl="${xAttr}" htmlUrl="${hAttr}"/>`);
        });
        lines.push('    </outline>');
      });
      lines.push('  </body>', '</opml>');
      const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/x-opml;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'rss-subscriptions.opml';
      a.click();
    }

    function getCheckedCount() {
      let c = 0;
      for (const g of appState.groups) {
        for (const item of g.items) {
          if (item.checked) c++;
        }
      }
      return c;
    }

    function escapeHtml(str) {
      return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function escapeXmlAttr(val) {
      return escapeHtml(val).replace(/'/g, '&#x27;');
    }

    function renderAll() {
      document.getElementById('docTitleDisplay').textContent = appState.doc_title || 'RSS 订阅源挑选';
      const query = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      const container = document.getElementById('groupsContainer');

      let checkedFeeds = 0;
      const groupOptionsCache = appState.groups.map(g => ({
        id: g.id,
        label: escapeHtml(g.name)
      }));

      const sectionsHtml = [];

      appState.groups.forEach(group => {
        const groupCheckedCount = group.items.filter(i => i.checked).length;
        checkedFeeds += groupCheckedCount;

        const visibleItems = query
          ? group.items.filter(item => `${item.title} ${item.desc} ${item.xmlUrl} ${item.htmlUrl}`.toLowerCase().includes(query))
          : group.items;

        if (query && visibleItems.length === 0) return;

        const groupOptionsHtml = groupOptionsCache.map(opt =>
          `<option value="${opt.id}" ${opt.id === group.id ? 'selected' : ''}>${opt.label}</option>`
        ).join('');

        let cardsHtml = '';
        if (visibleItems.length === 0) {
          cardsHtml = `<div class="empty-group-hint">暂无订阅源</div>`;
        } else {
          cardsHtml = visibleItems.map(item => {
            const blogUrl = item.htmlUrl || deriveBlogUrlClient(item.xmlUrl);

            return `
              <div class="feed-card${item.checked ? ' checked' : ''}" data-item-id="${item.id}">
                <input
                  type="checkbox"
                  class="feed-checkbox"
                  ${item.checked ? 'checked' : ''}
                  onchange="toggleItemCheck('${group.id}', '${item.id}', this)"
                />
                <div class="card-body">
                  <div class="card-header-row">
                    <div class="card-title-group">
                      <span class="feed-name" onclick="toggleItemByName('${group.id}', '${item.id}', this)">${escapeHtml(item.title)}</span>
                    </div>
                    <div class="card-right-ops">
                      <select class="group-select" onchange="moveFeedToGroup('${group.id}', '${item.id}', this.value)">
                        ${groupOptionsHtml}
                      </select>
                      <button class="btn-text" onclick="openFeedModal('edit', '${group.id}', '${item.id}')">编辑</button>
                      <button class="btn-text danger" onclick="deleteFeed('${group.id}', '${item.id}')">×</button>
                    </div>
                  </div>
                  <div class="feed-desc">${escapeHtml(item.desc || '')}</div>
                  <div class="feed-urls">
                    <div class="url-row">
                      <span class="url-label">Blog:</span>
                      ${getReachBadgeHtml(blogUrl)}
                      <a class="url-link" href="${escapeHtml(blogUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(blogUrl)}</a>
                    </div>
                    <div class="url-row">
                      <span class="url-label">RSS:</span>
                      ${getReachBadgeHtml(item.xmlUrl)}
                      <a class="url-link" href="${escapeHtml(item.xmlUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.xmlUrl)}</a>
                    </div>
                  </div>
                </div>
              </div>
            `;
          }).join('');
        }

        sectionsHtml.push(`
          <section class="group-section" data-group-id="${group.id}">
            <div class="group-header">
              <div class="group-title-wrap">
                <h2 class="group-title">${escapeHtml(group.name)}</h2>
                <span class="group-count" id="groupStat_${group.id}">(${groupCheckedCount}/${group.items.length})</span>
              </div>
              <div class="group-actions">
                <button class="btn btn-sm" onclick="toggleGroupCheck('${group.id}', true)">全选本组</button>
                <button class="btn btn-sm" onclick="toggleGroupCheck('${group.id}', false)">全不选</button>
                <button class="btn btn-sm" onclick="openRenameGroupModal('${group.id}')">重命名</button>
                <button class="btn btn-sm" onclick="deleteGroup('${group.id}')">删除分组</button>
              </div>
            </div>
            <div class="group-list">${cardsHtml}</div>
          </section>
        `);
      });

      container.innerHTML = sectionsHtml.join('');
      document.getElementById('topCheckedCount').textContent = checkedFeeds;
    }

    function toggleItemCheck(groupId, itemId, checkboxEl) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      const item = group.items.find(i => i.id === itemId);
      if (!item) return;
      item.checked = checkboxEl.checked;

      const card = checkboxEl.closest('.feed-card');
      if (card) card.classList.toggle('checked', item.checked);

      const groupCheckedCount = group.items.filter(i => i.checked).length;
      const statEl = document.getElementById(`groupStat_${group.id}`);
      if (statEl) statEl.textContent = `(${groupCheckedCount}/${group.items.length})`;
      document.getElementById('topCheckedCount').textContent = getCheckedCount();
    }

    function toggleItemByName(groupId, itemId, nameEl) {
      const card = nameEl.closest('.feed-card');
      if (!card) return;
      const checkbox = card.querySelector('.feed-checkbox');
      if (!checkbox) return;
      checkbox.checked = !checkbox.checked;
      toggleItemCheck(groupId, itemId, checkbox);
    }

    function toggleGroupCheck(groupId, checkedState) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      group.items.forEach(item => { item.checked = checkedState; });
      renderAll();
    }

    function toggleAllVisible(checkedState) {
      const query = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      appState.groups.forEach(group => {
        group.items.forEach(item => {
          if (!query || `${item.title} ${item.desc} ${item.xmlUrl} ${item.htmlUrl}`.toLowerCase().includes(query)) {
            item.checked = checkedState;
          }
        });
      });
      renderAll();
    }

    function moveFeedToGroup(fromGroupId, itemId, toGroupId) {
      if (fromGroupId === toGroupId) return;
      const fromGroup = appState.groups.find(g => g.id === fromGroupId);
      const toGroup = appState.groups.find(g => g.id === toGroupId);
      if (!fromGroup || !toGroup) return;
      const idx = fromGroup.items.findIndex(i => i.id === itemId);
      if (idx === -1) return;
      const [movedItem] = fromGroup.items.splice(idx, 1);
      toGroup.items.push(movedItem);
      renderAll();
      syncMarkdownToServer(`已移动「${movedItem.title}」至「${toGroup.name}」`);
    }

    function deleteFeed(groupId, itemId) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      const idx = group.items.findIndex(i => i.id === itemId);
      if (idx === -1) return;
      const item = group.items[idx];
      if (!confirm(`确定删除「${item.title}」吗？`)) return;
      group.items.splice(idx, 1);
      renderAll();
      syncMarkdownToServer(`已删除「${item.title}」`);
    }

    function openNewGroupModal() {
      document.getElementById('groupModalMode').value = 'create';
      document.getElementById('groupModalId').value = '';
      document.getElementById('groupModalTitle').textContent = '新建分组';
      document.getElementById('groupNameInput').value = '';
      document.getElementById('groupModal').classList.add('open');
      setTimeout(() => document.getElementById('groupNameInput').focus(), 50);
    }

    function openRenameGroupModal(groupId) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      document.getElementById('groupModalMode').value = 'rename';
      document.getElementById('groupModalId').value = groupId;
      document.getElementById('groupModalTitle').textContent = '重命名分组';
      document.getElementById('groupNameInput').value = group.name;
      document.getElementById('groupModal').classList.add('open');
      setTimeout(() => document.getElementById('groupNameInput').focus(), 50);
    }

    function saveGroupModal() {
      const mode = document.getElementById('groupModalMode').value;
      const name = (document.getElementById('groupNameInput').value || '').trim();
      if (!name) return;
      if (mode === 'create') {
        appState.groups.push({ id: 'group_' + Date.now(), name, items: [] });
        closeModal('groupModal');
        renderAll();
        syncMarkdownToServer(`已新建分组「${name}」`);
      } else {
        const groupId = document.getElementById('groupModalId').value;
        const group = appState.groups.find(g => g.id === groupId);
        if (group) {
          group.name = name;
          closeModal('groupModal');
          renderAll();
          syncMarkdownToServer(`已重命名分组为「${name}」`);
        }
      }
    }

    function deleteGroup(groupId) {
      const idx = appState.groups.findIndex(g => g.id === groupId);
      if (idx === -1) return;
      const group = appState.groups[idx];
      if (!confirm(`确定删除分组「${group.name}」吗？`)) return;
      appState.groups.splice(idx, 1);
      renderAll();
      syncMarkdownToServer(`已删除分组「${group.name}」`);
    }

    function openFeedModal(mode, groupId, itemId) {
      if (appState.groups.length === 0) return;
      document.getElementById('feedModalMode').value = mode;
      const groupSelect = document.getElementById('feedGroupInput');
      groupSelect.innerHTML = appState.groups.map(g =>
        `<option value="${g.id}">${escapeHtml(g.name)}</option>`
      ).join('');

      if (mode === 'create') {
        document.getElementById('feedModalTitle').textContent = '新增订阅源';
        const targetGid = groupId || appState.groups[0].id;
        groupSelect.value = targetGid;
        document.getElementById('feedModalGroupId').value = targetGid;
        document.getElementById('feedModalItemId').value = '';
        document.getElementById('feedTitleInput').value = '';
        document.getElementById('feedDescInput').value = '';
        document.getElementById('feedXmlUrlInput').value = '';
        document.getElementById('feedHtmlUrlInput').value = '';
      } else {
        document.getElementById('feedModalTitle').textContent = '编辑订阅源';
        const group = appState.groups.find(g => g.id === groupId);
        if (!group) return;
        const item = group.items.find(i => i.id === itemId);
        if (!item) return;
        groupSelect.value = groupId;
        document.getElementById('feedModalGroupId').value = groupId;
        document.getElementById('feedModalItemId').value = itemId;
        document.getElementById('feedTitleInput').value = item.title || '';
        document.getElementById('feedDescInput').value = item.desc || '';
        document.getElementById('feedXmlUrlInput').value = item.xmlUrl || '';
        document.getElementById('feedHtmlUrlInput').value = item.htmlUrl || deriveBlogUrlClient(item.xmlUrl);
      }
      document.getElementById('feedModal').classList.add('open');
    }

    function previewDerivedBlogUrl() {
      const xmlUrl = document.getElementById('feedXmlUrlInput').value;
      document.getElementById('feedHtmlUrlInput').value = deriveBlogUrlClient(xmlUrl);
    }

    function saveFeedModal() {
      const mode = document.getElementById('feedModalMode').value;
      const targetGroupId = document.getElementById('feedGroupInput').value;
      const title = (document.getElementById('feedTitleInput').value || '').trim();
      const desc = (document.getElementById('feedDescInput').value || '').trim();
      const xmlUrl = (document.getElementById('feedXmlUrlInput').value || '').trim();
      const htmlUrl = (document.getElementById('feedHtmlUrlInput').value || '').trim() || deriveBlogUrlClient(xmlUrl);
      if (!title || !xmlUrl) return;

      const targetGroup = appState.groups.find(g => g.id === targetGroupId);
      if (!targetGroup) return;

      if (mode === 'create') {
        targetGroup.items.push({
          id: 'item_' + Date.now(),
          title,
          desc,
          xmlUrl,
          htmlUrl,
          checked: true
        });
        closeModal('feedModal');
        renderAll();
        syncMarkdownToServer(`已新增「${title}」`);
      } else {
        const origGroupId = document.getElementById('feedModalGroupId').value;
        const itemId = document.getElementById('feedModalItemId').value;
        const origGroup = appState.groups.find(g => g.id === origGroupId);
        if (!origGroup) return;
        const idx = origGroup.items.findIndex(i => i.id === itemId);
        if (idx === -1) return;
        const item = origGroup.items[idx];
        item.title = title;
        item.desc = desc;
        item.xmlUrl = xmlUrl;
        item.htmlUrl = htmlUrl;
        if (origGroupId !== targetGroupId) {
          origGroup.items.splice(idx, 1);
          targetGroup.items.push(item);
        }
        closeModal('feedModal');
        renderAll();
        syncMarkdownToServer(`已更新「${title}」`);
      }
    }

    function closeModal(id) {
      document.getElementById(id).classList.remove('open');
    }

    if (!isServerConnected) {
      updateSyncBadge('offline', '离线模式');
    }
    renderAll();
    if (isServerConnected) {
      checkAllReachability();
    }
  </script>
</body>
</html>
"""


def build_html(state: Optional[Dict] = None, html_path: Path = HTML_FILE) -> str:
    """将当前状态注入 HTML 模板并生成 rss-picker.html"""
    if state is None:
        state = parse_markdown()
    state_json = json.dumps(state, ensure_ascii=False)
    html_content = HTML_TEMPLATE.replace("__INITIAL_STATE_JSON__", state_json)
    html_path.write_text(html_content, encoding="utf-8")
    return html_content


class RSSPickerRequestHandler(BaseHTTPRequestHandler):
    """本地双向同步 HTTP 服务：实时将网页端的分组/订阅源修改同步回 rss-lists.md 与 rss-subscriptions.opml"""

    def log_message(self, format: str, *args) -> None:
        # 简化终端日志输出
        pass

    def _send_json(self, status_code: int, payload: Dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed_path = urllib.parse.urlsplit(self.path).path
        if parsed_path in ("/", "/index.html", "/rss-picker.html"):
            state = parse_markdown()
            html_content = build_html(state)
            body = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed_path == "/api/state":
            state = parse_markdown()
            self._send_json(200, state)
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed_path = urllib.parse.urlsplit(self.path).path

        if parsed_path == "/api/check-urls":
            try:
                payload = self._read_json_body()
                urls = payload.get("urls") or []
                results = check_urls_batch(urls)
                self._send_json(200, {"ok": True, "results": results})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if parsed_path == "/api/sync-md":
            try:
                state = self._read_json_body()
                save_markdown(state, MD_FILE)
                build_html(state, HTML_FILE)
                total_items = sum(len(g.get("items") or []) for g in (state.get("groups") or []))
                print(f"[SYNC] 已实时同步修改至 {MD_FILE.name} （共 {len(state.get('groups') or [])} 个分组，{total_items} 个源）")
                self._send_json(200, {"ok": True, "total_items": total_items})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if parsed_path == "/api/generate-opml":
            try:
                state = self._read_json_body()
                # 同时确保结构修改也已同步到 rss-lists.md 与 rss-picker.html
                save_markdown(state, MD_FILE)
                save_opml(state, OPML_FILE)
                build_html(state, HTML_FILE)
                checked_count = sum(
                    1
                    for g in (state.get("groups") or [])
                    for item in (g.get("items") or [])
                    if item.get("checked", False)
                )
                print(f"[OPML] 已生成并更新 {OPML_FILE.name} （共导出 {checked_count} 个已勾选订阅源）")
                self._send_json(200, {"ok": True, "checked_count": checked_count})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self.send_error(404, "Not Found")


def find_free_port(start_port: int = 8765) -> int:
    """查找本地可用端口"""
    for port in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="RSS 订阅源挑选与双向同步工具")
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="仅根据 rss-lists.md 生成 rss-picker.html，不启动本地同步服务",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="本地同步服务起始端口 (默认: 8765)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动服务时不自动弹出浏览器标签页",
    )
    args = parser.parse_args()

    state = parse_markdown()
    build_html(state)
    total_groups = len(state.get("groups") or [])
    total_items = sum(len(g.get("items") or []) for g in (state.get("groups") or []))
    print(f"[INIT] 已解析 {MD_FILE.name}：{total_groups} 个分组，{total_items} 个订阅源")
    print(f"[INIT] 已生成挑选页：{HTML_FILE}")

    if args.build_only:
        return

    port = find_free_port(args.port)
    server = HTTPServer(("127.0.0.1", port), RSSPickerRequestHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"[SERVER] 本地双向同步服务已启动: {url}")
    print("[SERVER] 在网页中移动/编辑/增删分组或订阅源将实时同步回 rss-lists.md，按 Ctrl+C 停止服务。")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open_new_tab(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] 已退出本地同步服务。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


