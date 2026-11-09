---
name: md-table-png
description: >
  Export Chinese markdown/HTML tables to cropped PNG screenshots. Use when
  the user asks 导出为图, 表截图, 把表做成图, PNG of tables, or shareable
  table images from a report.
---

# 中文表导出 PNG

Playwright MCP **禁止 `file://`**。不要用 GenerateImage 画数据表。

## 步骤

1. 把表写成自包含 HTML（微软雅黑，表宽约 1600px，深色表头）。大表拆成多张：给分/考核 vs 风格/性格 vs 速查 vs 反话。
2. 在 HTML 所在目录：`python -m http.server 8765 --bind 127.0.0.1`。
3. Edge 无头截图（本机常见 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`）：

```text
msedge --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=1680,2200 --screenshot=out.png http://127.0.0.1:8765/page.html
```

4. Pillow 裁掉浅底空白。桌面放中文文件名副本，方便点开。
5. 停掉 http.server。

`--window-size` 要高于内容，否则表会被裁。截完用 PIL 看 `size` 与文件体积：只有标题没有表 = HTML 切段切早了（非贪婪 `</div>` 切到内层）。
