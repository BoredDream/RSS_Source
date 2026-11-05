#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精简版 RSS 订阅源挑选与双向同步脚本 (switch.py)
- 数据源: rss-lists.md
- 订阅导出: rss-subscriptions.opml
- 静态挑选页: rss-picker.html
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import html
import json
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
from typing import Dict, List, Optional, Set

BASE_DIR = Path(__file__).resolve().parent
MD_FILE = BASE_DIR / "rss-lists.md"
OPML_FILE = BASE_DIR / "rss-subscriptions.opml"
HTML_FILE = BASE_DIR / "rss-picker.html"


def parse_opml(opml_path: Path = OPML_FILE) -> Set[str]:
    """读取现有 OPML 中已订阅的 xmlUrl 集合"""
    subscribed: Set[str] = set()
    if not opml_path.exists():
        return subscribed
    try:
        root = ET.parse(opml_path).getroot()
        for node in root.findall(".//outline[@xmlUrl]"):
            url = (node.attrib.get("xmlUrl") or "").strip()
            if url:
                subscribed.add(url)
    except Exception:
        pass
    return subscribed


def parse_markdown(md_path: Path = MD_FILE, opml_path: Path = OPML_FILE) -> Dict:
    """
    解析统一格式的 rss-lists.md：
    ## 分组名（数量）
    - **名称** — 描述
      Feed: https://...
      Blog: https://...
    """
    subscribed = parse_opml(opml_path)
    doc_title = "全部 RSS 订阅源"
    groups: List[Dict] = []
    cur_group: Optional[Dict] = None
    cur_item: Optional[Dict] = None
    seq = 1

    if not md_path.exists():
        return {"doc_title": doc_title, "groups": groups}

    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and not line.startswith("## "):
            doc_title = line[2:].strip()
        elif line.startswith("## "):
            name = re.sub(r"\s*[（(]\d+[）)]\s*$", "", line[3:].strip())
            cur_group = {"id": f"g_{len(groups) + 1}", "name": name, "items": []}
            groups.append(cur_group)
        elif line.startswith("- **") and cur_group is not None:
            m = re.match(r"^-\s+\*\*(.+?)\*\*\s*(?:—\s*(.*))?$", line)
            if m:
                cur_item = {
                    "id": f"i_{seq}",
                    "title": m.group(1).strip(),
                    "desc": (m.group(2) or "").strip(),
                    "xmlUrl": "",
                    "htmlUrl": "",
                    "checked": True,
                }
                seq += 1
                cur_group["items"].append(cur_item)
        elif line.startswith("Feed:") and cur_item is not None:
            xml_url = line.split(":", 1)[1].strip().strip("`")
            cur_item["xmlUrl"] = xml_url
            cur_item["checked"] = (xml_url in subscribed) if subscribed else True
        elif line.startswith(("Blog:", "站点:")) and cur_item is not None:
            cur_item["htmlUrl"] = line.split(":", 1)[1].strip().strip("`")

    return {"doc_title": doc_title, "groups": groups}


