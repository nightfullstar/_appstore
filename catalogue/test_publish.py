#!/usr/bin/env python3
"""Publish writes recasaos.org first; a GitHub failure leaves those files."""
import hashlib, json, subprocess, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLISH = HERE / "publish.sh"
ZIP_NAME = "recasaos.zip"
SHA_NAME = "recasaos.zip.sha256"
INDEX_NAME = "store-index.json"


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_artifacts(root: Path, zip_bytes=b"zip-bytes", index=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / ZIP_NAME).write_bytes(zip_bytes)
    digest = digest_of(zip_bytes)
    (root / SHA_NAME).write_text(f"{digest}  {ZIP_NAME}\n")
    payload = index if index is not None else {"schema": "recasaos.store-index.v1", "sha256": digest}
    (root / INDEX_NAME).write_text(json.dumps(payload) + "\n")
    return digest


def publish(artifacts: Path, org: Path, github="true", extra=None):
    cmd = ["bash", str(PUBLISH), "--artifacts", str(artifacts), "--org", str(org), "--github", github]
    if extra:
        cmd.extend(extra)
    return subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True)


class Publish(unittest.TestCase):
    def test_org_dest_is_written_before_github_runs(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, org = t / "artifacts", t / "org"
            digest = write_artifacts(artifacts)
            marker = t / "github.log"
            github = (
                f'test -f "{org}/store/{ZIP_NAME}" && '
                f'test -f "{org}/store/{SHA_NAME}" && '
                f'test -f "{org}/store/{INDEX_NAME}" && '
                f'test "$(cat "{org}/store/health")" = "ok" && '
                f'echo org-first > "{marker}"'
            )
            result = publish(artifacts, org, github=github)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((org / "store" / ZIP_NAME).read_bytes(), b"zip-bytes")
            self.assertEqual((org / "store" / SHA_NAME).read_text(), f"{digest}  {ZIP_NAME}\n")
            self.assertEqual(json.loads((org / "store" / INDEX_NAME).read_text())["sha256"], digest)
            self.assertEqual((org / "store" / "health").read_text(), "ok\n")
            self.assertEqual(marker.read_text(), "org-first\n")

    def test_failing_github_leaves_org_files(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, org = t / "artifacts", t / "org"
            write_artifacts(artifacts, zip_bytes=b"keep-me")
            result = publish(artifacts, org, github="echo github-failed >&2; exit 7")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("github", result.stderr.lower())
            self.assertEqual((org / "store" / ZIP_NAME).read_bytes(), b"keep-me")
            self.assertTrue((org / "store" / SHA_NAME).is_file())
            self.assertTrue((org / "store" / INDEX_NAME).is_file())
            self.assertEqual((org / "store" / "health").read_text().strip(), "ok")

    def test_health_is_ok_only_when_zip_and_index_exist(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, org = t / "artifacts", t / "org"
            write_artifacts(artifacts)
            (artifacts / INDEX_NAME).unlink()
            result = publish(artifacts, org, github="true")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((org / "store" / ZIP_NAME).is_file())
            self.assertFalse((org / "store" / INDEX_NAME).is_file())
            health = org / "store" / "health"
            self.assertTrue(not health.is_file() or health.read_text().strip() != "ok")

            write_artifacts(artifacts)
            result = publish(artifacts, org, github="true")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((org / "store" / "health").read_text(), "ok\n")


if __name__ == "__main__":
    unittest.main()
