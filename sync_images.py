#!/usr/bin/env python3
"""在 Mac 上运行：把服务器上生成的原图同步到本地素材文件夹。只下载本地还没有的文件。

用法：
  export CPA_IMG_BASE="https://images.example.com"        # 用了路径暗号就带上 /<MCP_SECRET>
  export CPA_IMG_TOKEN="<MCP_TOKEN>"                     # 用了请求头鉴权才需要
  python3 sync_images.py ~/ClaudeDoSomeVideo/assets/ai-images
  python3 sync_images.py <目标文件夹> --since 2026-10-20   # 只同步某天之后的
"""
import json, os, sys, urllib.parse, urllib.request
from pathlib import Path

base = os.environ.get("CPA_IMG_BASE", "").rstrip("/")
if not base or len(sys.argv) < 2:
    sys.exit(__doc__)
dest = Path(sys.argv[1]).expanduser()
dest.mkdir(parents=True, exist_ok=True)
since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else ""

token = os.environ.get("CPA_IMG_TOKEN", "")
hdrs = {"User-Agent": "cpa-image-mcp-sync/1.0"}
if token:
    hdrs["Authorization"] = f"Bearer {token}"


def get(url, timeout):
    return urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=timeout)


with get(f"{base}/manifest.json", 30) as r:
    items = json.load(r)

new = 0
for it in items:
    if it["path"][:10] < since:
        continue
    out = dest / it["path"]
    if out.exists():
        continue
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"{base}/files/{urllib.parse.quote(it['path'])}"
    with get(url, 120) as r:
        out.write_bytes(r.read())
    new += 1
    print(f"↓ {it['path']}  {it['prompt'][:40]}")

# 顺手在本地留一份清单，方便 Claude 按 image_id 找文件、看提示词
(dest / "manifest.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
print(f"完成：新下载 {new} 张，共 {len(items)} 条记录。")
