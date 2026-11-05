# RSS_Source

基于 Markdown (`rss-lists.md`) 维护的 RSS 订阅源清单与可视化挑选管理工具。

## 核心文件说明

- `rss-lists.md`：全量 RSS 订阅源数据源文件（共 4 个分组、183 个订阅源，保持统一的 `Feed` / `Blog` 字段格式）。
- `switch.py`：精简版解析、可达性检测与双向同步脚本（零第三方依赖，仅使用 Python 标准库）。
- `rss-picker.html`：由脚本生成的深色多列网格挑选与分组管理页面。
- `rss-subscriptions.opml`：最终导出的标准 OPML 2.0 订阅文件（可直接导入 FeedFlow 等阅读器）。

## `rss-lists.md` 标准格式

```markdown
## 分组名称（数量）

- **博客名称** — 博客描述
  Feed: https://example.com/feed.xml
  Blog: https://example.com/
```

## 快速使用

### 1. 启动交互挑选与实时同步服务（推荐）

```bash
py switch.py
```

运行后会自动：
1. 解析 `rss-lists.md` 并结合 `rss-subscriptions.opml` 同步每个订阅源的勾选状态；
2. 生成静态挑选页 `rss-picker.html`；
3. 启动本地双向同步服务并自动在浏览器打开挑选页，并发检测所有 `Blog` 与 `RSS` 地址的可达性。

在网页端可执行的操作：
- **点击 `Blog` / `RSS` 链接**：自动在**后台新标签页**打开对应网页或订阅源，当前挑选页保持焦点不跳转。
- **分组与订阅源管理**：新建/重命名/删除分组、下拉框跨组移动、新增/编辑/删除订阅源，所有变更**实时同步回写 `rss-lists.md`**。
- **导出 OPML**：勾选需要的订阅源后点击顶部 **「生成 OPML」**，直接更新项目目录下的 `rss-subscriptions.opml`。

### 2. 仅重新构建 HTML 与 OPML（不启动服务）

```bash
py switch.py --build-only
```
