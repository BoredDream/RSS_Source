#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 订阅源挑选与管理脚本 (switch.py)
- 数据源: rss-lists.md
- 订阅导出: rss-subscriptions.opml
- 静态挑选页: rss-picker.html
"""

import argparse
import html
import json
import os
import re
import socket
import threading
import urllib.parse
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


def extract_badges(title: str, desc: str, xml_url: str) -> List[Dict[str, str]]:
    """
    根据描述和 URL 自动在内存中提取状态徽章（模仿参考 HTML 挑选页样式）：
    - status: 在更 (green) | 慢更 (amber) | 存档 (gray)
    - extra: Release (blue) | 需代理 (orange) 等
    """
    badges: List[Dict[str, str]] = []

    if re.search(r"存档|停更|已逝|最后一篇", desc):
        badges.append({"label": "存档", "type": "archived"})
    elif re.search(r"更新慢|慢更|低频|偶发", desc):
        badges.append({"label": "慢更", "type": "slow"})
    else:
        badges.append({"label": "在更", "type": "active"})

    if "releases.atom" in xml_url.lower() or "github 发布" in desc.lower():
        badges.append({"label": "Release", "type": "release"})

    if re.search(r"需代理|墙内镜像|blogspot|medium\.com|substack\.com", desc.lower() + " " + xml_url.lower()):
        if re.search(r"需代理|blogspot", desc.lower() + " " + xml_url.lower()):
            badges.append({"label": "⚠️ 需代理", "type": "proxy"})

    return badges


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
                "badges": [],
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
            pending_item["badges"] = extract_badges(
                pending_item["title"], pending_item["desc"], xml_url
            )
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
      --bg-header: rgba(22, 24, 28, 0.92);
      --bg-card: #1b1d21;
      --bg-card-hover: #212429;
      --bg-card-checked: #1d2228;
      --border-card: #2c3038;
      --border-card-hover: #404652;
      --border-checked: rgba(59, 130, 246, 0.45);
      --text-primary: #f1f3f5;
      --text-secondary: #9aa0a6;
      --text-muted: #6c727f;
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
      --orange-bg: rgba(245, 158, 11, 0.16);
      --orange-text: #fbbf24;
      --orange-border: rgba(245, 158, 11, 0.35);
      --blue-bg: rgba(59, 130, 246, 0.14);
      --blue-text: #60a5fa;
      --blue-border: rgba(59, 130, 246, 0.28);
      --danger: #ef4444;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-page);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      line-height: 1.5;
      padding-bottom: 90px;
      -webkit-font-smoothing: antialiased;
    }

    /* 顶部粘性工具栏 */
    .top-bar {
      position: sticky;
      top: 0;
      z-index: 100;
      background: var(--bg-header);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border-card);
      padding: 14px 24px;
    }

    .top-bar-inner {
      max-width: 960px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
    }

    .page-title-wrap {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .page-title {
      font-size: 18px;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: 0.2px;
    }

    .sync-status {
      font-size: 12px;
      padding: 3px 10px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--green-bg);
      color: var(--green-text);
      border: 1px solid var(--green-border);
      transition: all 0.2s;
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

    .top-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .btn {
      background: #252830;
      color: var(--text-primary);
      border: 1px solid #363b47;
      padding: 6px 12px;
      border-radius: 7px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: all 0.15s ease;
    }

    .btn:hover {
      background: #2f333d;
      border-color: #4b5262;
    }

    .btn-primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 600;
      padding: 7px 15px;
    }

    .btn-primary:hover {
      background: var(--accent-hover);
      border-color: var(--accent-hover);
    }

    .btn-sm {
      padding: 4px 9px;
      font-size: 12px;
      border-radius: 6px;
    }

    .btn-danger:hover {
      background: rgba(239, 68, 68, 0.18);
      border-color: rgba(239, 68, 68, 0.45);
      color: #f87171;
    }

    /* 搜索与筛选栏 */
    .filter-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }

    .search-box {
      flex: 1;
      min-width: 220px;
      position: relative;
    }

    .search-input {
      width: 100%;
      background: #16181c;
      border: 1px solid var(--border-card);
      color: var(--text-primary);
      padding: 7px 12px 7px 32px;
      border-radius: 7px;
      font-size: 13px;
      outline: none;
      transition: border-color 0.15s;
    }

    .search-input:focus {
      border-color: var(--accent);
    }

    .search-icon {
      position: absolute;
      left: 10px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 13px;
      pointer-events: none;
    }

    .filter-pills {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }

    .pill {
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 999px;
      background: #1b1d21;
      border: 1px solid var(--border-card);
      color: var(--text-secondary);
      cursor: pointer;
      user-select: none;
      transition: all 0.15s;
    }

    .pill:hover {
      border-color: var(--border-card-hover);
      color: var(--text-primary);
    }

    .pill.active {
      background: var(--blue-bg);
      color: var(--blue-text);
      border-color: var(--blue-border);
      font-weight: 600;
    }

    /* 主容器 */
    .container {
      max-width: 960px;
      margin: 28px auto 0;
      padding: 0 24px;
    }

    /* 分组区块 */
    .group-section {
      margin-bottom: 34px;
      border-radius: 12px;
      transition: outline 0.15s;
    }

    .group-section.drag-over {
      outline: 2px dashed var(--accent);
      outline-offset: 8px;
    }

    .group-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      padding: 0 4px;
      gap: 12px;
      flex-wrap: wrap;
    }

    .group-title-area {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .group-title {
      font-size: 18px;
      font-weight: 700;
      color: var(--text-primary);
      letter-spacing: 0.3px;
    }

    .group-count {
      font-size: 16px;
      font-weight: 600;
      color: var(--text-secondary);
    }

    .group-actions {
      display: flex;
      align-items: center;
      gap: 6px;
      opacity: 0.75;
      transition: opacity 0.15s;
    }

    .group-section:hover .group-actions {
      opacity: 1;
    }

    .group-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-height: 24px;
    }

    .empty-group-hint {
      padding: 20px;
      text-align: center;
      border: 1px dashed var(--border-card);
      border-radius: 10px;
      color: var(--text-muted);
      font-size: 13px;
    }

    /* 博客卡片（还原参考图视觉风格） */
    .feed-card {
      background: var(--bg-card);
      border: 1px solid var(--border-card);
      border-radius: 10px;
      padding: 14px 18px;
      display: flex;
      align-items: flex-start;
      gap: 14px;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
      position: relative;
    }

    .feed-card:hover {
      background: var(--bg-card-hover);
      border-color: var(--border-card-hover);
    }

    .feed-card.checked {
      background: var(--bg-card-checked);
      border-color: var(--border-checked);
    }

    .feed-card.dragging {
      opacity: 0.45;
      border-style: dashed;
    }

    /* 自定义复选框 */
    .checkbox-wrap {
      padding-top: 2px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .custom-checkbox {
      width: 19px;
      height: 19px;
      border: 2px solid #525866;
      border-radius: 5px;
      background: transparent;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s ease;
    }

    .checkbox-wrap:hover .custom-checkbox {
      border-color: #7c8596;
    }

    .feed-card.checked .custom-checkbox {
      background: var(--accent);
      border-color: var(--accent);
    }

    .custom-checkbox svg {
      width: 13px;
      height: 13px;
      stroke: #fff;
      stroke-width: 3;
      fill: none;
      opacity: 0;
      transform: scale(0.8);
      transition: all 0.12s ease;
    }

    .feed-card.checked .custom-checkbox svg {
      opacity: 1;
      transform: scale(1);
    }

    /* 卡片主体内容 */
    .card-body {
      flex: 1;
      min-width: 0;
    }

    .card-top-line {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 5px;
    }

    .feed-title-link {
      font-size: 15.5px;
      font-weight: 700;
      color: var(--text-primary);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      cursor: pointer;
      transition: color 0.15s;
    }

    .feed-title-link:hover {
      color: #60a5fa;
      text-decoration: underline;
    }

    .external-icon {
      font-size: 12px;
      color: var(--text-muted);
      opacity: 0;
      transition: opacity 0.15s, transform 0.15s;
    }

    .feed-title-link:hover .external-icon {
      opacity: 1;
      transform: translate(1px, -1px);
      color: #60a5fa;
    }

    /* 状态徽章 */
    .badge {
      font-size: 11.5px;
      font-weight: 600;
      padding: 1.5px 9px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      line-height: 1.4;
      user-select: none;
    }

    .badge-active {
      background: var(--green-bg);
      color: var(--green-text);
      border: 1px solid var(--green-border);
    }

    .badge-slow {
      background: var(--amber-bg);
      color: var(--amber-text);
      border: 1px solid var(--amber-border);
    }

    .badge-archived {
      background: var(--gray-bg);
      color: var(--gray-text);
      border: 1px solid var(--gray-border);
    }

    .badge-proxy {
      background: var(--orange-bg);
      color: var(--orange-text);
      border: 1px solid var(--orange-border);
    }

    .badge-release {
      background: var(--blue-bg);
      color: var(--blue-text);
      border: 1px solid var(--blue-border);
    }

    .feed-desc {
      font-size: 13.5px;
      color: var(--text-secondary);
      line-height: 1.5;
      word-break: break-word;
    }

    .feed-urls-meta {
      margin-top: 5px;
      font-size: 11.5px;
      color: var(--text-muted);
      display: none;
      gap: 12px;
      flex-wrap: wrap;
    }

    .feed-card:hover .feed-urls-meta {
      display: flex;
    }

    .meta-link {
      color: var(--text-muted);
      text-decoration: none;
    }

    .meta-link:hover {
      color: var(--text-secondary);
      text-decoration: underline;
    }

    /* 卡片右侧快捷操作区 */
    .card-controls {
      display: flex;
      align-items: center;
      gap: 6px;
      opacity: 0;
      transition: opacity 0.15s ease;
      flex-shrink: 0;
    }

    .feed-card:hover .card-controls {
      opacity: 1;
    }

    .move-select {
      background: #15171a;
      color: var(--text-secondary);
      border: 1px solid #323741;
      border-radius: 6px;
      padding: 3px 7px;
      font-size: 12px;
      cursor: pointer;
      outline: none;
    }

    .move-select:hover {
      border-color: #4b5262;
      color: var(--text-primary);
    }

    .icon-btn {
      background: #23262d;
      border: 1px solid #323741;
      color: var(--text-secondary);
      width: 26px;
      height: 26px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 12px;
      transition: all 0.15s;
    }

    .icon-btn:hover {
      background: #2e323b;
      color: var(--text-primary);
      border-color: #4b5262;
    }

    .icon-btn.delete:hover {
      background: rgba(239, 68, 68, 0.16);
      color: #f87171;
      border-color: rgba(239, 68, 68, 0.4);
    }

    .drag-handle {
      cursor: grab;
      color: var(--text-muted);
      padding: 2px 4px;
      font-size: 15px;
      user-select: none;
    }

    .drag-handle:active {
      cursor: grabbing;
    }

    /* 底部批量操作浮动栏 */
    .bottom-bar {
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 95;
      background: rgba(26, 29, 35, 0.96);
      backdrop-filter: blur(12px);
      border: 1px solid #383e4c;
      border-radius: 12px;
      padding: 10px 18px;
      display: flex;
      align-items: center;
      gap: 14px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.55);
      flex-wrap: wrap;
    }

    .bottom-stats {
      font-size: 13.5px;
      color: var(--text-secondary);
    }

    .bottom-stats strong {
      color: var(--text-primary);
      font-weight: 700;
    }

    .divider {
      width: 1px;
      height: 18px;
      background: #323741;
    }

    /* 模态弹窗 */
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.68);
      backdrop-filter: blur(3px);
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
      border-radius: 12px;
      width: 100%;
      max-width: 500px;
      padding: 22px;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6);
    }

    .modal-title {
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 16px;
      color: var(--text-primary);
    }

    .form-group {
      margin-bottom: 14px;
    }

    .form-label {
      display: block;
      font-size: 12.5px;
      color: var(--text-secondary);
      margin-bottom: 6px;
    }

    .form-input {
      width: 100%;
      background: #131518;
      border: 1px solid #2e333d;
      color: var(--text-primary);
      padding: 8px 12px;
      border-radius: 7px;
      font-size: 13.5px;
      outline: none;
    }

    .form-input:focus {
      border-color: var(--accent);
    }

    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 20px;
    }

    /* Toast 提示 */
    .toast-container {
      position: fixed;
      bottom: 80px;
      right: 24px;
      z-index: 300;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .toast {
      background: #232730;
      border: 1px solid #3b4252;
      color: var(--text-primary);
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 13px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      animation: fadeIn 0.2s ease;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
  </style>
</head>
<body>

  <!-- 顶部导航与操作栏 -->
  <header class="top-bar">
    <div class="top-bar-inner">
      <div class="title-row">
        <div class="page-title-wrap">
          <h1 class="page-title" id="docTitleDisplay">RSS 订阅源挑选页</h1>
          <span class="sync-status" id="syncStatusBadge">🟢 实时同步 rss-lists.md</span>
        </div>
        <div class="top-actions">
          <button class="btn" onclick="openNewGroupModal()">📁 + 新建分组</button>
          <button class="btn" onclick="openFeedModal('create')">➕ 新增订阅源</button>
          <button class="btn" onclick="toggleAllVisible(true)">全选当前</button>
          <button class="btn" onclick="toggleAllVisible(false)">全不选当前</button>
          <button class="btn btn-primary" onclick="generateOpml()">
            ⚡ 生成 OPML (<span id="topCheckedCount">0</span>)
          </button>
        </div>
      </div>

      <div class="filter-row">
        <div class="search-box">
          <span class="search-icon">🔍</span>
          <input
            type="text"
            id="searchInput"
            class="search-input"
            placeholder="搜索博客名称、描述、Feed URL 或主页地址..."
            oninput="renderAll()"
          />
        </div>
        <div class="filter-pills" id="filterPills">
          <span class="pill active" data-filter="all" onclick="setFilter('all')">全部</span>
          <span class="pill" data-filter="checked" onclick="setFilter('checked')">已勾选</span>
          <span class="pill" data-filter="unchecked" onclick="setFilter('unchecked')">未勾选</span>
          <span class="pill" data-filter="active" onclick="setFilter('active')">在更</span>
          <span class="pill" data-filter="slow" onclick="setFilter('slow')">慢更</span>
          <span class="pill" data-filter="release" onclick="setFilter('release')">GitHub Release</span>
        </div>
      </div>
    </div>
  </header>

  <!-- 订阅源分组列表容器 -->
  <main class="container" id="groupsContainer"></main>

  <!-- 底部快捷批量栏 -->
  <div class="bottom-bar">
    <div class="bottom-stats">
      共 <strong id="totalGroupsCount">0</strong> 个分组，<strong id="totalFeedsCount">0</strong> 个源 · 已勾选 <strong id="bottomCheckedCount">0</strong> 项
    </div>
    <div class="divider"></div>
    <div style="display:flex;align-items:center;gap:8px;">
      <span style="font-size:12.5px;color:var(--text-secondary);">将已勾选项批量移至:</span>
      <select id="batchMoveSelect" class="move-select"></select>
      <button class="btn btn-sm" onclick="batchMoveChecked()">执行移动</button>
    </div>
    <div class="divider"></div>
    <button class="btn btn-sm" onclick="downloadFile('opml')" title="直接触发浏览器下载 .opml">⬇️ 下载 OPML</button>
    <button class="btn btn-sm" onclick="downloadFile('md')" title="直接触发浏览器下载 .md">⬇️ 下载 MD</button>
  </div>

  <!-- 新增/编辑订阅源 Modal -->
  <div class="modal-backdrop" id="feedModal">
    <div class="modal">
      <div class="modal-title" id="feedModalTitle">编辑订阅源</div>
      <input type="hidden" id="feedModalMode" value="edit" />
      <input type="hidden" id="feedModalGroupId" value="" />
      <input type="hidden" id="feedModalItemId" value="" />

      <div class="form-group">
        <label class="form-label">所属分组</label>
        <select id="feedGroupInput" class="form-input"></select>
      </div>
      <div class="form-group">
        <label class="form-label">博客名称 *</label>
        <input type="text" id="feedTitleInput" class="form-input" placeholder="例如：Tony Bai" />
      </div>
      <div class="form-group">
        <label class="form-label">简介与状态说明（包含“更新慢”/“存档”将自动生成对应标签）</label>
        <input type="text" id="feedDescInput" class="form-input" placeholder="例如：Go 语言中文社区最具影响力的个人博主之一" />
      </div>
      <div class="form-group">
        <label class="form-label">RSS Feed 地址 (xmlUrl) *</label>
        <input type="text" id="feedXmlUrlInput" class="form-input" placeholder="https://example.com/feed/" oninput="previewDerivedBlogUrl()" />
      </div>
      <div class="form-group">
        <label class="form-label">自动同步推导的博客主页 (htmlUrl，点击卡片标题时打开)</label>
        <input type="text" id="feedHtmlUrlInput" class="form-input" placeholder="留空将自动根据 Feed URL 推导" />
      </div>

      <div class="modal-actions">
        <button class="btn" onclick="closeModal('feedModal')">取消</button>
        <button class="btn btn-primary" onclick="saveFeedModal()">保存并同步 MD</button>
      </div>
    </div>
  </div>

  <!-- 新建/重命名分组 Modal -->
  <div class="modal-backdrop" id="groupModal">
    <div class="modal">
      <div class="modal-title" id="groupModalTitle">新建分组</div>
      <input type="hidden" id="groupModalMode" value="create" />
      <input type="hidden" id="groupModalId" value="" />

      <div class="form-group">
        <label class="form-label">分组（文件夹）名称 *</label>
        <input type="text" id="groupNameInput" class="form-input" placeholder="例如：老牌权威 / Infra / AI 工程" />
      </div>

      <div class="modal-actions">
        <button class="btn" onclick="closeModal('groupModal')">取消</button>
        <button class="btn btn-primary" onclick="saveGroupModal()">确认并同步 MD</button>
      </div>
    </div>
  </div>

  <div class="toast-container" id="toastContainer"></div>

  <script>
    // 初始内嵌状态数据（由 switch.py 生成时注入）
    let appState = __INITIAL_STATE_JSON__;
    let currentFilter = 'all';
    let isServerConnected = window.location.protocol.startsWith('http');
    let dragSource = null; // { groupId, itemId }

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

    function computeBadgesClient(title, desc, xmlUrl) {
      const badges = [];
      const d = desc || '';
      const u = (xmlUrl || '').toLowerCase();
      if (/存档|停更|已逝|最后一篇/.test(d)) {
        badges.push({ label: '存档', type: 'archived' });
      } else if (/更新慢|慢更|低频|偶发/.test(d)) {
        badges.push({ label: '慢更', type: 'slow' });
      } else {
        badges.push({ label: '在更', type: 'active' });
      }
      if (u.includes('releases.atom') || d.toLowerCase().includes('github 发布')) {
        badges.push({ label: 'Release', type: 'release' });
      }
      if (/需代理|blogspot/i.test(d + ' ' + u)) {
        badges.push({ label: '⚠️ 需代理', type: 'proxy' });
      }
      return badges;
    }

    function showToast(msg) {
      const container = document.getElementById('toastContainer');
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = msg;
      container.appendChild(el);
      setTimeout(() => {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.25s';
        setTimeout(() => el.remove(), 250);
      }, 2400);
    }

    function updateSyncBadge(state, text) {
      const badge = document.getElementById('syncStatusBadge');
      badge.className = 'sync-status ' + (state === 'saving' ? 'saving' : state === 'offline' ? 'offline' : '');
      badge.textContent = text;
    }

    // 实时同步结构变更回后端 rss-lists.md
    async function syncMarkdownToServer(reasonMsg) {
      // 确保所有条目的 htmlUrl 与 badges 保持最新
      for (const g of appState.groups) {
        for (const item of g.items) {
          if (!item.htmlUrl) {
            item.htmlUrl = deriveBlogUrlClient(item.xmlUrl);
          }
          item.badges = computeBadgesClient(item.title, item.desc, item.xmlUrl);
        }
      }

      if (!isServerConnected) {
        updateSyncBadge('offline', '⚪ 离线静态模式 (点击底部可下载最新 MD/OPML)');
        if (reasonMsg) showToast(`${reasonMsg}（离线模式，请点底部下载保存）`);
        return;
      }

      try {
        updateSyncBadge('saving', '⏳ 正在同步至 rss-lists.md...');
        const res = await fetch('/api/sync-md', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(appState)
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        updateSyncBadge('ok', '🟢 已同步至 rss-lists.md');
        if (reasonMsg) showToast(`✅ ${reasonMsg}（已同步至 rss-lists.md）`);
      } catch (err) {
        console.error(err);
        isServerConnected = false;
        updateSyncBadge('offline', '⚪ 本地服务未连接 (离线模式)');
        if (reasonMsg) showToast(`⚠️ 无法连接服务，已在页面内生效`);
      }
    }

    // 生成/更新 rss-subscriptions.opml
    async function generateOpml() {
      const checkedCount = getCheckedCount();
      if (checkedCount === 0) {
        if (!confirm('当前没有勾选任何订阅源，确定生成空的 OPML 吗？')) return;
      }

      if (isServerConnected) {
        try {
          const res = await fetch('/api/generate-opml', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(appState)
          });
          if (!res.ok) throw new Error('HTTP ' + res.status);
          const data = await res.json();
          showToast(`🎉 已生成并更新 rss-subscriptions.opml（共 ${data.checked_count} 个订阅源）`);
          return;
        } catch (err) {
          console.warn('回退至浏览器直接下载 OPML:', err);
        }
      }
      downloadFile('opml');
    }

    function setFilter(filterKey) {
      currentFilter = filterKey;
      document.querySelectorAll('#filterPills .pill').forEach(el => {
        el.classList.toggle('active', el.getAttribute('data-filter') === filterKey);
      });
      renderAll();
    }

    function itemMatchesFilter(item, query) {
      if (query) {
        const q = query.toLowerCase();
        const hay = `${item.title} ${item.desc} ${item.xmlUrl} ${item.htmlUrl}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (currentFilter === 'checked' && !item.checked) return false;
      if (currentFilter === 'unchecked' && item.checked) return false;
      if (currentFilter === 'active' && !item.badges.some(b => b.type === 'active')) return false;
      if (currentFilter === 'slow' && !item.badges.some(b => b.type === 'slow')) return false;
      if (currentFilter === 'release' && !item.badges.some(b => b.type === 'release')) return false;
      return true;
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

    function renderAll() {
      document.getElementById('docTitleDisplay').textContent = appState.doc_title || 'RSS 订阅源挑选页';
      const query = (document.getElementById('searchInput').value || '').trim();
      const container = document.getElementById('groupsContainer');
      container.innerHTML = '';

      let totalFeeds = 0;
      let checkedFeeds = 0;

      appState.groups.forEach((group, gIdx) => {
        totalFeeds += group.items.length;
        const groupCheckedCount = group.items.filter(i => i.checked).length;
        checkedFeeds += groupCheckedCount;

        const visibleItems = group.items.filter(item => itemMatchesFilter(item, query));
        // 如果正在搜索或筛选且本分组无匹配项，则隐藏空分组（全量视图下保留空分组以便拖入或管理）
        if ((query || currentFilter !== 'all') && visibleItems.length === 0) {
          return;
        }

        const section = document.createElement('section');
        section.className = 'group-section';
        section.dataset.groupId = group.id;

        // 支持拖拽放置到分组
        section.addEventListener('dragover', e => {
          e.preventDefault();
          section.classList.add('drag-over');
        });
        section.addEventListener('dragleave', () => {
          section.classList.remove('drag-over');
        });
        section.addEventListener('drop', e => {
          e.preventDefault();
          section.classList.remove('drag-over');
          if (dragSource && dragSource.groupId !== group.id) {
            moveFeedToGroup(dragSource.groupId, dragSource.itemId, group.id);
          }
        });

        const header = document.createElement('div');
        header.className = 'group-header';
        header.innerHTML = `
          <div class="group-title-area">
            <h2 class="group-title">${escapeHtml(group.name)}</h2>
            <span class="group-count">(${group.items.length})</span>
            <span style="font-size:12px;color:var(--text-muted);">已选 ${groupCheckedCount}/${group.items.length}</span>
          </div>
          <div class="group-actions">
            <button class="btn btn-sm" onclick="toggleGroupCheck('${group.id}', true)">全选本组</button>
            <button class="btn btn-sm" onclick="toggleGroupCheck('${group.id}', false)">全不选</button>
            <button class="btn btn-sm" onclick="openFeedModal('create', '${group.id}')">+ 添加源</button>
            <button class="btn btn-sm" onclick="openRenameGroupModal('${group.id}')">✏️ 重命名</button>
            <button class="btn btn-sm" onclick="moveGroupOrder(${gIdx}, -1)" ${gIdx === 0 ? 'disabled' : ''} title="上移分组">↑</button>
            <button class="btn btn-sm" onclick="moveGroupOrder(${gIdx}, 1)" ${gIdx === appState.groups.length - 1 ? 'disabled' : ''} title="下移分组">↓</button>
            <button class="btn btn-sm btn-danger" onclick="deleteGroup('${group.id}')">🗑️ 删除分组</button>
          </div>
        `;
        section.appendChild(header);

        const listEl = document.createElement('div');
        listEl.className = 'group-list';

        if (visibleItems.length === 0) {
          listEl.innerHTML = `<div class="empty-group-hint">当前分组暂无订阅源，可拖拽卡片至此或点击「+ 添加源」</div>`;
        } else {
          visibleItems.forEach(item => {
            const card = document.createElement('div');
            card.className = 'feed-card' + (item.checked ? ' checked' : '');
            card.draggable = true;
            card.dataset.itemId = item.id;

            card.addEventListener('dragstart', e => {
              dragSource = { groupId: group.id, itemId: item.id };
              card.classList.add('dragging');
              e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', () => {
              card.classList.remove('dragging');
              dragSource = null;
            });
            card.addEventListener('dragover', e => {
              e.preventDefault();
              e.stopPropagation();
            });
            card.addEventListener('drop', e => {
              e.preventDefault();
              e.stopPropagation();
              section.classList.remove('drag-over');
              if (dragSource && dragSource.itemId !== item.id) {
                moveFeedBeforeTarget(dragSource.groupId, dragSource.itemId, group.id, item.id);
              }
            });

            const badgesHtml = (item.badges || []).map(b => {
              const cls = b.type === 'slow' ? 'badge-slow'
                : b.type === 'archived' ? 'badge-archived'
                : b.type === 'proxy' ? 'badge-proxy'
                : b.type === 'release' ? 'badge-release'
                : 'badge-active';
              return `<span class="badge ${cls}">${escapeHtml(b.label)}</span>`;
            }).join('');

            const groupOptionsHtml = appState.groups.map(g =>
              `<option value="${g.id}" ${g.id === group.id ? 'selected' : ''}>📁 ${escapeHtml(g.name)}</option>`
            ).join('');

            const blogUrl = item.htmlUrl || deriveBlogUrlClient(item.xmlUrl);

            card.innerHTML = `
              <div class="checkbox-wrap" onclick="toggleItemCheck('${group.id}', '${item.id}')" title="勾选/取消订阅">
                <div class="custom-checkbox">
                  <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"></polyline></svg>
                </div>
              </div>
              <div class="card-body">
                <div class="card-top-line">
                  <a class="feed-title-link" href="${escapeHtml(blogUrl)}" target="_blank" rel="noopener noreferrer" title="点击在新标签页打开博客主页: ${escapeHtml(blogUrl)}">
                    <span>${escapeHtml(item.title)}</span>
                    <span class="external-icon">↗</span>
                  </a>
                  ${badgesHtml}
                </div>
                <div class="feed-desc" onclick="toggleItemCheck('${group.id}', '${item.id}')" style="cursor:pointer;">
                  ${escapeHtml(item.desc || '暂无描述')}
                </div>
                <div class="feed-urls-meta">
                  <a class="meta-link" href="${escapeHtml(blogUrl)}" target="_blank" rel="noopener noreferrer">🌐 主页: ${escapeHtml(blogUrl)}</a>
                  <a class="meta-link" href="${escapeHtml(item.xmlUrl)}" target="_blank" rel="noopener noreferrer">📡 Feed: ${escapeHtml(item.xmlUrl)}</a>
                </div>
              </div>
              <div class="card-controls">
                <select class="move-select" title="移动到其他分组" onchange="moveFeedToGroup('${group.id}', '${item.id}', this.value)">
                  ${groupOptionsHtml}
                </select>
                <button class="icon-btn" title="编辑订阅源" onclick="openFeedModal('edit', '${group.id}', '${item.id}')">✏️</button>
                <button class="icon-btn delete" title="删除该订阅源" onclick="deleteFeed('${group.id}', '${item.id}')">🗑️</button>
                <span class="drag-handle" title="拖拽移动或排序">⠿</span>
              </div>
            `;
            listEl.appendChild(card);
          });
        }

        section.appendChild(listEl);
        container.appendChild(section);
      });

      // 更新统计与批量移动下拉框
      document.getElementById('topCheckedCount').textContent = checkedFeeds;
      document.getElementById('bottomCheckedCount').textContent = checkedFeeds;
      document.getElementById('totalGroupsCount').textContent = appState.groups.length;
      document.getElementById('totalFeedsCount').textContent = totalFeeds;

      const batchSelect = document.getElementById('batchMoveSelect');
      const prevVal = batchSelect.value;
      batchSelect.innerHTML = appState.groups.map(g =>
        `<option value="${g.id}">${escapeHtml(g.name)}</option>`
      ).join('');
      if (prevVal && appState.groups.some(g => g.id === prevVal)) {
        batchSelect.value = prevVal;
      }
    }

    // 勾选操作
    function toggleItemCheck(groupId, itemId) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      const item = group.items.find(i => i.id === itemId);
      if (!item) return;
      item.checked = !item.checked;
      renderAll();
    }

    function toggleGroupCheck(groupId, checkedState) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      group.items.forEach(item => { item.checked = checkedState; });
      renderAll();
    }

    function toggleAllVisible(checkedState) {
      const query = (document.getElementById('searchInput').value || '').trim();
      appState.groups.forEach(group => {
        group.items.forEach(item => {
          if (itemMatchesFilter(item, query)) {
            item.checked = checkedState;
          }
        });
      });
      renderAll();
    }

    // 移动订阅源到另一个分组
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
      syncMarkdownToServer(`已将「${movedItem.title}」移动至分组「${toGroup.name}」`);
    }

    // 拖拽排序/跨组插入到目标卡片位置前
    function moveFeedBeforeTarget(fromGroupId, itemId, toGroupId, targetItemId) {
      const fromGroup = appState.groups.find(g => g.id === fromGroupId);
      const toGroup = appState.groups.find(g => g.id === toGroupId);
      if (!fromGroup || !toGroup) return;
      const fromIdx = fromGroup.items.findIndex(i => i.id === itemId);
      if (fromIdx === -1) return;
      const [movedItem] = fromGroup.items.splice(fromIdx, 1);
      const targetIdx = toGroup.items.findIndex(i => i.id === targetItemId);
      if (targetIdx === -1) {
        toGroup.items.push(movedItem);
      } else {
        toGroup.items.splice(targetIdx, 0, movedItem);
      }
      renderAll();
      syncMarkdownToServer(`已调整「${movedItem.title}」位置`);
    }

    // 批量移动所有已勾选项到目标分组
    function batchMoveChecked() {
      const targetGroupId = document.getElementById('batchMoveSelect').value;
      const targetGroup = appState.groups.find(g => g.id === targetGroupId);
      if (!targetGroup) return;

      let movedCount = 0;
      appState.groups.forEach(group => {
        if (group.id === targetGroupId) return;
        const remaining = [];
        group.items.forEach(item => {
          if (item.checked) {
            targetGroup.items.push(item);
            movedCount++;
          } else {
            remaining.push(item);
          }
        });
        group.items = remaining;
      });

      if (movedCount === 0) {
        showToast('没有需要移动的勾选项');
        return;
      }
      renderAll();
      syncMarkdownToServer(`已批量移动 ${movedCount} 个订阅源至「${targetGroup.name}」`);
    }

    // 删除单个订阅源
    function deleteFeed(groupId, itemId) {
      const group = appState.groups.find(g => g.id === groupId);
      if (!group) return;
      const idx = group.items.findIndex(i => i.id === itemId);
      if (idx === -1) return;
      const item = group.items[idx];
      if (!confirm(`确定要从 rss-lists.md 中删除订阅源「${item.title}」吗？`)) return;
      group.items.splice(idx, 1);
      renderAll();
      syncMarkdownToServer(`已删除订阅源「${item.title}」`);
    }

    // 分组管理：新建、重命名、上下移动、删除
    function openNewGroupModal() {
      document.getElementById('groupModalMode').value = 'create';
      document.getElementById('groupModalId').value = '';
      document.getElementById('groupModalTitle').textContent = '新建分组（文件夹）';
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
      if (!name) {
        alert('请输入分组名称');
        return;
      }
      if (mode === 'create') {
        const newGroup = {
          id: 'group_' + Date.now(),
          name: name,
          items: []
        };
        appState.groups.push(newGroup);
        closeModal('groupModal');
        renderAll();
        syncMarkdownToServer(`已创建新分组「${name}」`);
      } else {
        const groupId = document.getElementById('groupModalId').value;
        const group = appState.groups.find(g => g.id === groupId);
        if (group) {
          const oldName = group.name;
          group.name = name;
          closeModal('groupModal');
          renderAll();
          syncMarkdownToServer(`已将分组「${oldName}」重命名为「${name}」`);
        }
      }
    }

    function moveGroupOrder(index, delta) {
      const newIndex = index + delta;
      if (newIndex < 0 || newIndex >= appState.groups.length) return;
      const temp = appState.groups[index];
      appState.groups[index] = appState.groups[newIndex];
      appState.groups[newIndex] = temp;
      renderAll();
      syncMarkdownToServer(`已调整分组「${temp.name}」顺序`);
    }

    function deleteGroup(groupId) {
      const idx = appState.groups.findIndex(g => g.id === groupId);
      if (idx === -1) return;
      const group = appState.groups[idx];
      if (group.items.length > 0) {
        if (!confirm(`分组「${group.name}」下包含 ${group.items.length} 个订阅源，删除分组将同时删除这些源，确定继续吗？`)) {
          return;
        }
      } else {
        if (!confirm(`确定要删除空分组「${group.name}」吗？`)) return;
      }
      appState.groups.splice(idx, 1);
      renderAll();
      syncMarkdownToServer(`已删除分组「${group.name}」`);
    }

    // 新增/编辑订阅源 Modal
    function openFeedModal(mode, groupId, itemId) {
      if (appState.groups.length === 0) {
        alert('请先创建一个分组');
        return;
      }
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
      setTimeout(() => document.getElementById('feedTitleInput').focus(), 50);
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

      if (!title || !xmlUrl) {
        alert('请填写博客名称和 RSS Feed 地址');
        return;
      }

      const targetGroup = appState.groups.find(g => g.id === targetGroupId);
      if (!targetGroup) return;

      if (mode === 'create') {
        const newItem = {
          id: 'item_' + Date.now(),
          title,
          desc,
          xmlUrl,
          htmlUrl,
          checked: true,
          badges: computeBadgesClient(title, desc, xmlUrl)
        };
        targetGroup.items.push(newItem);
        closeModal('feedModal');
        renderAll();
        syncMarkdownToServer(`已新增订阅源「${title}」`);
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
        item.badges = computeBadgesClient(title, desc, xmlUrl);

        if (origGroupId !== targetGroupId) {
          origGroup.items.splice(idx, 1);
          targetGroup.items.push(item);
        }
        closeModal('feedModal');
        renderAll();
        syncMarkdownToServer(`已更新订阅源「${title}」`);
      }
    }

    function closeModal(id) {
      document.getElementById(id).classList.remove('open');
    }

    // 离线/浏览器直接构建下载支持
    function serializeMarkdownClient() {
      const lines = [`# ${appState.doc_title || 'FeedFlow 订阅清单'}`, '', ''];
      appState.groups.forEach((g, idx) => {
        lines.push(`## ${g.name}（${g.items.length}）`, '');
        g.items.forEach(item => {
          if (item.desc) {
            lines.push(`- **${item.title}** — ${item.desc}  `);
          } else {
            lines.push(`- **${item.title}**  `);
          }
          if (item.xmlUrl) {
            lines.push(`  ${item.xmlUrl}`);
          }
        });
        if (idx < appState.groups.length - 1) lines.push('');
      });
      return lines.join('\n') + '\n';
    }

    function escapeXmlAttrClient(val) {
      return String(val || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#x27;');
    }

    function serializeOpmlClient() {
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
        const gAttr = escapeXmlAttrClient(g.name);
        lines.push(`    <outline text="${gAttr}" title="${gAttr}">`);
        g.items.filter(i => i.checked && i.xmlUrl).forEach(item => {
          const tAttr = escapeXmlAttrClient(item.title);
          const xAttr = escapeXmlAttrClient(item.xmlUrl);
          const hAttr = escapeXmlAttrClient(item.htmlUrl || deriveBlogUrlClient(item.xmlUrl));
          lines.push(`      <outline type="rss" text="${tAttr}" title="${tAttr}" xmlUrl="${xAttr}" htmlUrl="${hAttr}"/>`);
        });
        lines.push('    </outline>');
      });
      lines.push('  </body>', '</opml>');
      return lines.join('\n') + '\n';
    }

    function downloadFile(type) {
      const isOpml = type === 'opml';
      const content = isOpml ? serializeOpmlClient() : serializeMarkdownClient();
      const filename = isOpml ? 'rss-subscriptions.opml' : 'rss-lists.md';
      const mime = isOpml ? 'text/x-opml;charset=utf-8' : 'text/markdown;charset=utf-8';
      const blob = new Blob([content], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast(`⬇️ 已下载 ${filename}`);
    }

    // 初始化渲染
    if (!isServerConnected) {
      updateSyncBadge('offline', '⚪ 离线静态模式 (运行 python switch.py 可开启自动同步)');
    }
    renderAll();
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


