"""Configuration helper tests use stdlib and synthetic values only."""

import contextlib
import importlib.util
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("configuration_under_test", Path(__file__).resolve().parents[1] / "scripts" / "configure_env.py")
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


TEMPLATE = """# Synthetic template
CPA_API_KEY=replace-me
PUBLIC_HOST=images.example.com
MCP_TOKEN=
MCP_SECRET=
DOWNLOAD_SIGNING_KEY=
DOWNLOAD_TTL_HOURS=72
IMAGE_RETENTION_DAYS=3
PORT=8787
"""


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.template = self.directory / ".env.example"
        self.template.write_text(TEMPLATE, encoding="utf-8")
        self.env = self.directory / "private.env"

    def tearDown(self):
        self.temp.cleanup()

    def valid(self, **changes):
        values = {
            "CPA_API_KEY": "sk-synthetic-only-0123456789",
            "PUBLIC_HOST": "img-mcp.synthetictest.net",
            "MCP_TOKEN": "t" * 48,
            "MCP_SECRET": "",
            "DOWNLOAD_SIGNING_KEY": "d" * 64,
            "DOWNLOAD_TTL_HOURS": "72",
            "IMAGE_RETENTION_DAYS": "3",
            "PORT": "8787",
            **changes,
        }
        self.env.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
        return values

    def test_first_prepare_generates_private_independent_credentials_and_defaults(self):
        self.template.write_text(TEMPLATE.replace("DOWNLOAD_TTL_HOURS=72", "DOWNLOAD_TTL_HOURS=").replace("IMAGE_RETENTION_DAYS=3", "IMAGE_RETENTION_DAYS="), encoding="utf-8")
        result = configuration.prepare_env(self.template, self.env)
        values = configuration.parse_env(self.env)
        self.assertTrue(result["created"])
        self.assertTrue(result["changed"])
        self.assertIsNone(result["backup"])
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertRegex(values["MCP_TOKEN"], r"^[0-9a-f]{64}$")
        self.assertRegex(values["DOWNLOAD_SIGNING_KEY"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(values["MCP_TOKEN"], values["DOWNLOAD_SIGNING_KEY"])
        self.assertEqual(values["MCP_SECRET"], "")
        self.assertEqual(values["DOWNLOAD_TTL_HOURS"], "72")
        self.assertEqual(values["IMAGE_RETENTION_DAYS"], "3")
        self.assertIn("# Synthetic template", self.env.read_text())

    def test_prepare_preserves_existing_credentials_and_is_byte_and_mtime_idempotent(self):
        original_values = self.valid(MCP_SECRET="s" * 48)
        original = self.env.read_bytes()
        original_mtime = self.env.stat().st_mtime_ns
        for _ in range(2):
            result = configuration.prepare_env(self.template, self.env)
            self.assertFalse(result["created"])
            self.assertFalse(result["changed"])
            self.assertIsNone(result["backup"])
        self.assertEqual(self.env.read_bytes(), original)
        self.assertEqual(self.env.stat().st_mtime_ns, original_mtime)
        self.assertEqual(configuration.parse_env(self.env), original_values)
        self.assertEqual(list(self.directory.glob("private.env.bak-*")), [])

    def test_existing_secret_only_does_not_generate_token(self):
        self.valid(MCP_TOKEN="", MCP_SECRET="s" * 48)
        result = configuration.prepare_env(self.template, self.env)
        self.assertFalse(result["changed"])
        self.assertEqual(configuration.parse_env(self.env)["MCP_TOKEN"], "")
        configuration.validate_env(self.env)

    def test_changed_existing_file_has_exact_private_backup_and_only_one_backup(self):
        self.valid(DOWNLOAD_SIGNING_KEY="", DOWNLOAD_TTL_HOURS="")
        original = self.env.read_bytes()
        result = configuration.prepare_env(self.template, self.env)
        backup = Path(result["backup"])
        self.assertRegex(backup.name, r"private\.env\.bak-\d{8}T\d{12}Z$")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(configuration.parse_env(self.env)["MCP_TOKEN"], "t" * 48)
        configuration.prepare_env(self.template, self.env)
        self.assertEqual(len(list(self.directory.glob("private.env.bak-*"))), 1)

    def test_parser_handles_quotes_comments_and_exports_without_expansion(self):
        self.env.write_text("export A='two words # literal'\nB=\"$HOME $(never-execute)\" # comment\nC=value # comment\nEMPTY=\nD='it'\\''s'\n", encoding="utf-8")
        self.assertEqual(configuration.parse_env(self.env), {
            "A": "two words # literal", "B": "$HOME $(never-execute)",
            "C": "value", "EMPTY": "", "D": "it's",
        })

    def test_shell_text_is_never_executed_and_invalid_syntax_does_not_echo_values(self):
        marker = self.directory / "should-not-exist"
        self.env.write_text(f"ATTACK='$(touch {marker})'\n", encoding="utf-8")
        self.assertIn("$(touch", configuration.parse_env(self.env)["ATTACK"])
        self.assertFalse(marker.exists())
        for text in (f"ATTACK=$(touch {marker})\n", "SECRET='unclosed-sensitive-data\n", "sensitive-bare-command\n"):
            self.env.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                configuration.parse_env(self.env)
            self.assertNotIn(str(marker), str(error.exception))
            self.assertNotIn("sensitive", str(error.exception))
        self.assertFalse(marker.exists())

    def test_validate_rejects_each_invalid_field_without_leaking_secrets(self):
        cases = {
            "CPA_API_KEY": ["", "replace-with-private-secret", "your-private-secret"],
            "PUBLIC_HOST": ["images.example.com", "https://valid.net", "valid.net/path", "valid.net:8787", "-bad.net", "localhost"],
            "MCP_TOKEN": ["short", "x" * 24 + "!"],
            "DOWNLOAD_SIGNING_KEY": ["short", "replace-with-private-secret" * 3],
            "DOWNLOAD_TTL_HOURS": ["0", "-1", "1.5", "NaN"],
            "IMAGE_RETENTION_DAYS": ["0", "-1", "1.5", "NaN"],
            "PORT": ["0", "65536", "-1", "NaN"],
        }
        for field, invalid_values in cases.items():
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    self.valid(**{field: value})
                    with self.assertRaises(ValueError) as error:
                        configuration.validate_env(self.env)
                    self.assertIn(field, str(error.exception))
                    self.assertNotIn("private-secret", str(error.exception))
                    self.assertNotIn("t" * 48, str(error.exception))
                    self.assertNotIn("d" * 64, str(error.exception))
        self.valid(MCP_TOKEN="", MCP_SECRET="")
        with self.assertRaisesRegex(ValueError, "MCP_TOKEN, MCP_SECRET"):
            configuration.validate_env(self.env)

    def test_valid_quoted_key_and_domain_port_boundaries(self):
        self.valid(CPA_API_KEY="'synthetic key with spaces'", PUBLIC_HOST="img-mcp.synthetictest.net", PORT="65535")
        self.assertEqual(configuration.validate_env(self.env)["CPA_API_KEY"], "synthetic key with spaces")
        self.valid(PORT="1")
        configuration.validate_env(self.env)

    def test_cli_messages_contain_status_and_field_names_but_no_credentials(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(configuration.main(["prepare", "--template", str(self.template), "--env-file", str(self.env)]), 0)
        values = configuration.parse_env(self.env)
        self.assertIn("Status: created", output.getvalue())
        self.assertIn("CPA_API_KEY", output.getvalue())
        self.assertIn("PUBLIC_HOST", output.getvalue())
        for key in ("MCP_TOKEN", "DOWNLOAD_SIGNING_KEY"):
            self.assertNotIn(values[key], output.getvalue() + errors.getvalue())
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(configuration.main(["validate", "--env-file", str(self.env)]), 1)
        self.assertNotIn("replace-me", errors.getvalue())
        self.assertIn("CPA_API_KEY", errors.getvalue())
        self.valid()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configuration.main(["validate", "--env-file", str(self.env)]), 0)

    def test_duplicate_assignments_and_symlink_target_are_rejected_safely(self):
        self.env.write_text("MCP_TOKEN=one-secret\nMCP_TOKEN=another-secret\n", encoding="utf-8")
        with self.assertRaises(ValueError) as error:
            configuration.parse_env(self.env)
        self.assertNotIn("one-secret", str(error.exception))
        target = self.directory / "target"
        target.write_text("preserve")
        self.env.unlink()
        self.env.symlink_to(target)
        with self.assertRaises(ValueError):
            configuration.prepare_env(self.template, self.env)
        self.assertEqual(target.read_text(), "preserve")

    def test_health_retries_and_success_prints_only_ok(self):
        self.valid()
        opener = unittest.mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b"ok"
        output = io.StringIO()
        with patch.object(configuration.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stdout(output):
            self.assertTrue(configuration.health(self.env))
        self.assertEqual(output.getvalue(), "ok\n")
        self.assertEqual(opener.open.call_args.args[0], "http://127.0.0.1:8787/healthz")
        opener.open.side_effect = OSError("synthetic secret must not be logged")
        with patch.object(configuration.urllib.request, "build_opener", return_value=opener), patch.object(configuration.time, "monotonic", side_effect=[0, 0, 0, 11, 11]), patch.object(configuration.time, "sleep"):
            self.assertFalse(configuration.health(self.env))


if __name__ == "__main__":
    unittest.main(verbosity=2)
