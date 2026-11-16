#!/usr/bin/env python3
"""Publish writes recasaos.org first; a GitHub failure leaves those files."""
import hashlib, io, json, os, subprocess, tempfile, unittest, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLISH = HERE / "publish.sh"
ZIP_NAME = "recasaos.zip"
SHA_NAME = "recasaos.zip.sha256"
INDEX_NAME = "store-index.json"
ORG_ZIP_URL = "https://recasaos.org/store/recasaos.zip"
GITHUB_ZIP_URL = "https://github.com/ReCasaOS/_appstore/releases/latest/download/recasaos.zip"
CATALOGUE_BRANCH_ZIP_URL = "https://raw.githubusercontent.com/ReCasaOS/_appstore/catalogue/store/recasaos.zip"
RELEASE_TAG = "catalogue-latest"


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def catalogue_zip(apps=1):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("catalogue.json", json.dumps({"apps": [{"id": f"a{i}"} for i in range(apps)]}))
    return buf.getvalue()


def write_artifacts(root: Path, zip_bytes=b"zip-bytes", index=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / ZIP_NAME).write_bytes(zip_bytes)
    digest = digest_of(zip_bytes)
    (root / SHA_NAME).write_text(f"{digest}  {ZIP_NAME}\n")
    payload = index if index is not None else {"schema": "recasaos.store-index.v1", "sha256": digest}
    (root / INDEX_NAME).write_text(json.dumps(payload) + "\n")
    return digest


def write_stub(bin_dir: Path, name: str, body: str):
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def curl_stub_body():
    return r"""
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--output) out="$2"; shift 2 ;;
    -H|--header|-A|--user-agent|--connect-timeout|--max-time|-m) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
src=""
case "$url" in
  *://recasaos.org/store/recasaos.zip) src="${STUB_ORG_ZIP:-}" ;;
  */releases/latest/download/recasaos.zip) src="${STUB_GITHUB_ZIP:-}" ;;
  */catalogue/store/recasaos.zip) src="${STUB_CATALOGUE_ZIP:-}" ;;
  *) echo "curl stub: unknown url $url" >&2; exit 1 ;;
esac
[ -n "$src" ] && [ -f "$src" ] || exit 22
[ -n "$out" ] || exit 1
cp "$src" "$out"
"""


def gh_stub_body(fail_release=False):
    fail = "echo gh release failed >&2; exit 1" if fail_release else "exit 0"
    return f"""
echo "gh $*" >> "${{STUB_LOG}}"
if [ "$1" = "release" ] && [ "$2" = "view" ]; then
  exit 1
fi
{fail}
"""


def git_stub_body(fail_push=False):
    push = "echo git push failed >&2; exit 1" if fail_push else "exit 0"
    return f"""
echo "git $*" >> "${{STUB_LOG}}"
if [ "$1" = "push" ]; then
  {push}
fi
case "$1" in
  hash-object) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  write-tree) echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  commit-tree) echo cccccccccccccccccccccccccccccccccccccccc ;;
esac
exit 0
"""


def publish(artifacts: Path, org: Path, github="true", extra=None, env=None, skip_github_flag=False):
    cmd = ["bash", str(PUBLISH), "--artifacts", str(artifacts), "--org", str(org)]
    if not skip_github_flag:
        cmd.extend(["--github", github])
    if extra:
        cmd.extend(extra)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, env=run_env)