def serialize_markdown(state: Dict) -> str:
    """将分组与订阅源序列化回统一格式的 rss-lists.md"""
    title = (state.get("doc_title") or "全部 RSS 订阅源").strip()
    out: List[str] = [f"# {title}", ""]
    for g in state.get("groups") or []:
        gname = (g.get("name") or "未命名分组").strip()
        items = g.get("items") or []
        out.append(f"## {gname}（{len(items)}）")
        out.append("")
        for it in items:
            t = (it.get("title") or "").strip()
            d = (it.get("desc") or "").strip()
            f = (it.get("xmlUrl") or "").strip()
            b = (it.get("htmlUrl") or "").strip()
            out.append(f"- **{t}** — {d}" if d else f"- **{t}**")
            out.append(f"  Feed: {f}")
            out.append(f"  Blog: {b}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def save_markdown(state: Dict, md_path: Path = MD_FILE) -> None:
    md_path.write_text(serialize_markdown(state), encoding="utf-8")


def _xml_attr(val: str) -> str:
    return (
        (val or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def serialize_opml(state: Dict) -> str:
    """将已勾选 (checked=True) 的订阅源导出为 OPML 2.0"""
    groups = state.get("groups") or []
    active_names = [
        g["name"]
        for g in groups
        if any(i.get("checked") and i.get("xmlUrl") for i in g.get("items") or [])
    ]
    head_title = f"FeedFlow 订阅（{' / '.join(active_names)}）" if active_names else "FeedFlow 订阅"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        f"  <head><title>{html.escape(head_title, quote=False)}</title></head>",
        "  <body>",
    ]
    for g in groups:
        checked = [i for i in (g.get("items") or []) if i.get("checked") and i.get("xmlUrl")]
        if not checked:
            continue
        ga = _xml_attr(g.get("name", ""))
        lines.append(f'    <outline text="{ga}" title="{ga}">')
        for it in checked:
            ta = _xml_attr(it.get("title", ""))
            xa = _xml_attr(it.get("xmlUrl", ""))
            ha = _xml_attr(it.get("htmlUrl", ""))
            lines.append(
                f'      <outline type="rss" text="{ta}" title="{ta}" xmlUrl="{xa}" htmlUrl="{ha}"/>'
            )
        lines.append("    </outline>")
    lines.extend(["  </body>", "</opml>"])
    return "\n".join(lines) + "\n"


def save_opml(state: Dict, opml_path: Path = OPML_FILE) -> None:
    opml_path.write_text(serialize_opml(state), encoding="utf-8")


def check_single_url(url: str, timeout: float = 8.0) -> Dict:
    """检测单个 Blog 或 RSS URL 的可达性"""
    target = (url or "").strip()
    if not target:
        return {"ok": False, "code": "EMPTY"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
        if err.code in (206, 401, 403, 405, 406, 429):
            return {"ok": True, "code": err.code}
        return {"ok": False, "code": err.code}
    except Exception:
        return {"ok": False, "code": "ERR"}


def check_urls_batch(urls: List[str], max_workers: int = 12) -> Dict[str, Dict]:
    unique = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    if not unique:
        return {}
    results: Dict[str, Dict] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique))) as pool:
        futs = {pool.submit(check_single_url, u): u for u in unique}
        for fut, u in futs.items():
            try:
                results[u] = fut.result()
            except Exception:
                results[u] = {"ok": False, "code": "ERR"}
    return results


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>RSS 订阅源挑选</title>
  <style>
    :root {
      --bg: #121316;
      --card: #1b1d21;
      --card-checked: #1d2229;
      --border: #2c3038;
      --border-checked: rgba(59, 130, 246, 0.5);
      --text: #f1f3f5;
      --sub: #9aa0a6;
      --muted: #6e7681;
      --accent: #3b82f6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.45;
      padding-bottom: 40px;
    }
    .top-bar {
      position: sticky; top: 0; z-index: 100;
      background: #16181c; border-bottom: 1px solid var(--border);
      padding: 12px 24px;
    }
    .top-inner {
      max-width: 1800px; margin: 0 auto;
      display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    }
    .title-area { display: flex; align-items: center; gap: 10px; }
    .page-title { font-size: 17px; font-weight: 700; }
    .sync-badge {
      font-size: 12px; padding: 2px 8px; border-radius: 99px;
      background: rgba(16,185,129,0.14); color: #10b981; border: 1px solid rgba(16,185,129,0.3);
    }
    .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .input {
      background: var(--bg); border: 1px solid var(--border); color: var(--text);
      padding: 6px 10px; border-radius: 6px; font-size: 13px; outline: none;
    }
    .btn {
      background: #252830; color: var(--text); border: 1px solid #363b47;
      padding: 6px 11px; border-radius: 6px; font-size: 13px; cursor: pointer;
    }
    .btn:hover { background: #2f333d; }
    .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
    .btn-sm { padding: 3px 8px; font-size: 12px; }
    .btn-text {
      background: transparent; border: none; color: var(--muted);
      padding: 2px 5px; font-size: 12px; cursor: pointer;
    }
    .btn-text:hover { color: var(--text); }
    .container { max-width: 1800px; margin: 20px auto 0; padding: 0 24px; }
    .group-section { margin-bottom: 26px; }
    .group-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #23262d;
    }
    .group-title { font-size: 17px; font-weight: 700; }
    .group-count { font-size: 13px; color: var(--sub); margin-left: 8px; }
    .group-list {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 10px;
    }
    .feed-card {
      background: var(--card); border: 1px solid var(--border); border-radius: 8px;
      padding: 12px 14px; display: flex; align-items: flex-start; gap: 10px;
    }
    .feed-card.checked { background: var(--card-checked); border-color: var(--border-checked); }
    .feed-cb { width: 17px; height: 17px; margin-top: 3px; accent-color: var(--accent); cursor: pointer; flex-shrink: 0; }
    .card-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
    .card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .feed-name { font-size: 15px; font-weight: 700; cursor: pointer; }
    .group-select {
      background: #141619; color: var(--sub); border: 1px solid #323741;
      border-radius: 5px; padding: 2px 6px; font-size: 12px; cursor: pointer;
    }
    .feed-desc { font-size: 13px; color: var(--sub); word-break: break-word; }
    .feed-urls {
      display: flex; flex-direction: column; gap: 4px; font-size: 12px;
      margin-top: 2px; padding-top: 6px; border-top: 1px dashed #262a32;
    }
    .url-row { display: flex; align-items: center; gap: 6px; min-width: 0; }
    .url-label { color: var(--muted); font-family: monospace; font-size: 11px; width: 34px; flex-shrink: 0; }
    .reach {
      font-size: 11px; padding: 0 5px; border-radius: 4px; flex-shrink: 0;
      background: rgba(156,163,175,0.14); color: #9ca3af; border: 1px solid rgba(156,163,175,0.26);
    }
    .reach.ok { background: rgba(16,185,129,0.14); color: #10b981; border-color: rgba(16,185,129,0.3); }
    .reach.fail { background: rgba(239,68,68,0.15); color: #f87171; border-color: rgba(239,68,68,0.35); }
    .url-link {
      color: #60a5fa; text-decoration: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .url-link:hover { text-decoration: underline; }
    .modal-bg {
      position: fixed; inset: 0; background: rgba(0,0,0,0.65); z-index: 200;
      display: none; align-items: center; justify-content: center;
    }
    .modal-bg.open { display: flex; }
    .modal { background: var(--card); border: 1px solid #343944; border-radius: 10px; width: 440px; padding: 20px; }
    .form-row { margin-bottom: 10px; }
    .form-row label { display: block; font-size: 12px; color: var(--sub); margin-bottom: 4px; }
    .form-row .input { width: 100%; }
    .toast {
      position: fixed; bottom: 20px; right: 20px; z-index: 300;
      background: #232730; border: 1px solid #3b4252; padding: 8px 14px; border-radius: 6px; font-size: 13px;
    }
  </style>
</head>
<body>
  <header class="top-bar">
    <div class="top-inner">
      <div class="title-area">
        <h1 class="page-title" id="docTitle">RSS 订阅源挑选</h1>
        <span class="sync-badge" id="syncBadge">已同步 rss-lists.md</span>
      </div>
      <div class="controls">
        <input type="text" id="searchInput" class="input" placeholder="搜索名称 / 描述 / URL..." oninput="render()" />
        <button class="btn" id="checkBtn" onclick="checkAllUrls()">检测可达性</button>
        <button class="btn" onclick="addGroup()">+ 新建分组</button>
        <button class="btn" onclick="openFeedModal('create')">+ 新增源</button>
        <button class="btn" onclick="toggleAll(true)">全选</button>
        <button class="btn" onclick="toggleAll(false)">全不选</button>
        <button class="btn btn-primary" onclick="generateOpml()">生成 OPML (<span id="checkedCount">0</span>)</button>
      </div>
    </div>
  </header>

  <main class="container" id="container"></main>

  <div class="modal-bg" id="feedModal">
    <div class="modal">
      <h3 style="margin-bottom:12px;font-size:15px;" id="modalTitle">编辑订阅源</h3>
      <input type="hidden" id="mMode" /><input type="hidden" id="mGid" /><input type="hidden" id="mIid" />
      <div class="form-row"><label>分组</label><select id="mGroup" class="input"></select></div>
      <div class="form-row"><label>名称 *</label><input id="mTitle" class="input" /></div>
      <div class="form-row"><label>描述</label><input id="mDesc" class="input" /></div>
      <div class="form-row"><label>RSS URL *</label><input id="mXml" class="input" /></div>
      <div class="form-row"><label>Blog URL *</label><input id="mHtml" class="input" /></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
        <button class="btn" onclick="document.getElementById('feedModal').classList.remove('open')">取消</button>
        <button class="btn btn-primary" onclick="saveFeedModal()">保存</button>
      </div>
    </div>
  </div>

  <script>
    let appState = __INITIAL_STATE_JSON__;
    const reachMap = {};

    function esc(s) {
      return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function toast(msg) {
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = msg;
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 2200);
    }

    // 点击链接在后台新标签页打开，当前页不跳转
    function openBgTab(e, url) {
      if (e.ctrlKey || e.metaKey || e.button === 1) return;
      e.preventDefault();
      const isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform);
      const a = document.createElement('a');
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.style.display = 'none';
      document.body.appendChild(a);
      a.dispatchEvent(new MouseEvent('click', {
        bubbles: true, cancelable: true, view: window, button: 0, ctrlKey: !isMac, metaKey: isMac
      }));
      a.remove();
    }

    function reachHtml(url) {
      const st = reachMap[url];
      if (!st) return `<span class="reach" data-url="${esc(url)}">待检测</span>`;
      if (st === 'checking') return `<span class="reach" data-url="${esc(url)}">检测中</span>`;
      return st === 'ok'
        ? `<span class="reach ok" data-url="${esc(url)}">可达</span>`
        : `<span class="reach fail" data-url="${esc(url)}">不可达</span>`;
    }

    async function checkAllUrls() {
      const btn = document.getElementById('checkBtn');
      btn.disabled = true;
      const urls = [];
      appState.groups.forEach(g => g.items.forEach(i => {
        if (i.htmlUrl) urls.push(i.htmlUrl);
        if (i.xmlUrl) urls.push(i.xmlUrl);
      }));
      const unique = Array.from(new Set(urls));
      unique.forEach(u => { reachMap[u] = 'checking'; });
      render();

      const step = 16;
      for (let i = 0; i < unique.length; i += step) {
        const batch = unique.slice(i, i + step);
        btn.textContent = `检测中 (${Math.min(i + batch.length, unique.length)}/${unique.length})`;
        try {
          const res = await fetch('/api/check-urls', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: batch })
          });
          if (res.ok) {
            const { results } = await res.json();
            Object.entries(results || {}).forEach(([u, r]) => {
              reachMap[u] = r.ok ? 'ok' : 'fail';
              document.querySelectorAll(`.reach[data-url="${CSS.escape(u)}"]`).forEach(el => {
                el.className = 'reach ' + (r.ok ? 'ok' : 'fail');
                el.textContent = r.ok ? '可达' : '不可达';
              });
            });
          }
        } catch (e) {}
      }
      btn.disabled = false;
      btn.textContent = '检测可达性';
      toast('可达性检测完成');
    }

    async function syncMd(msg) {
      try {
        await fetch('/api/sync-md', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(appState)
        });
        if (msg) toast(msg);
      } catch (e) {
        toast('本地同步服务未连接');
      }
    }

    async function generateOpml() {
      try {
        const res = await fetch('/api/generate-opml', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(appState)
        });
        const data = await res.json();
        toast(`已导出 ${data.checked_count} 个订阅源至 rss-subscriptions.opml`);
      } catch (e) {
        toast('生成 OPML 失败，请确认 py switch.py 正在运行');
      }
    }

    function render() {
      document.getElementById('docTitle').textContent = appState.doc_title || 'RSS 订阅源挑选';
      const q = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      let totalChecked = 0;
      const html = [];

      appState.groups.forEach(g => {
        const gChecked = g.items.filter(i => i.checked).length;
        totalChecked += gChecked;
        const items = q
          ? g.items.filter(i => `${i.title} ${i.desc} ${i.xmlUrl} ${i.htmlUrl}`.toLowerCase().includes(q))
          : g.items;
        if (q && !items.length) return;

        const opts = appState.groups.map(og =>
          `<option value="${og.id}" ${og.id === g.id ? 'selected' : ''}>${esc(og.name)}</option>`
        ).join('');

        const cards = items.map(it => `
          <div class="feed-card${it.checked ? ' checked' : ''}">
            <input type="checkbox" class="feed-cb" ${it.checked ? 'checked' : ''} onchange="toggleItem('${g.id}','${it.id}',this)" />
            <div class="card-body">
              <div class="card-head">
                <span class="feed-name" onclick="this.closest('.feed-card').querySelector('.feed-cb').click()">${esc(it.title)}</span>
                <div>
                  <select class="group-select" onchange="moveItem('${g.id}','${it.id}',this.value)">${opts}</select>
                  <button class="btn-text" onclick="openFeedModal('edit','${g.id}','${it.id}')">编辑</button>
                  <button class="btn-text" onclick="delItem('${g.id}','${it.id}')">×</button>
                </div>
              </div>
              <div class="feed-desc">${esc(it.desc)}</div>
              <div class="feed-urls">
                <div class="url-row">
                  <span class="url-label">Blog:</span>
                  ${reachHtml(it.htmlUrl)}
                  <a class="url-link" href="${esc(it.htmlUrl)}" target="_blank" onclick="openBgTab(event,this.href)">${esc(it.htmlUrl)}</a>
                </div>
                <div class="url-row">
                  <span class="url-label">RSS:</span>
                  ${reachHtml(it.xmlUrl)}
                  <a class="url-link" href="${esc(it.xmlUrl)}" target="_blank" onclick="openBgTab(event,this.href)">${esc(it.xmlUrl)}</a>
                </div>
              </div>
            </div>
          </div>
        `).join('');

        html.push(`
          <section class="group-section">
            <div class="group-header">
              <div>
                <span class="group-title">${esc(g.name)}</span>
                <span class="group-count" id="gstat_${g.id}">(${gChecked}/${g.items.length})</span>
              </div>
              <div style="display:flex;gap:6px;">
                <button class="btn btn-sm" onclick="toggleGroup('${g.id}',true)">全选本组</button>
                <button class="btn btn-sm" onclick="toggleGroup('${g.id}',false)">全不选</button>
                <button class="btn btn-sm" onclick="renameGroup('${g.id}')">重命名</button>
                <button class="btn btn-sm" onclick="delGroup('${g.id}')">删除分组</button>
              </div>
            </div>
            <div class="group-list">${cards}</div>
          </section>
        `);
      });

      document.getElementById('container').innerHTML = html.join('');
      document.getElementById('checkedCount').textContent = totalChecked;
    }

    function toggleItem(gid, iid, cb) {
      const g = appState.groups.find(x => x.id === gid);
      const it = g && g.items.find(x => x.id === iid);
      if (!it) return;
      it.checked = cb.checked;
      cb.closest('.feed-card').classList.toggle('checked', it.checked);
      document.getElementById(`gstat_${gid}`).textContent = `(${g.items.filter(x => x.checked).length}/${g.items.length})`;
      document.getElementById('checkedCount').textContent = appState.groups.reduce((s, x) => s + x.items.filter(i => i.checked).length, 0);
    }

    function toggleGroup(gid, val) {
      const g = appState.groups.find(x => x.id === gid);
      if (g) { g.items.forEach(i => i.checked = val); render(); }
    }

    function toggleAll(val) {
      const q = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      appState.groups.forEach(g => g.items.forEach(i => {
        if (!q || `${i.title} ${i.desc} ${i.xmlUrl} ${i.htmlUrl}`.toLowerCase().includes(q)) i.checked = val;
      }));
      render();
    }

    function moveItem(fromGid, iid, toGid) {
      if (fromGid === toGid) return;
      const fg = appState.groups.find(x => x.id === fromGid);
      const tg = appState.groups.find(x => x.id === toGid);
      const idx = fg.items.findIndex(x => x.id === iid);
      if (idx > -1 && tg) {
        const [it] = fg.items.splice(idx, 1);
        tg.items.push(it);
        render();
        syncMd(`已移动至「${tg.name}」`);
      }
    }

    function delItem(gid, iid) {
      const g = appState.groups.find(x => x.id === gid);
      const idx = g ? g.items.findIndex(x => x.id === iid) : -1;
      if (idx > -1 && confirm(`确定删除「${g.items[idx].title}」？`)) {
        g.items.splice(idx, 1);
        render();
        syncMd('已删除订阅源');
      }
    }

    function addGroup() {
      const name = prompt('请输入新分组名称：');
      if (name && name.trim()) {
        appState.groups.push({ id: 'g_' + Date.now(), name: name.trim(), items: [] });
        render();
        syncMd('已新建分组');
      }
    }

    function renameGroup(gid) {
      const g = appState.groups.find(x => x.id === gid);
      const name = g && prompt('重命名分组：', g.name);
      if (name && name.trim()) {
        g.name = name.trim();
        render();
        syncMd('已重命名分组');
      }
    }

    function delGroup(gid) {
      const idx = appState.groups.findIndex(x => x.id === gid);
      if (idx > -1 && confirm(`确定删除分组「${appState.groups[idx].name}」？`)) {
        appState.groups.splice(idx, 1);
        render();
        syncMd('已删除分组');
      }
    }

    function openFeedModal(mode, gid, iid) {
      document.getElementById('mMode').value = mode;
      const sel = document.getElementById('mGroup');
      sel.innerHTML = appState.groups.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join('');
      if (mode === 'create') {
        document.getElementById('modalTitle').textContent = '新增订阅源';
        sel.value = gid || (appState.groups[0] && appState.groups[0].id);
        ['mGid','mIid','mTitle','mDesc','mXml','mHtml'].forEach(id => document.getElementById(id).value = '');
      } else {
        document.getElementById('modalTitle').textContent = '编辑订阅源';
        const g = appState.groups.find(x => x.id === gid);
        const it = g && g.items.find(x => x.id === iid);
        if (!it) return;
        sel.value = gid;
        document.getElementById('mGid').value = gid;
        document.getElementById('mIid').value = iid;
        document.getElementById('mTitle').value = it.title;
        document.getElementById('mDesc').value = it.desc;
        document.getElementById('mXml').value = it.xmlUrl;
        document.getElementById('mHtml').value = it.htmlUrl;
      }
      document.getElementById('feedModal').classList.add('open');
    }

    function saveFeedModal() {
      const mode = document.getElementById('mMode').value;
      const toGid = document.getElementById('mGroup').value;
      const title = document.getElementById('mTitle').value.trim();
      const desc = document.getElementById('mDesc').value.trim();
      const xmlUrl = document.getElementById('mXml').value.trim();
      const htmlUrl = document.getElementById('mHtml').value.trim() || xmlUrl;
      if (!title || !xmlUrl) return alert('请填写名称和 RSS URL');

      const tg = appState.groups.find(x => x.id === toGid);
      if (mode === 'create') {
        tg.items.push({ id: 'i_' + Date.now(), title, desc, xmlUrl, htmlUrl, checked: true });
      } else {
        const fg = appState.groups.find(x => x.id === document.getElementById('mGid').value);
        const idx = fg.items.findIndex(x => x.id === document.getElementById('mIid').value);
        if (idx > -1) {
          const it = Object.assign(fg.items[idx], { title, desc, xmlUrl, htmlUrl });
          if (fg.id !== tg.id) { fg.items.splice(idx, 1); tg.items.push(it); }
        }
      }
      document.getElementById('feedModal').classList.remove('open');
      render();
      syncMd('已保存并同步');
    }

    render();
    checkAllUrls();
  </script>
