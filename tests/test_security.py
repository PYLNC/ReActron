"""Fernet 与 Provider credential origin 的阻断测试。"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from backend import security, services
from backend.config import AppConfig
from backend.db import Database


class SecretKeyTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.secret_file = Path(self.temporary_directory.name) / ".secret_key"
        self.secret_file_patch = patch.object(security, "SECRET_FILE", self.secret_file)
        self.secret_file_patch.start()
        self.previous_fernet = security._fernet
        security._fernet = None

    def tearDown(self):
        security._fernet = self.previous_fernet
        self.secret_file_patch.stop()
        self.temporary_directory.cleanup()

    def test_standard_environment_key_is_used_verbatim(self):
        key = Fernet.generate_key()
        with patch.dict(os.environ, {"APP_SECRET_KEY": key.decode("ascii")}, clear=False):
            self.assertEqual(security._load_key(), key)

    def test_environment_key_round_trips_across_processes(self):
        key = Fernet.generate_key().decode("ascii")
        environment = os.environ.copy()
        environment["APP_SECRET_KEY"] = key
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        repository = Path(__file__).resolve().parents[1]

        encrypted = subprocess.run(
            [
                sys.executable,
                "-c",
                "from backend.security import encrypt_text; "
                "print(encrypt_text('cross-process'))",
            ],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        decrypted = subprocess.run(
            [
                sys.executable,
                "-c",
                "from backend.security import decrypt_text; "
                f"print(decrypt_text({encrypted!r}))",
            ],
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        self.assertEqual(decrypted, "cross-process")

    def test_invalid_environment_key_never_reads_fallback(self):
        fallback_key = Fernet.generate_key()
        self.secret_file.write_bytes(fallback_key)

        with (
            patch.dict(os.environ, {"APP_SECRET_KEY": "not-a-fernet-key"}, clear=False),
            patch("backend.security.Path.read_bytes", side_effect=AssertionError("fallback read")),
        ):
            with self.assertRaisesRegex(RuntimeError, "APP_SECRET_KEY"):
                security.validate_secret_key()

        self.assertEqual(self.secret_file.read_bytes(), fallback_key)

    def test_existing_valid_key_file_is_read(self):
        key = Fernet.generate_key()
        self.secret_file.write_bytes(key)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_SECRET_KEY", None)
            self.assertEqual(security._load_key(), key)

    def test_windows_without_environment_or_key_file_fails(self):
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(security.sys, "platform", "win32"),
        ):
            os.environ.pop("APP_SECRET_KEY", None)
            with self.assertRaisesRegex(RuntimeError, "APP_SECRET_KEY"):
                security.validate_secret_key()

        self.assertFalse(self.secret_file.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_posix_first_writer_is_atomic_and_mode_0600(self):
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(security.sys, "platform", "linux"),
        ):
            os.environ.pop("APP_SECRET_KEY", None)
            with ThreadPoolExecutor(max_workers=8) as executor:
                keys = list(executor.map(lambda _: security._load_key(), range(8)))

        self.assertTrue(all(key == keys[0] for key in keys))
        self.assertEqual(stat.S_IMODE(self.secret_file.stat().st_mode), 0o600)
        self.assertEqual(list(self.secret_file.parent.iterdir()), [self.secret_file])


class BaseUrlTest(unittest.TestCase):
    def test_normalizes_idna_case_default_port_and_trailing_slash(self):
        self.assertEqual(
            services.normalize_base_url("HTTPS://BÜCHER.Example.:443/v1/"),
            "https://xn--bcher-kva.example/v1",
        )
        self.assertEqual(
            services.credential_origin("https://BÜCHER.example/v2"),
            ("https", "xn--bcher-kva.example", 443),
        )

    def test_origin_uses_effective_port_and_excludes_path(self):
        self.assertEqual(
            services.credential_origin("https://example.com/one"),
            services.credential_origin("https://EXAMPLE.com:443/two/"),
        )
        self.assertEqual(
            services.credential_origin("https://example.com:8443/v1"),
            ("https", "example.com", 8443),
        )

    def test_allows_only_loopback_http(self):
        self.assertEqual(
            services.normalize_base_url("http://127.0.0.1:8000/v1/"),
            "http://127.0.0.1:8000/v1",
        )
        self.assertEqual(
            services.credential_origin("http://[::1]/v1"),
            ("http", "::1", 80),
        )
        with self.assertRaises(ValueError):
            services.normalize_base_url("http://example.com/v1")

    def test_rejects_userinfo_query_fragment_and_invalid_port(self):
        invalid_urls = (
            "https://user:password@example.com/v1",
            "https://example.com/v1?mode=test",
            "https://example.com/v1#fragment",
            "https://example.com:",
        )
        for base_url in invalid_urls:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                services.normalize_base_url(base_url)


class RuntimeCredentialBindingTest(unittest.TestCase):
    config = AppConfig(
        api_key="environment-key",
        base_url="https://api.example/v1",
        model="model",
        max_rounds=5,
        tool_timeout=10.0,
    )

    def test_environment_key_is_not_combined_with_cross_origin_db_url(self):
        with (
            patch.object(services, "load_config", return_value=self.config),
            patch.object(
                services.db,
                "get_settings",
                return_value={"base_url": "https://other.example/v1"},
            ),
        ):
            result = services.get_runtime_config()

        self.assertEqual(result.base_url, "https://other.example/v1")
        self.assertEqual(result.api_key, "")

    def test_cross_origin_bound_key_is_not_decrypted(self):
        settings = {
            "api_key_encrypted": "ciphertext",
            "base_url": "https://other.example/v1",
            "credential_base_url": "https://api.example/v1",
        }
        with (
            patch.object(services, "load_config", return_value=self.config),
            patch.object(services.db, "get_settings", return_value=settings),
            patch.object(services, "decrypt_text") as decrypt,
        ):
            result = services.get_runtime_config()

        decrypt.assert_not_called()
        self.assertEqual(result.api_key, "")

    def test_legacy_unbound_key_is_invalid_and_does_not_fallback(self):
        settings = {
            "api_key_encrypted": "legacy-ciphertext",
            "base_url": "https://api.example/v1",
        }
        with (
            patch.object(services, "load_config", return_value=self.config),
            patch.object(services.db, "get_settings", return_value=settings),
            patch.object(services, "decrypt_text") as decrypt,
        ):
            result = services.get_runtime_config()

        decrypt.assert_not_called()
        self.assertEqual(result.api_key, "")

    def test_same_origin_path_change_reuses_environment_key(self):
        with (
            patch.object(services, "load_config", return_value=self.config),
            patch.object(
                services.db,
                "get_settings",
                return_value={"base_url": "https://API.example:443/v2/"},
            ),
        ):
            result = services.get_runtime_config()

        self.assertEqual(result.base_url, "https://api.example/v2")
        self.assertEqual(result.api_key, "environment-key")

    def test_same_origin_path_change_reuses_bound_database_key(self):
        settings = {
            "api_key_encrypted": "ciphertext",
            "base_url": "https://api.example/v2",
            "credential_base_url": "https://API.example:443/v1",
        }
        with (
            patch.object(services, "load_config", return_value=self.config),
            patch.object(services.db, "get_settings", return_value=settings),
            patch.object(services, "decrypt_text", return_value="database-key") as decrypt,
        ):
            result = services.get_runtime_config()

        decrypt.assert_called_once_with("ciphertext")
        self.assertEqual(result.api_key, "database-key")


class SettingsUpdateTest(unittest.TestCase):
    config = AppConfig(
        api_key="environment-key",
        base_url="https://api.example/v1",
        model="model",
        max_rounds=5,
        tool_timeout=10.0,
    )

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary_directory.name) / "settings.db")

    def tearDown(self):
        self.db.close()
        self.temporary_directory.cleanup()

    def _seed_bound_key(self):
        self.db.save_settings(
            api_key_encrypted="old-ciphertext",
            base_url="https://api.example/v1",
            credential_base_url="https://api.example/v1",
            model="model",
        )

    def test_cross_origin_without_new_key_leaves_entire_row_unchanged(self):
        self._seed_bound_key()
        before = self.db.get_settings()
        with (
            patch.object(services, "db", self.db),
            patch.object(services, "load_config", return_value=self.config),
            patch.object(services, "encrypt_text") as encrypt,
        ):
            with self.assertRaises(services.ProviderKeyReentryRequired):
                services.save_public_settings(
                    base_url="https://other.example/v1",
                    model="must-not-save",
                )

        encrypt.assert_not_called()
        self.assertEqual(self.db.get_settings(), before)

    def test_cross_origin_with_new_key_rebinds_all_provider_fields(self):
        self._seed_bound_key()
        with (
            patch.object(services, "db", self.db),
            patch.object(services, "load_config", return_value=self.config),
            patch.object(
                services, "encrypt_text", return_value="new-ciphertext"
            ) as encrypt,
            patch.object(services, "decrypt_text", return_value="new-key"),
        ):
            result = services.save_public_settings(
                api_key="  new-key  ",
                base_url="HTTPS://Other.Example:443/v2/",
                model="new-model",
            )

        encrypt.assert_called_once_with("new-key")
        settings = self.db.get_settings()
        self.assertEqual(settings["api_key_encrypted"], "new-ciphertext")
        self.assertEqual(settings["base_url"], "https://other.example/v2")
        self.assertEqual(
            settings["credential_base_url"], "https://other.example/v2"
        )
        self.assertEqual(settings["model"], "new-model")
        self.assertTrue(result["api_key_configured"])

    def test_same_origin_path_change_reuses_and_rebinds_existing_key(self):
        self._seed_bound_key()
        with (
            patch.object(services, "db", self.db),
            patch.object(services, "load_config", return_value=self.config),
            patch.object(services, "encrypt_text") as encrypt,
            patch.object(services, "decrypt_text", return_value="old-key"),
        ):
            services.save_public_settings(
                base_url="https://API.example:443/v2/",
            )

        encrypt.assert_not_called()
        settings = self.db.get_settings()
        self.assertEqual(settings["api_key_encrypted"], "old-ciphertext")
        self.assertEqual(settings["base_url"], "https://api.example/v2")
        self.assertEqual(
            settings["credential_base_url"], "https://api.example/v2"
        )

    def test_legacy_unbound_key_requires_reentry_before_provider_update(self):
        self.db.save_settings(
            api_key_encrypted="legacy-ciphertext",
            base_url="https://api.example/v1",
        )
        before = self.db.get_settings()
        with (
            patch.object(services, "db", self.db),
            patch.object(services, "load_config", return_value=self.config),
        ):
            with self.assertRaises(services.ProviderKeyReentryRequired):
                services.save_public_settings(base_url="https://api.example/v1")

        self.assertEqual(self.db.get_settings(), before)


if __name__ == "__main__":
    unittest.main()
