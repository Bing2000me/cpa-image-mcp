"""Synthetic, isolated coverage for signed original downloads and image retention."""

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from PIL import Image


initial = tempfile.TemporaryDirectory()
spec = importlib.util.spec_from_file_location(
    "image_mcp_downloads_under_test", Path(__file__).resolve().parents[1] / "server.py"
)
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
with patch.dict(os.environ, {
    "CPA_API_KEY": "synthetic-cpa-key-never-used",
    "CPA_BASE": "http://127.0.0.1:1/v1",
    "MCP_TOKEN": "t" * 48,
    "MCP_SECRET": "s" * 48,
    "DOWNLOAD_SIGNING_KEY": "d" * 64,
    "DOWNLOAD_TTL_HOURS": "72",
    "IMAGE_RETENTION_DAYS": "3",
    "PUBLIC_HOST": "img-mcp.example.test",
    "IMAGE_DIR": initial.name,
}):
    spec.loader.exec_module(server)


def png():
    output = io.BytesIO()
    Image.new("RGB", (64, 96), "blue").save(output, "PNG")
    return output.getvalue()


def rewrite_url(url, *, image_id=None, **query_changes):
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    query.update(query_changes)
    path = "/download/" + image_id if image_id is not None else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server.IMAGE_DIR = Path(self.temp.name)
        server.MANIFEST = server.IMAGE_DIR / "manifest.json"
        server.USAGE = server.IMAGE_DIR / "usage.json"
        server._lock = asyncio.Lock()
        # Each test models a fresh process; the SDK session manager can run once.
        server.mcp._session_manager = None
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, image_id="synthetic-image", *, created=None, name=None, **fields):
        created = time.time() if created is None else created
        day = server.IMAGE_DIR / "2026-10-22"
        day.mkdir(exist_ok=True)
        path = day / (name or image_id + "_原始图片.png")
        path.write_bytes(png())
        os.utime(path, (created, created))
        item = {
            "id": image_id,
            "path": str(path.relative_to(server.IMAGE_DIR)),
            "created": datetime.fromtimestamp(created, timezone.utc).isoformat(),
            "prompt": "synthetic image",
            "size": "64x96",
            "parent": None,
            **fields,
        }
        items = server._load(server.MANIFEST, [])
        items.append(item)
        server._save(server.MANIFEST, items)
        return item, path

    @asynccontextmanager
    async def client(self, *, lifespan=False):
        app = server.create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://img-mcp.example.test"
        ) as client:
            if lifespan:
                async with app.router.lifespan_context(app):
                    yield client
            else:
                yield client

    async def test_config_defaults_and_independent_signed_https_url(self):
        self.assertEqual(server.DOWNLOAD_TTL_SECONDS, 72 * 3600)
        self.assertEqual(server.IMAGE_RETENTION_SECONDS, 3 * 86400)
        self.assertEqual(server.CLEANUP_INTERVAL_SECONDS, 3600)
        now = time.time()
        item, _ = self.seed(created=now)
        metadata = server._download_metadata(item, now=now)
        url = urlsplit(metadata["download_url"])
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "img-mcp.example.test")
        self.assertEqual(url.path, "/download/synthetic-image")
        query = parse_qs(url.query)
        self.assertEqual(set(query), {"expires", "token"})
        self.assertRegex(query["token"][0], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(int(query["expires"][0]), int(now + 72 * 3600))
        canonical = f"v1\n{item['id']}\n{item['path']}\n{query['expires'][0]}".encode()
        expected = base64.urlsafe_b64encode(hmac.new(b"d" * 64, canonical, hashlib.sha256).digest()).decode().rstrip("=")
        self.assertEqual(query["token"][0], expected)
        self.assertTrue(metadata["download_expires_at"])
        for secret in (server.CPA_API_KEY, server.MCP_TOKEN, server.MCP_SECRET, "d" * 64):
            self.assertNotIn(secret, metadata["download_url"])
        self.assertEqual(server._download_metadata(item, now=now), metadata)
        with patch.object(server, "DOWNLOAD_SIGNING_KEY", "e" * 64):
            self.assertNotEqual(server._download_metadata(item, now=now)["download_url"], metadata["download_url"])

    async def test_download_repeat_get_and_head_need_no_api_key(self):
        item, path = self.seed()
        url = server._download_metadata(item)["download_url"]
        async with self.client() as client:
            first = await client.get(url)
            second = await client.get(url)
            head = await client.head(url)
            unauthenticated_mcp = await client.post("/mcp")
            unauthenticated_file = await client.get("/files/2026-10-22/" + path.name)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, path.read_bytes())
        self.assertEqual(second.content, first.content)
        self.assertEqual(first.headers["content-type"].split(";")[0], "image/png")
        disposition = first.headers["content-disposition"]
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertIn("filename*=utf-8''", disposition.lower())
        from urllib.parse import unquote
        self.assertIn(path.name, unquote(disposition))
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(int(head.headers["content-length"]), len(first.content))
        self.assertEqual(unauthenticated_mcp.status_code, 401)
        self.assertEqual(unauthenticated_file.status_code, 401)
        self.assertTrue(path.exists(), "link downloads must not consume the original")

    async def test_signature_rejects_id_expiry_token_and_missing_signature(self):
        item, _ = self.seed()
        self.seed("another-image")
        url = server._download_metadata(item)["download_url"]
        expires = parse_qs(urlsplit(url).query)["expires"][0]
        invalid = [
            rewrite_url(url, image_id="another-image"),
            rewrite_url(url, expires=str(int(expires) + 1)),
            rewrite_url(url, token="0" * 64),
            rewrite_url(url, token="x"),
            rewrite_url(url, expires="not-a-number"),
            "/download/synthetic-image",
        ]
        async with self.client() as client:
            for candidate in invalid:
                with self.subTest(url=candidate):
                    self.assertEqual((await client.get(candidate)).status_code, 404)

    async def test_valid_expired_link_is_gone_without_deleting_retained_original(self):
        now = time.time()
        item, path = self.seed(created=now - 100)
        with patch.object(server, "DOWNLOAD_TTL_SECONDS", 10):
            expired_url = server._download_metadata(item, now=now - 30)["download_url"]
        async with self.client() as client:
            self.assertEqual((await client.get(expired_url)).status_code, 410)
            self.assertEqual((await client.get(rewrite_url(expired_url, token="0" * 64))).status_code, 404)
        self.assertTrue(path.exists())
        self.assertEqual(server._find(item["id"])["id"], item["id"])

    async def test_expiry_is_capped_by_remaining_retention_and_missing_file_is_404(self):
        now = time.time()
        item, path = self.seed(created=now - 2 * 86400)
        metadata = server._download_metadata(item, now=now)
        expiry = int(parse_qs(urlsplit(metadata["download_url"]).query)["expires"][0])
        self.assertEqual(expiry, int(now + 86400))
        path.unlink()
        async with self.client() as client:
            self.assertEqual((await client.get(metadata["download_url"])).status_code, 404)
        with self.assertRaises(ValueError):
            server._find(item["id"])
        self.assertNotIn(item["id"], await server.list_images())

    async def test_cleanup_removes_expired_missing_thumbnails_orphans_but_preserves_usage(self):
        now = time.time()
        old = now - server.IMAGE_RETENTION_SECONDS - 10
        keep, keep_path = self.seed("keep", created=now)
        expired, expired_path = self.seed("expired", created=old)
        missing, missing_path = self.seed("missing", created=now)
        missing_path.unlink()
        thumbnail_paths = []
        items = server._load(server.MANIFEST, [])
        for index, field in enumerate(("thumbnail_path", "thumbnail_paths", "preview_path")):
            thumb = server.IMAGE_DIR / f"thumbnail-{index}.jpg"
            thumb.write_bytes(b"synthetic thumbnail")
            thumbnail_paths.append(thumb)
            items[1][field] = [thumb.name] if field == "thumbnail_paths" else thumb.name
        missing_thumb = server.IMAGE_DIR / "missing-thumb.png"
        missing_thumb.write_bytes(png())
        items[2]["thumbnail_path"] = missing_thumb.name
        server._save(server.MANIFEST, items)
        orphan = server.IMAGE_DIR / "old-orphan.png"
        orphan.write_bytes(png())
        os.utime(orphan, (old, old))
        fresh_orphan = server.IMAGE_DIR / "fresh-orphan.png"
        fresh_orphan.write_bytes(png())
        server._save(server.USAGE, {"synthetic-day": 12})
        usage_before = server.USAGE.read_bytes()
        result = await server.cleanup_expired_images(now=now)
        self.assertIsInstance(result, dict)
        self.assertFalse(expired_path.exists())
        self.assertTrue(keep_path.exists())
        self.assertTrue(fresh_orphan.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(all(not path.exists() for path in thumbnail_paths + [missing_thumb]))
        self.assertEqual([record["id"] for record in server._load(server.MANIFEST, [])], [keep["id"]])
        self.assertEqual(server.USAGE.read_bytes(), usage_before)
        self.assertEqual(await server.cleanup_expired_images(now=now), {key: 0 for key in result})

    async def test_find_and_listing_exclude_expired_before_scheduled_cleanup(self):
        now = time.time()
        expired, path = self.seed("expired", created=now - server.IMAGE_RETENTION_SECONDS - 10)
        self.seed("visible", created=now)
        with self.assertRaises(ValueError):
            server._find(expired["id"])
        listing = await server.list_images()
        self.assertNotIn("expired", listing)
        self.assertIn("visible", listing)
        self.assertTrue(path.exists(), "listing should filter without requiring scheduled cleanup")

    async def test_cleanup_never_follows_manifest_traversal_or_symlink_outside_image_dir(self):
        now = time.time()
        old = now - server.IMAGE_RETENTION_SECONDS - 10
        with tempfile.TemporaryDirectory() as outside:
            protected = Path(outside) / "protected.png"
            protected.write_bytes(png())
            os.utime(protected, (old, old))
            symlink = server.IMAGE_DIR / "outside-link.png"
            symlink.symlink_to(protected)
            records = [
                {"id": "absolute", "path": str(protected), "created": datetime.fromtimestamp(old, timezone.utc).isoformat()},
                {"id": "traversal", "path": os.path.relpath(protected, server.IMAGE_DIR), "created": datetime.fromtimestamp(old, timezone.utc).isoformat()},
                {"id": "symlink", "path": symlink.name, "created": datetime.fromtimestamp(old, timezone.utc).isoformat()},
            ]
            item, _ = self.seed("expired-with-outside-preview", created=old, preview_path=str(protected))
            server._save(server.MANIFEST, records + [item])
            async with self.client() as client:
                for record in records:
                    try:
                        url = server._download_metadata(record, now=old)["download_url"]
                    except ValueError:
                        continue  # Rejecting an unsafe record before signing is equally safe.
                    self.assertIn((await client.get(url)).status_code, (404, 410))
            await server.cleanup_expired_images(now=now)
            self.assertEqual(protected.read_bytes(), png())
            self.assertEqual(server._load(server.MANIFEST, []), [])

    async def test_tools_and_manifest_append_metadata_without_persisting_signed_urls(self):
        with patch.object(server, "_call_cpa", AsyncMock(return_value=png())):
            generated = await server.generate_image("synthetic", None, size="auto")
            self.assertIsInstance(generated, list)
            source = server._load(server.MANIFEST, [])[0]
            edited = await server.edit_image(source["id"], "synthetic edit", None)
        retrieved = await server.get_image(source["id"], delete_after_return=False)
        listing = await server.list_images()
        for result in (generated[0], edited[0], retrieved[0], listing):
            self.assertIn("download_url", result)
            self.assertIn("download_expires_at", result)
            self.assertIn("https://img-mcp.example.test/download/", result)
        self.assertIsInstance(listing, str)
        self.assertEqual(retrieved[1].data, png())
        async with self.client() as client:
            response = await client.get("/manifest.json", headers={"Authorization": "Bearer " + server.MCP_TOKEN})
        self.assertEqual(response.status_code, 200)
        for record in response.json():
            self.assertIn("download_url", record)
            self.assertIn("download_expires_at", record)
        for record in server._load(server.MANIFEST, []):
            self.assertNotIn("download_url", record)
            self.assertNotIn("download_expires_at", record)

    async def test_lifespan_cleans_at_start_then_periodically_and_stops_on_shutdown(self):
        self.seed("expired", created=time.time() - server.IMAGE_RETENTION_SECONDS - 10)
        cleanup = AsyncMock(wraps=server.cleanup_expired_images)
        with patch.object(server, "cleanup_expired_images", cleanup), patch.object(server, "CLEANUP_INTERVAL_SECONDS", 0.01):
            async with self.client(lifespan=True):
                self.assertGreaterEqual(cleanup.await_count, 1)
                self.assertEqual(server._load(server.MANIFEST, []), [])
                async def wait_for_periodic():
                    while cleanup.await_count < 2:
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(wait_for_periodic(), timeout=2)
            after_shutdown = cleanup.await_count
            await asyncio.sleep(0.04)
            self.assertEqual(cleanup.await_count, after_shutdown)

    async def test_get_image_default_still_deletes_only_after_complete_mcp_response(self):
        item, path = self.seed()
        original_url = server._download_metadata(item)["download_url"]
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_image", "arguments": {"image_id": item["id"]}},
        }
        headers = {"Authorization": "Bearer " + server.MCP_TOKEN, "Accept": "application/json, text/event-stream"}
        async with self.client(lifespan=True) as client:
            response = await client.post("/mcp", json=payload, headers=headers)
            self.assertEqual(response.status_code, 200)
            messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
            result = next(message["result"] for message in messages if message.get("id") == 1)
            self.assertFalse(result.get("isError"))
            image = next(content for content in result["content"] if content["type"] == "image")
            self.assertEqual(base64.b64decode(image["data"]), png())
            self.assertIn("download_url", next(content["text"] for content in result["content"] if content["type"] == "text"))
            self.assertFalse(path.exists())
            self.assertEqual((await client.get(original_url)).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