</body>
</html>
"""


def build_html(state: Optional[Dict] = None, html_path: Path = HTML_FILE) -> str:
    if state is None:
        state = parse_markdown()
    content = HTML_TEMPLATE.replace("__INITIAL_STATE_JSON__", json.dumps(state, ensure_ascii=False))
    html_path.write_text(content, encoding="utf-8")
    return content


class RSSRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, code: int, data: Dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path in ("/", "/index.html", "/rss-picker.html"):
            body = build_html(parse_markdown()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/check-urls":
            urls = self._read_json().get("urls") or []
            self._send_json(200, {"ok": True, "results": check_urls_batch(urls)})
        elif path == "/api/sync-md":
            state = self._read_json()
            save_markdown(state)
            build_html(state)
            self._send_json(200, {"ok": True})
        elif path == "/api/generate-opml":
            state = self._read_json()
            save_markdown(state)
            save_opml(state)
            build_html(state)
            count = sum(
                1 for g in state.get("groups", []) for i in g.get("items", []) if i.get("checked") and i.get("xmlUrl")
            )
            print(f"[OPML] 已生成并更新 {OPML_FILE.name} （共导出 {count} 个已勾选订阅源）")
            self._send_json(200, {"ok": True, "checked_count": count})
        else:
            self.send_error(404)


def main() -> None:
    parser = argparse.ArgumentParser(description="精简版 RSS 订阅源挑选与同步工具")
    parser.add_argument("--build-only", action="store_true", help="仅生成 HTML 与 OPML，不启动服务")
    parser.add_argument("--port", type=int, default=8765, help="服务端口")
    args = parser.parse_args()

    state = parse_markdown()
    build_html(state)
    if args.build_only or not parse_opml():
        save_opml(state)
    total = sum(len(g["items"]) for g in state["groups"])
    print(f"[INIT] 已解析 {MD_FILE.name}：{len(state['groups'])} 个分组，{total} 个订阅源")

    if args.build_only:
        return

    port = args.port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break
            port += 1

    server = HTTPServer(("127.0.0.1", port), RSSRequestHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"[SERVER] 已启动: {url} (按 Ctrl+C 停止)")
    threading.Timer(0.3, lambda: webbrowser.open_new_tab(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
