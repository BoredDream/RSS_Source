#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 订阅源挑选与管理脚本 (switch.py)
- 数据源: rss-lists.md
- 订阅导出: rss-subscriptions.opml
- 静态挑选页: rss-picker.html
"""

import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
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