def publish_with_stubs(artifacts: Path, org: Path, bin_dir: Path, env=None, extra=None):
    run_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    if env:
        run_env.update(env)
    return publish(artifacts, org, skip_github_flag=True, extra=extra, env=run_env)


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

    def test_github_release_failure_leaves_org_files_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, org, bin_dir = t / "artifacts", t / "org", t / "bin"
            write_artifacts(artifacts, zip_bytes=catalogue_zip())
            log = t / "stub.log"
            write_stub(bin_dir, "gh", gh_stub_body(fail_release=True))
            write_stub(bin_dir, "git", git_stub_body(fail_push=False))
            result = publish_with_stubs(artifacts, org, bin_dir, env={"STUB_LOG": str(log)})
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("github publish failed", result.stderr.lower())
            self.assertEqual((org / "store" / ZIP_NAME).read_bytes(), catalogue_zip())
            self.assertTrue((org / "store" / SHA_NAME).is_file())
            self.assertTrue((org / "store" / INDEX_NAME).is_file())
            self.assertEqual((org / "store" / "health").read_text().strip(), "ok")
            logged = log.read_text() if log.is_file() else ""
            self.assertRegex(logged, rf"release (create|upload) {RELEASE_TAG}(\s|$)")
            self.assertNotRegex(logged, r"release (create|upload|view) catalogue(\s|$)")

    def test_github_branch_push_failure_leaves_org_files_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, org, bin_dir = t / "artifacts", t / "org", t / "bin"
            write_artifacts(artifacts, zip_bytes=catalogue_zip())
            log = t / "stub.log"
            write_stub(bin_dir, "gh", gh_stub_body(fail_release=False))
            write_stub(bin_dir, "git", git_stub_body(fail_push=True))
            result = publish_with_stubs(artifacts, org, bin_dir, env={"STUB_LOG": str(log)})
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("github publish failed", result.stderr.lower())
            self.assertEqual((org / "store" / ZIP_NAME).read_bytes(), catalogue_zip())
            self.assertTrue((org / "store" / SHA_NAME).is_file())
            self.assertTrue((org / "store" / INDEX_NAME).is_file())
            self.assertEqual((org / "store" / "health").read_text().strip(), "ok")
            logged = log.read_text() if log.is_file() else ""
            self.assertIn("refs/heads/catalogue", logged)
            self.assertRegex(logged, rf"--latest|release create {RELEASE_TAG}")
            self.assertNotRegex(logged, r"release (create|upload|view) catalogue(\s|$)")

    def test_fetch_previous_prefers_org_then_github_then_branch(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            bin_dir = t / "bin"
            write_stub(bin_dir, "curl", curl_stub_body())
            org_zip, gh_zip, br_zip = t / "org.zip", t / "gh.zip", t / "br.zip"
            dest = t / "prev.zip"
            org_zip.write_bytes(b"from-org")
            gh_zip.write_bytes(b"from-github")
            br_zip.write_bytes(b"from-branch")
            env = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "STUB_ORG_ZIP": str(org_zip),
                "STUB_GITHUB_ZIP": str(gh_zip),
                "STUB_CATALOGUE_ZIP": str(br_zip),
            }
            result = subprocess.run(
                ["bash", str(PUBLISH), "--fetch-previous", str(dest)],
                cwd=str(HERE), capture_output=True, text=True, env={**os.environ, **env},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dest.read_bytes(), b"from-org")

            dest.unlink()
            org_zip.unlink()
            result = subprocess.run(
                ["bash", str(PUBLISH), "--fetch-previous", str(dest)],
                cwd=str(HERE), capture_output=True, text=True, env={**os.environ, **env},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dest.read_bytes(), b"from-github")

            dest.unlink()
            gh_zip.unlink()
            result = subprocess.run(
                ["bash", str(PUBLISH), "--fetch-previous", str(dest)],
                cwd=str(HERE), capture_output=True, text=True, env={**os.environ, **env},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dest.read_bytes(), b"from-branch")

    def test_skip_only_when_org_and_github_both_match(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            artifacts, bin_dir = t / "artifacts", t / "bin"
            write_artifacts(artifacts, zip_bytes=b"new-zip")
            write_stub(bin_dir, "curl", curl_stub_body())
            org_zip, gh_zip = t / "org.zip", t / "gh.zip"
            env = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "STUB_ORG_ZIP": str(org_zip),
                "STUB_GITHUB_ZIP": str(gh_zip),
            }

            def skip_check():
                return subprocess.run(
                    ["bash", str(PUBLISH), "--skip-if-published", "--artifacts", str(artifacts)],
                    cwd=str(HERE), capture_output=True, text=True, env={**os.environ, **env},
                )

            org_zip.write_bytes(b"new-zip")
            gh_zip.write_bytes(b"stale-github")
            result = skip_check()
            self.assertNotEqual(result.returncode, 0, "org match alone must not skip a GitHub retry")

            gh_zip.write_bytes(b"new-zip")
            result = skip_check()
            self.assertEqual(result.returncode, 0, result.stderr)

            gh_zip.unlink()
            result = skip_check()
            self.assertNotEqual(result.returncode, 0, "missing GitHub zip must not skip")


if __name__ == "__main__":
    unittest.main()
