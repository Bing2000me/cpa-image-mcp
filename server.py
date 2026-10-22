"""
cpa-image-mcp — 把 CPA（CLIProxyAPI）的生图接口包装成远程 MCP 服务，
供 claude.ai 的自定义连接器调用。

工具：
  generate_image  文生图
  edit_image      在已生成的图上修改（按 image_id 引用）
  get_image       获取原图或按需缩放后的完整 MCP 图片内容
  list_images     列出最近生成的图

另外提供受保护的同步路由 /manifest.json、/files/<日期>/<文件名>，
以及无需 API 请求头、使用独立过期签名的下载路由 /download/<image_id>。
"""

import asyncio
import base64
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from PIL import Image as PILImage
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

# ---------- 配置（全部来自环境变量，见 .env.example） ----------
CPA_BASE = os.environ.get("CPA_BASE", "http://127.0.0.1:8317/v1").rstrip("/")
CPA_API_KEY = os.environ["CPA_API_KEY"]
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
# 两种鉴权方式，至少开一种，也可以同时开：
#   MCP_TOKEN  —— 请求头鉴权：连接器里配置 Authorization: Bearer <token> 或 x-api-key: <token>
#   MCP_SECRET —— 路径暗号：地址写成 /<secret>/mcp（连接器看不到“Request headers”时的退路）
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")  # 例如 images.example.com
DOWNLOAD_SIGNING_KEY = os.environ.get("DOWNLOAD_SIGNING_KEY", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_HOURS", "72")) * 3600
IMAGE_RETENTION_SECONDS = int(os.environ.get("IMAGE_RETENTION_DAYS", "3")) * 86400
CLEANUP_INTERVAL_SECONDS = 3600
IMAGE_DIR = Path(os.environ.get("IMAGE_DIR", "/var/lib/cpa-image-mcp/images"))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "40"))
PREVIEW_PX = int(os.environ.get("PREVIEW_PX", "512"))
PORT = int(os.environ.get("PORT", "8787"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Shanghai"))

if not MCP_TOKEN and not MCP_SECRET:
    raise SystemExit("MCP_TOKEN 和 MCP_SECRET 至少要设置一个，否则任何人都能调用")
for _name, _val in (("MCP_TOKEN", MCP_TOKEN), ("MCP_SECRET", MCP_SECRET)):
    if _val and (len(_val) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", _val)):
        raise SystemExit(f"{_name} 至少 24 位，只能含字母、数字、- 和 _")
if len(DOWNLOAD_SIGNING_KEY) < 32:
    raise SystemExit("DOWNLOAD_SIGNING_KEY 至少 32 位，请生成独立随机密钥并写入环境变量")
if DOWNLOAD_TTL_SECONDS <= 0 or IMAGE_RETENTION_SECONDS <= 0:
    raise SystemExit("DOWNLOAD_TTL_HOURS 和 IMAGE_RETENTION_DAYS 必须为正整数")
PREFIX = f"/{MCP_SECRET}" if MCP_SECRET else ""

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST = IMAGE_DIR / "manifest.json"
USAGE = IMAGE_DIR / "usage.json"
_lock = asyncio.Lock()
_pending_image_deletes: ContextVar[list[str] | None] = ContextVar("pending_image_deletes", default=None)
_logger = logging.getLogger("cpa-image")

allowed_hosts = ["127.0.0.1:*", "localhost:*"]
if PUBLIC_HOST:
    allowed_hosts += [PUBLIC_HOST, f"{PUBLIC_HOST}:*"]

mcp = FastMCP(
    name="cpa-image",
    instructions=(
        "通过用户自己的 CPA 调用 gpt-image-2 生成图片。"
        "只用于需要画面质感的插画、场景、封面；时间线、流程图、图表等精确图形不要用它。"
        "提示词尽量具体：主体、构图、风格、光线、画幅。gpt-image-2 只支持固定尺寸，不支持透明背景。"
        "generate_image 和 edit_image 消耗用户额度，不要为了试探而重复生成。"
        "需要原图时调用 get_image；该工具不生图、不消耗生图额度。"
        "四个工具都附带无需请求头的 HTTPS 签名下载链接，可在有效期内重复下载。"
        "要保留 get_image 返回的下载链接，请设 delete_after_return=false；否则原图发送后即删除。"
    ),
    host="127.0.0.1",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=["https://claude.ai", "https://*.claude.ai"],
    ),
)


# ---------- 小工具 ----------
def _now() -> datetime:
    return datetime.now(TZ)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def _timestamp(now=None) -> float:
    if isinstance(now, datetime):
        return now.timestamp()
    return time.time() if now is None else float(now)


def _image_path(relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("图片路径必须为安全的相对路径")
    path = IMAGE_DIR / relative
    if not path.resolve().is_relative_to(IMAGE_DIR.resolve()) or path.resolve() == IMAGE_DIR.resolve():
        raise ValueError("图片路径超出 IMAGE_DIR")
    return path


def _retention_deadline(item: dict) -> float:
    try:
        created = datetime.fromisoformat(item["created"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=TZ)
        return created.timestamp() + IMAGE_RETENTION_SECONDS
    except (KeyError, TypeError, ValueError):
        try:
            return _image_path(item["path"]).stat().st_mtime + IMAGE_RETENTION_SECONDS
        except (OSError, KeyError, ValueError):
            return 0


def _available(item: dict, now=None) -> bool:
    try:
        return _timestamp(now) < _retention_deadline(item) and _image_path(item["path"]).is_file()
    except (KeyError, OSError, ValueError):
        return False


def _sign_download(item: dict, expires: int) -> str:
    message = f"v1\n{item['id']}\n{item['path']}\n{expires}".encode()
    digest = hmac.digest(DOWNLOAD_SIGNING_KEY.encode(), message, "sha256")
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _download_metadata(item: dict, now=None) -> dict:
    now = _timestamp(now)
    if not _available(item, now):
        raise ValueError("图片不存在或已超过保留期")
    if not PUBLIC_HOST:
        raise ValueError("PUBLIC_HOST 未配置，无法生成 HTTPS 下载链接")
    expires = min(int(now + DOWNLOAD_TTL_SECONDS), int(_retention_deadline(item)))
    return {
        "download_url": f"https://{PUBLIC_HOST}/download/{quote(item['id'], safe='')}?expires={expires}&token={_sign_download(item, expires)}",
        "download_expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
    }


def _download_text(item: dict) -> str:
    metadata = _download_metadata(item)
    return "\n" + "\n".join(f"{key}={value}" for key, value in metadata.items())


def _item_files(item: dict) -> list[Path]:
    relative_paths = [item.get("path"), item.get("thumbnail_path"), item.get("preview_path")]
    thumbnails = item.get("thumbnail_paths", [])
    if isinstance(thumbnails, list):
        relative_paths.extend(thumbnails)
    paths = []
    for relative in relative_paths:
        if not isinstance(relative, str):
            continue
        try:
            paths.append(_image_path(relative))
        except ValueError:
            _logger.warning("忽略不属于图片目录的路径，image_id=%s", item.get("id"))
    return list(dict.fromkeys(paths))


def _remove_item_files(item: dict) -> None:
    for path in _item_files(item):
        path.unlink(missing_ok=True)


async def cleanup_expired_images(now=None) -> dict:
    now = _timestamp(now)
    deleted_records = 0
    deleted_files = 0
    async with _lock:
        items = _load(MANIFEST, [])
        kept = []
        for item in items:
            if _available(item, now):
                kept.append(item)
                continue
            try:
                paths = _item_files(item)
                removed_files = sum(path.is_file() for path in paths)
                _remove_item_files(item)
                deleted_files += removed_files
                deleted_records += 1
            except OSError:
                kept.append(item)  # 留下记录，下个周期重试；list_images 仍会隐藏过期项。
                _logger.exception("过期图片清理失败，image_id=%s", item.get("id"))
        if kept != items:
            _save(MANIFEST, kept)
        referenced = {path for item in kept for path in _item_files(item)}
        for path in IMAGE_DIR.rglob("*"):
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"):
                continue
            try:
                safe = _image_path(str(path.relative_to(IMAGE_DIR)))
                if safe in referenced or not safe.is_file():
                    continue
                if now - safe.stat().st_mtime >= IMAGE_RETENTION_SECONDS:
                    safe.unlink()
                    deleted_files += 1
            except (OSError, ValueError):
                _logger.warning("孤立图片清理失败或路径不安全")
    return {"deleted_records": deleted_records, "deleted_files": deleted_files}


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            await cleanup_expired_images()
        except Exception:
            _logger.exception("定期图片清理失败，下个周期重试")


async def _take_quota() -> int:
    """占用一次当日额度，返回占用后的次数；超限则抛错。"""
    async with _lock:
        usage = _load(USAGE, {})
        today = _now().strftime("%Y-%m-%d")
        used = usage.get(today, 0)
        if used >= DAILY_LIMIT:
            raise RuntimeError(f"今日已生成 {used} 张，达到上限 {DAILY_LIMIT}。可在 .env 里调整 DAILY_LIMIT。")
        usage = {today: used + 1}  # 只保留当天，文件不会变大
        _save(USAGE, usage)
        return used + 1


async def _refund_quota() -> None:
    async with _lock:
        usage = _load(USAGE, {})
        today = _now().strftime("%Y-%m-%d")
        if usage.get(today, 0) > 0:
            usage[today] -= 1
            _save(USAGE, usage)


def _slug(text: str) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", text.strip()).strip("-")
    return s[:40] or "img"


def _preview(png: bytes) -> tuple[bytes, tuple[int, int]]:
    im = PILImage.open(io.BytesIO(png))
    size = im.size
    im = im.convert("RGB")
    im.thumbnail((PREVIEW_PX, PREVIEW_PX))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue(), size


def _find(image_id: str) -> dict:
    for item in _load(MANIFEST, []):
        if item["id"] == image_id and _available(item):
            return item
    raise ValueError(f"找不到 image_id={image_id}，可以先调用 list_images 查看。")


def _normalize_size(size: str) -> str:
    size = size.strip().lower()
    if size not in ("1024x1536", "1536x1024", "1024x1024", "auto"):
        raise ValueError("size 按宽x高填写：1024x1536（竖版）、1536x1024（横版）、1024x1024 或 auto")
    return size


def _size_prompt(prompt: str, size: str) -> str:
    if size == "auto":
        return prompt
    width, height = map(int, size.split("x"))
    orientation, ratio = {
        "1024x1536": ("portrait / vertical", "2:3"),
        "1536x1024": ("landscape / horizontal", "3:2"),
        "1024x1024": ("square", "1:1"),
    }[size]
    return (
        f"{prompt}\n\n"
        f"Output canvas requirement: {width} pixels wide and {height} pixels high "
        f"(width x height: {size}), {orientation} orientation, aspect ratio {ratio}. "
        "Compose the image upright for this canvas and preserve this output orientation."
    )


async def _delete_delivered_images(image_ids: list[str]) -> None:
    async with _lock:
        items = _load(MANIFEST, [])
        for image_id in dict.fromkeys(image_ids):
            item = next((item for item in items if item["id"] == image_id), None)
            if item is None:
                continue
            try:
                _remove_item_files(item)
                items = [item for item in items if item["id"] != image_id]
                _save(MANIFEST, items)
            except OSError:
                _logger.exception("原图发送后的清理失败，image_id=%s", image_id)


async def _heartbeat(ctx: Context, stop: asyncio.Event) -> None:
    """生图常要几十秒到几分钟。每 15 秒发一条日志通知，让连接保持有数据流动，
    避免 Cloudflare 因长时间无响应而断开（524）。"""
    t0 = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            try:
                await ctx.info(f"生成中… 已等待 {int(time.monotonic() - t0)} 秒")
            except Exception:
                pass


async def _call_cpa(ctx: Context, endpoint: str, *, json_body=None, files=None, data=None) -> bytes:
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(ctx, stop))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
            r = await client.post(
                f"{CPA_BASE}{endpoint}",
                headers={"Authorization": f"Bearer {CPA_API_KEY}"},
                json=json_body,
                files=files,
                data=data,
            )
    finally:
        stop.set()
        await hb
    if r.status_code != 200:
        raise RuntimeError(f"CPA 返回 {r.status_code}：{r.text[:500]}")
    item = r.json()["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):  # 少数兼容实现返回链接
        async with httpx.AsyncClient(timeout=60) as client:
            return (await client.get(item["url"])).content
    raise RuntimeError("CPA 的返回里没有图片数据")


async def _store(png: bytes, prompt: str, size: str, name_hint: str, parent: str | None) -> list:
    now = _now()
    image_id = now.strftime("%m%d-%H%M%S-") + secrets.token_hex(2)
    day = now.strftime("%Y-%m-%d")
    fname = f"{image_id}_{_slug(name_hint or prompt)}.png"
    (IMAGE_DIR / day).mkdir(exist_ok=True)
    (IMAGE_DIR / day / fname).write_bytes(png)

    preview, (w, h) = _preview(png)
    record = {
        "id": image_id,
        "path": f"{day}/{fname}",
        "prompt": prompt,
        "size": f"{w}x{h}",
        "requested_size": size,
        "parent": parent,
        "created": now.isoformat(timespec="seconds"),
    }
    async with _lock:
        items = _load(MANIFEST, [])
        items.append(record)
        _save(MANIFEST, items)

    info = (
        f"已生成 image_id={image_id}\n"
        f"请求尺寸：{size}；实际尺寸：{w}×{h}\n"
        f"原图：{record['path']}（保存在服务器上，可用 get_image 获取）\n"
        f"下面是 {PREVIEW_PX}px 预览，仅用于确认构图。"
    )
    if size != "auto" and size != f"{w}x{h}":
        info += "\n注意：CPA 返回的实际尺寸与请求不一致，图片未经旋转、裁剪或拉伸。"
    info += _download_text(record)
    return [info, Image(data=preview, format="jpeg")]


# ---------- MCP 工具 ----------
@mcp.tool()
async def generate_image(
    prompt: str,
    ctx: Context,
    size: str = "1024x1536",
    quality: str = "high",
    name_hint: str = "",
) -> list:
    """文生图。

    prompt: 详细的画面描述（主体、构图、风格、光线、色调）。中英文都可以。
    size: 1024x1536（竖版，默认，适合竖屏视频）、1536x1024（横版）、1024x1024（方形）或 auto。
    quality: low / medium / high。草图用 low 更快、更省额度。
    name_hint: 文件名里的简短说明，比如 "唐代长安西市"，方便之后在素材文件夹里找。
    返回 image_id、请求/实际尺寸和一张小预览图。调用 get_image 获取原图，或在 Mac 上同步。
    """
    size = _normalize_size(size)
    await _take_quota()
    try:
        png = await _call_cpa(
            ctx,
            "/images/generations",
            json_body={"model": IMAGE_MODEL, "prompt": _size_prompt(prompt, size), "size": size, "quality": quality, "n": 1},
        )
    except Exception:
        await _refund_quota()
        raise
    return await _store(png, prompt, size, name_hint, parent=None)


@mcp.tool()
async def edit_image(
    image_id: str,
    prompt: str,
    ctx: Context,
    size: str = "auto",
    name_hint: str = "",
) -> list:
    """在一张已生成的图上修改，比如“把天空改成黄昏”“去掉右下角的人”。

    image_id: generate_image 或 list_images 返回的 id。
    prompt: 只描述要改的地方和需要保留的地方。
    生成的是新图，原图不会被覆盖。
    """
    size = _normalize_size(size)
    src = _find(image_id)
    src_bytes = (IMAGE_DIR / src["path"]).read_bytes()
    await _take_quota()
    try:
        png = await _call_cpa(
            ctx,
            "/images/edits",
            files={"image": ("source.png", src_bytes, "image/png")},
            data={"model": IMAGE_MODEL, "prompt": _size_prompt(prompt, size), "size": size, "n": "1"},
        )
    except Exception:
        await _refund_quota()
        raise
    return await _store(png, prompt, size, name_hint or src["path"].split("_", 1)[-1][:-4], parent=image_id)


@mcp.tool()
async def get_image(
    image_id: str,
    max_side: int | None = None,
    format: Literal["png", "jpeg"] = "png",
    delete_after_return: bool = True,
) -> list:
    """通过 MCP ImageContent（base64）获取已生成的图片，无需访问原图 URL，不消耗生图额度。

    image_id: generate_image / edit_image / list_images 返回的图片 ID。
    max_side: 默认不缩放；设为正整数时按比例限制最长边，只缩小、不放大。
    format: png（默认，无损；不缩放时直接返回已有 PNG 原图字节）或 jpeg（质量 90，透明处白底）。
    delete_after_return: 默认 true；返回原尺寸图片的 HTTP 响应完整发送后，删除服务器原图及清单记录。
    缩略图始终保留原图。后续还要 edit_image 或 Mac 同步时，请设为 false。
    返回说明及真正的图片内容。大图超出客户端限制时可用 max_side 或 jpeg 控制体积。
    """
    if max_side is not None and max_side <= 0:
        raise ValueError("max_side 必须为正整数或留空")
    if format not in ("png", "jpeg"):
        raise ValueError("format 只支持 png 或 jpeg")
    src = _find(image_id)
    original = (IMAGE_DIR / src["path"]).read_bytes()
    with PILImage.open(io.BytesIO(original)) as im:
        original_size = im.size
        resize = max_side is not None and max(im.size) > max_side
        if resize:
            im.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
        returned_size = im.size
        if format == "png" and im.format == "PNG" and not resize:
            payload = original
        else:
            buf = io.BytesIO()
            if format == "jpeg":
                rgba = im.convert("RGBA")
                rgb = PILImage.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                rgb.save(buf, "JPEG", quality=90)
            else:
                im.save(buf, "PNG")
            payload = buf.getvalue()
    w, h = original_size
    rw, rh = returned_size
    pending = _pending_image_deletes.get()
    will_delete = delete_after_return and returned_size == original_size and pending is not None
    if will_delete:
        pending.append(image_id)
    info = (
        f"image_id={image_id}\n"
        f"原始实际尺寸：{w}×{h}；返回实际尺寸：{rw}×{rh}\n"
        f"格式：{format}；图片字节数：{len(payload)}。此操作不消耗生图额度。"
    )
    if will_delete:
        info += "\n此 HTTP 响应完整发送后将删除服务器原图；请保存收到的图片。删除后该 ID 不再支持获取、编辑或同步。"
    else:
        info += "\n服务器原图保留。"
    info += _download_text(src)
    if will_delete:
        info += "\n下载链接也会随原图删除失效；需要有效期内重复下载时，请使用 delete_after_return=false。"
    return [info, Image(data=payload, format=format)]


@mcp.tool()
async def list_images(limit: int = 20) -> str:
    """列出最近生成的图片（新的在前），以及今日已用额度。"""
    items = [item for item in _load(MANIFEST, []) if _available(item)][-max(1, min(limit, 100)):][::-1]
    used = _load(USAGE, {}).get(_now().strftime("%Y-%m-%d"), 0)
    lines = [f"今日已生成 {used}/{DAILY_LIMIT} 张"]
    for it in items:
        tag = f"（由 {it['parent']} 修改）" if it.get("parent") else ""
        lines.append(f"- {it['id']}  {it['size']}  {it['path']}{tag}\n  {it['prompt'][:80]}")
        lines.append(_download_text(it).lstrip("\n"))
    return "\n".join(lines)


# ---------- 给 Mac 同步脚本用的只读路由 ----------
@mcp.custom_route("/manifest.json", methods=["GET"])
async def manifest(_: Request) -> Response:
    return JSONResponse([{**item, **_download_metadata(item)} for item in _load(MANIFEST, []) if _available(item)])


@mcp.custom_route("/files/{day}/{name}", methods=["GET"])
async def files(request: Request) -> Response:
    day, name = request.path_params["day"], request.path_params["name"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or "/" in name or ".." in name:
        return Response(status_code=404)
    item = next((item for item in _load(MANIFEST, []) if item.get("path") == f"{day}/{name}"), None)
    if item is None:
        return Response(status_code=404)
    if not _available(item):
        return Response(status_code=410)
    return FileResponse(_image_path(item["path"]))


@mcp.custom_route("/download/{image_id}", methods=["GET", "HEAD"])
async def download_image(request: Request) -> Response:
    image_id = request.path_params["image_id"]
    item = next((item for item in _load(MANIFEST, []) if item.get("id") == image_id), None)
    expires = request.query_params.get("expires", "")
    token = request.query_params.get("token", "")
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if item is None or not re.fullmatch(r"[0-9]{1,12}", expires) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return Response(status_code=404, headers=headers)
    expires = int(expires)
    if not hmac.compare_digest(token, _sign_download(item, expires)):
        return Response(status_code=404, headers=headers)
    if time.time() >= expires or time.time() >= _retention_deadline(item):
        return Response(status_code=410, headers=headers)
    try:
        path = _image_path(item["path"])
        if not path.is_file():
            return Response(status_code=404, headers=headers)
        with PILImage.open(path) as image:
            media_type = PILImage.MIME.get(image.format, "application/octet-stream")
    except (OSError, ValueError):
        return Response(status_code=404, headers=headers)
    return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="attachment", headers=headers)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> Response:
    return Response("ok")


# ---------- 请求头鉴权 ----------
class HeaderAuth:
    """请求头和路径暗号独立鉴权；除 /healthz 外至少通过其中一种。

    请求头：Authorization: Bearer <token> 或 x-api-key: <token>。
    路径暗号：/<MCP_SECRET>/...，验证后移除前缀再交给应用路由。

    401 里刻意不带 WWW-Authenticate 和 OAuth 元数据：本服务不做 OAuth，
    不给 Claude 任何“去走 OAuth 流程”的暗示。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        anonymous_download = scope.get("method") in ("GET", "HEAD") and scope.get("path", "").startswith("/download/")
        if scope["type"] == "http" and scope["path"] != "/healthz" and not anonymous_download:
            path_parts = scope["path"].split("/", 2)
            path_auth = bool(MCP_SECRET) and hmac.compare_digest(
                path_parts[1].encode(), MCP_SECRET.encode()
            )
            if path_auth:
                scope = dict(scope)
                scope["path"] = "/" + path_parts[2] if len(path_parts) > 2 else "/"
                scope.pop("raw_path", None)
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            auth = headers.get("authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
            header_auth = bool(MCP_TOKEN) and hmac.compare_digest(given.encode(), MCP_TOKEN.encode())
            if not path_auth and not header_auth:
                await Response("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ImageDeliveryCleanup:
    """仅在成功的 HTTP 响应完整发送后，执行本次取图登记的删除；中断或发送失败时保留。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        pending: list[str] = []
        token = _pending_image_deletes.set(pending)
        status = None
        complete = False
        disconnected = False

        async def checked_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not complete:
                disconnected = True
            return message

        async def checked_send(message):
            nonlocal status, complete
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, checked_receive, checked_send)
            if status == 200 and complete and not disconnected and pending:
                await _delete_delivered_images(pending)
        finally:
            _pending_image_deletes.reset(token)


def create_app():
    app = mcp.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await cleanup_expired_images()
        async with original_lifespan(application) as state:
            cleanup_task = asyncio.create_task(_cleanup_loop(), name="image-retention-cleanup")
            try:
                yield state
            finally:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task

    app.router.lifespan_context = lifespan
    app.add_middleware(HeaderAuth)
    app.add_middleware(ImageDeliveryCleanup)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="info", access_log=False)
