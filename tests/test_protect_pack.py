import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from protect import pack


class ProtectPackTests(unittest.TestCase):
    def test_build_archive_contains_importable_module_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package = root / "sample"
            package.mkdir()
            (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "worker.py").write_text("RESULT = 2\n", encoding="utf-8-sig")
            namespace = root / "namespace"
            namespace.mkdir()
            (namespace / "worker.py").write_text("RESULT = 3\n", encoding="utf-8")

            archive_bytes = pack.build_archive(
                root,
                sorted([*package.glob("*.py"), *namespace.glob("*.py")]),
            )

        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))["modules"]
            self.assertTrue(manifest["sample"]["package"])
            self.assertFalse(manifest["sample.worker"]["package"])
            self.assertEqual(manifest["sample.worker"]["origin"], "/app/sample/worker.pyc")
            payload = archive.read(manifest["sample.worker"]["entry"])
            self.assertEqual(payload[:4], pack.importlib.util.MAGIC_NUMBER)
            self.assertTrue(manifest["namespace"]["namespace"])
            self.assertIsNone(manifest["namespace"]["entry"])

    def test_encrypt_archive_requires_the_same_key_and_rejects_tampering(self) -> None:
        key = bytes(range(pack.KEY_SIZE))
        encrypted = pack.encrypt_archive(b"protected payload", key)
        nonce = encrypted[len(pack.PACKAGE_MAGIC) : len(pack.PACKAGE_MAGIC) + pack.NONCE_SIZE]
        ciphertext = encrypted[len(pack.PACKAGE_MAGIC) + pack.NONCE_SIZE :]

        plaintext = AESGCM(key).decrypt(nonce, ciphertext, pack.PACKAGE_MAGIC)
        self.assertEqual(plaintext, b"protected payload")

        tampered = bytearray(ciphertext)
        tampered[0] ^= 1
        with self.assertRaises(InvalidTag):
            AESGCM(key).decrypt(nonce, bytes(tampered), pack.PACKAGE_MAGIC)

    def test_load_key_rejects_non_256_bit_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            key_path = Path(temporary_directory) / "key.bin"
            key_path.write_bytes(b"too short")
            with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
                pack.load_key(key_path)


if __name__ == "__main__":
    unittest.main()
