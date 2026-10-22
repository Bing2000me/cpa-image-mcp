import asyncio
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

initial = tempfile.TemporaryDirectory()
os.environ.update(CPA_API_KEY="synthetic-test-key", MCP_TOKEN="t" * 48, MCP_SECRET="s" * 48,
                  DOWNLOAD_SIGNING_KEY="d" * 64, PUBLIC_HOST="img-mcp.example.test", IMAGE_DIR=initial.name)
spec = importlib.util.spec_from_file_location("image_mcp_under_test", Path(__file__).resolve().parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


def png(size=(640, 960), transparent=False):
    im = Image.new("RGBA" if transparent else "RGB", size, (200, 100, 50, 0) if transparent else "blue")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.IMAGE_DIR = Path(self.temp.name)
        server.MANIFEST = server.IMAGE_DIR / "manifest.json"
        server.USAGE = server.IMAGE_DIR / "usage.json"
        server._lock = asyncio.Lock()

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, image_id="test-image", data=None):
        data = data if data is not None else png()
        day = server.IMAGE_DIR / "2026-10-22"
        day.mkdir(exist_ok=True)
        path = day / (image_id + ".png")
        path.write_bytes(data)
        items = server._load(server.MANIFEST, [])
        items.append({"id": image_id, "path": "2026-10-22/" + path.name, "prompt": "synthetic", "size": "640x960"})
        server._save(server.MANIFEST, items)
        return path, data

    async def deliver(self, callback, status=200, fail=False, disconnect=False, complete=True):
        async def app(scope, receive, send):
            await callback()
            if disconnect:
                await receive()
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b"synthetic image response", "more_body": not complete})

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if fail and message["type"] == "http.response.body":
                raise OSError("synthetic connection interrupted")

        await server.ImageDeliveryCleanup(app)({"type": "http"}, receive, send)

    async def test_original_is_byte_exact_without_generation_quota(self):
        path, original = self.seed()
        result = await server.get_image("test-image", delete_after_return=False)
        content = result[1].to_image_content()
        self.assertEqual(content.type, "image")
        self.assertEqual(content.mimeType, "image/png")
        self.assertEqual(base64.b64decode(content.data), original)
        self.assertIn("640×960", result[0])
        self.assertTrue(path.exists())
        self.assertFalse(server.USAGE.exists())

    async def test_resize_preserves_aspect_ratio_and_original(self):
        path, original = self.seed()
        result = await server.get_image("test-image", max_side=300)
        returned = Image.open(io.BytesIO(result[1].data))
        self.assertEqual(returned.size, (200, 300))
        self.assertEqual(path.read_bytes(), original)

    async def test_jpeg_has_correct_mime_and_white_alpha_background(self):
        self.seed(data=png(transparent=True))
        result = await server.get_image("test-image", format="jpeg", max_side=300)
        self.assertEqual(result[1].to_image_content().mimeType, "image/jpeg")
        im = Image.open(io.BytesIO(result[1].data))
        self.assertEqual(im.size, (200, 300))
        self.assertEqual(im.mode, "RGB")
        self.assertTrue(all(value >= 250 for value in im.getpixel((0, 0))))

    async def test_max_side_never_upscales(self):
        _, original = self.seed()
        result = await server.get_image("test-image", max_side=9999, delete_after_return=False)
        self.assertEqual(result[1].data, original)

    async def test_invalid_inputs_do_not_touch_original_or_quota(self):
        path, _ = self.seed()
        for max_side in (0, -3):
            with self.assertRaises(ValueError):
                await server.get_image("test-image", max_side=max_side)
        with self.assertRaises(ValueError):
            await server.get_image("test-image", format="webp")
        with self.assertRaises(ValueError):
            await server.get_image("unknown")
        with self.assertRaises(ValueError):
            await server.generate_image("synthetic", None, size="1536:1024")
        self.assertTrue(path.exists())
        self.assertFalse(server.USAGE.exists())

    async def test_generate_and_edit_forward_size_quality_and_keep_512_preview(self):
        mock = AsyncMock(return_value=png((1024, 1536)))
        with patch.object(server, "_call_cpa", mock):
            generated = await server.generate_image("a tree", None, size=" 1024X1536 ", quality="low")
            body = mock.call_args.kwargs["json_body"]
            self.assertEqual(body["size"], "1024x1536")
            self.assertEqual(body["quality"], "low")
            self.assertIn("1024 pixels wide and 1536 pixels high", body["prompt"])
            self.assertIn("portrait / vertical", body["prompt"])
            self.assertIn("2:3", body["prompt"])
            self.assertEqual(Image.open(io.BytesIO(generated[1].data)).size, (341, 512))
            item = server._load(server.MANIFEST, [])[0]
            self.assertEqual(item["prompt"], "a tree")
            self.assertEqual(item["size"], "1024x1536")
            edited = await server.edit_image(item["id"], "add a bird", None, size="1536x1024")
            data = mock.call_args.kwargs["data"]
            self.assertEqual(data["size"], "1536x1024")
            self.assertIn("landscape / horizontal", data["prompt"])
            self.assertIn("实际尺寸：1024×1536", edited[0])
            self.assertIn("实际尺寸与请求不一致", edited[0])
        self.assertEqual(server._size_prompt("unchanged", "auto"), "unchanged")

    async def test_successful_full_size_delivery_deletes_after_send(self):
        path, _ = self.seed()
        async def get():
            result = await server.get_image("test-image")
            self.assertTrue(path.exists(), "must retain source while preparing and sending response")
            self.assertIn("完整发送后将删除", result[0])
        await self.deliver(get)
        self.assertFalse(path.exists())
        self.assertEqual(server._load(server.MANIFEST, []), [])
        self.assertIsNone(server._pending_image_deletes.get())
        self.assertFalse(server.USAGE.exists())

    async def test_failed_send_retains_source(self):
        path, _ = self.seed()
        with self.assertRaises(OSError):
            await self.deliver(lambda: server.get_image("test-image"), fail=True)
        self.assertTrue(path.exists())
        self.assertEqual(len(server._load(server.MANIFEST, [])), 1)
        self.assertIsNone(server._pending_image_deletes.get())

    async def test_disconnect_partial_or_error_response_retains_source(self):
        path, _ = self.seed()
        for kwargs in ({"disconnect": True}, {"complete": False}, {"status": 500}):
            await self.deliver(lambda: server.get_image("test-image"), **kwargs)
            self.assertTrue(path.exists())

    async def test_thumbnail_and_explicit_keep_do_not_delete(self):
        path, _ = self.seed()
        await self.deliver(lambda: server.get_image("test-image", max_side=300))
        self.assertTrue(path.exists())
        await self.deliver(lambda: server.get_image("test-image", delete_after_return=False))
        self.assertTrue(path.exists())

    async def test_real_mcp_http_content_and_cleanup(self):
        path, original = self.seed()
        inner = server.mcp.streamable_http_app()
        app = server.ImageDeliveryCleanup(server.HeaderAuth(inner))
        headers = {"Authorization": "Bearer " + server.MCP_TOKEN, "Accept": "application/json, text/event-stream"}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787", headers=headers) as client:
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
                tools = next(x["result"]["tools"] for x in messages if x.get("id") == 1)
                schema = next(x for x in tools if x["name"] == "get_image")["inputSchema"]
                self.assertIn("delete_after_return", schema["properties"])
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_image", "arguments": {"image_id": "test-image"}}})
                self.assertEqual(response.status_code, 200)
                messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
                result = next(x["result"] for x in messages if x.get("id") == 2)
                self.assertFalse(result.get("isError"))
                image = next(x for x in result["content"] if x["type"] == "image")
                self.assertEqual(image["mimeType"], "image/png")
                self.assertEqual(base64.b64decode(image["data"]), original)
        self.assertFalse(path.exists(), "cleanup must propagate through real MCP task and ASGI contexts")
        self.assertEqual(server._load(server.MANIFEST, []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
