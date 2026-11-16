import hashlib, http.server, json, sys, tempfile, threading, unittest, zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build

SOURCES = [{"id": i, "name": i, "url": "u", "licence": "MIT"} for i in ("a", "b", "c")]
PNG = b"\x89PNG\r\n\x1a\n" + b"icon"
ORG_ZIP = "https://recasaos.org/store/recasaos.zip"
GITHUB_ZIP = "https://github.com/ReCasaOS/_appstore/releases/latest/download/recasaos.zip"


def compose(name, image="nginx:1", category="Media", extra="", icon=None, screenshots=None):
    body = f"name: {name}\nservices:\n  {name}:\n    image: {image}\n{extra}x-casaos:\n  main: {name}\n  category: {category}\n"
    if icon is not None:
        body += f"  icon: {icon}\n"
    if screenshots is not None:
        body += "  screenshot_link:\n" + "".join(f"    - {s}\n" for s in screenshots)
    return body.encode()


def src(*apps, categories=None, recommend=None):
    out = {"apps": {n: compose(n, **kw) for n, kw in apps}}
    if categories is not None:
        out["category-list.json"] = categories
    if recommend is not None:
        out["recommend-list.json"] = recommend
    return out


ok = lambda ref: "ok"


def run_merge(loaded, previous=None, check=ok, **kw):
    return build.merge(SOURCES, loaded, previous, check, min_apps=kw.pop("min_apps", 1), **kw)


class Merge(unittest.TestCase):
    def run_merge(self, *a, **k):
        return run_merge(*a, **k)

    def test_priority_wins_and_losers_are_recorded(self):
        files, rep = self.run_merge({"a": src(("x", {})), "b": src(("x", {"image": "other:1"}), ("y", {}))})
        self.assertIn(b"nginx:1", files["Apps/x/docker-compose.yml"])
        self.assertEqual(rep["overridden"], {"x": ["b"]})
        self.assertEqual({a["id"]: a["source"] for a in rep["apps"]}, {"x": "a", "y": "b"})

    def test_invalid_compose_and_missing_image_are_dropped_and_a_lower_source_fills_in(self):
        broken = {"apps": {"x": b"name: [", "z": b"name: Z\nservices: {}\n"}}
        check = lambda ref: "missing" if ref == "gone:1" else "ok"
        _, rep = self.run_merge({"a": broken, "b": src(("x", {"image": "gone:1"})), "c": src(("x", {}))}, check=check)
        self.assertEqual([a["source"] for a in rep["apps"]], ["c"])
        self.assertEqual({d["reason"].split(":")[0].split(" ")[0] for d in rep["dropped"]}, {"yaml", "bad", "image"})

    def test_network_mode_with_networks_is_dropped(self):
        both = compose("x", extra="    network_mode: host\n    networks: [n]\n")
        _, rep = self.run_merge({"a": {"apps": {"x": both}}, "b": src(("y", {}))})
        self.assertEqual([a["id"] for a in rep["apps"]], ["y"])

    def test_unknown_image_verdict_keeps_the_app(self):
        _, rep = self.run_merge({"a": src(("x", {}))}, check=lambda ref: "unknown")
        self.assertEqual(len(rep["apps"]), 1)

    def test_dead_source_is_carried_over_from_the_previous_build(self):
        files, _ = self.run_merge({"a": src(("x", {})), "b": src(("y", {}))})
        prev = {"manifest": json.loads(build.dump({"apps": [{"id": "x", "source": "a"}, {"id": "y", "source": "b"}]})), "files": files}
        files2, rep = self.run_merge({"a": src(("x", {})), "b": None}, prev)
        self.assertEqual([a["id"] for a in rep["apps"]], ["x", "y"])
        self.assertEqual(rep["sources"][1]["status"], "carried over from the previous build")
        self.assertEqual(files2["Apps/y/docker-compose.yml"], files["Apps/y/docker-compose.yml"])

    def test_sharp_drop_is_refused(self):
        prev = {"manifest": {"apps": [{"id": f"p{i}", "source": "a"} for i in range(10)]}, "files": {}}
        with self.assertRaises(build.Refused):
            self.run_merge({"a": src(*[(f"p{i}", {}) for i in range(8)])}, prev)  # 8 < 90% of 10, and `a` is fetched
        self.run_merge({"a": src(*[(f"p{i}", {}) for i in range(9)])}, prev)  # 9 is fine

    def test_floor_is_refused(self):
        with self.assertRaises(build.Refused):
            self.run_merge({"a": src(("x", {}))}, min_apps=2)

    def test_categories_extended_and_recommend_filtered(self):
        base = [{"name": "Media", "font": "m", "description": ""}]
        files, _ = self.run_merge({"a": src(("x", {}), ("y", {"category": "Storage"}), categories=base, recommend=[{"appid": "x"}, {"appid": "nope"}])})
        cats = json.loads(files["category-list.json"])
        self.assertEqual([c["name"] for c in cats], ["Media", "Storage"])
        self.assertEqual(cats[1]["font"], "harddisk")
        self.assertEqual(json.loads(files["recommend-list.json"]), [{"appid": "x"}])


class Pieces(unittest.TestCase):
    def test_parse_image(self):
        self.assertEqual(build.parse_image("nginx"), ("registry-1.docker.io", "library/nginx", "latest"))
        self.assertEqual(build.parse_image("ghcr.io/o/r:1.2"), ("ghcr.io", "o/r", "1.2"))
        self.assertEqual(build.parse_image("lscr.io:443/a/b@sha256:abc")[2], "sha256:abc")
        self.assertIsNone(build.parse_image("${IMG}:1"))
        with self.assertRaises(ValueError):
            build.parse_image("Bad Image:1")

    def test_pack_is_deterministic_and_find_apps_reads_nested_layouts(self):
        files = {"b": b"2", "a": b"1"}
        with tempfile.TemporaryDirectory() as t:
            build.pack(files, Path(t) / "1.zip")
            build.pack(dict(reversed(files.items())), Path(t) / "2.zip")
            self.assertEqual((Path(t) / "1.zip").read_bytes(), (Path(t) / "2.zip").read_bytes())
            d = Path(t) / "x" / "deep" / "Apps" / "app"
            d.mkdir(parents=True)
            (d / "docker-compose.yml").write_bytes(b"k")
            self.assertEqual(build.find_apps(Path(t) / "x")["apps"], {"app": b"k"})
            self.assertIsNone(build.find_apps(Path(t) / "missing"))


class _Assets(http.server.BaseHTTPRequestHandler):
    routes = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        item = self.routes.get(self.path)
        if item is None:
            self.send_error(404)
            return
        body, ctype = item
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class asset_server:
    def __init__(self, routes):
        self.routes = routes

    def __enter__(self):
        _Assets.routes = self.routes
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Assets)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()


class StorePublish(unittest.TestCase):
    def test_store_index_fields_and_sibling_digest_match_the_zip(self):
        files, rep = run_merge({"a": src(("immich", {}))})
        with tempfile.TemporaryDirectory() as t:
            build.write_store(t, files, rep)
            zip_path = Path(t) / "recasaos.zip"
            zip_bytes = zip_path.read_bytes()
            digest = hashlib.sha256(zip_bytes).hexdigest()
            sibling = json.loads((Path(t) / "store-index.json").read_text())
            self.assertEqual(sibling["schema"], "recasaos.store-index.v1")
            self.assertEqual(sibling["store_id"], "recasaos-catalogue")
            self.assertEqual(sibling["url"], ORG_ZIP)
            self.assertIn(GITHUB_ZIP, sibling["mirrors"])
            self.assertEqual(sibling["sha256"], digest)
            self.assertEqual((Path(t) / "recasaos.zip.sha256").read_text(), f"{digest}  recasaos.zip\n")
            with zipfile.ZipFile(zip_path) as z:
                inner = json.loads(z.read("store-index.json"))
            self.assertEqual(inner["schema"], sibling["schema"])
            self.assertNotIn("sha256", inner)
            self.assertTrue(sibling["apps"])
            for app in sibling["apps"]:
                self.assertEqual(set(app), {"id", "revision", "content_hash"})
                self.assertEqual(app["id"], "immich")
                self.assertEqual(app["revision"], 1)
                self.assertTrue(app["content_hash"].startswith("sha256:"))
                self.assertEqual(len(app["content_hash"]), len("sha256:") + 64)

    def test_http_icon_is_written_and_compose_rewritten(self):
        with asset_server({"/icon.png": (PNG, "image/png")}) as base:
            files, _ = run_merge({"a": src(("immich", {"icon": f"{base}/icon.png"}))})
        self.assertEqual(files["Apps/immich/icon.png"], PNG)
        self.assertEqual(yaml.safe_load(files["Apps/immich/docker-compose.yml"])["x-casaos"]["icon"], "icon.png")

    def test_oversize_icon_is_omitted_and_the_app_is_kept(self):
        big = PNG + b"x" * (128 * 1024)
        with asset_server({"/icon.png": (big, "image/png")}) as base:
            files, rep = run_merge({"a": src(("immich", {"icon": f"{base}/icon.png"}))})
        self.assertEqual([a["id"] for a in rep["apps"]], ["immich"])
        self.assertFalse(any(p.rsplit("/", 1)[-1].startswith("icon.") for p in files if p.startswith("Apps/immich/")))
        self.assertEqual(yaml.safe_load(files["Apps/immich/docker-compose.yml"])["x-casaos"]["icon"], "")

    def test_failed_screenshot_is_skipped_and_later_ones_kept(self):
        shot = b"\x89PNG\r\n\x1a\n" + b"shot"
        big = shot + b"x" * (512 * 1024)
        with asset_server({"/big.png": (big, "image/png"), "/ok.png": (shot, "image/png")}) as base:
            files, _ = run_merge({"a": src(("immich", {"screenshots": [f"{base}/big.png", f"{base}/ok.png"]}))})
        self.assertEqual(files["Apps/immich/screenshot-1.png"], shot)
        self.assertNotIn("Apps/immich/screenshot-2.png", files)
        self.assertEqual(
            yaml.safe_load(files["Apps/immich/docker-compose.yml"])["x-casaos"]["screenshot_link"],
            ["screenshot-1.png"],
        )

    def test_carried_app_keeps_inlined_icon_from_previous_zip(self):
        with asset_server({"/icon.png": (PNG, "image/png")}) as base:
            files, _ = run_merge({"a": src(("x", {})), "b": src(("y", {"icon": f"{base}/icon.png"}))})
        prev = {
            "manifest": json.loads(build.dump({"apps": [{"id": "x", "source": "a"}, {"id": "y", "source": "b"}]})),
            "files": files,
        }
        files2, rep = run_merge({"a": src(("x", {})), "b": None}, prev)
        self.assertEqual(rep["sources"][1]["status"], "carried over from the previous build")
        self.assertEqual(files2["Apps/y/icon.png"], PNG)
        self.assertEqual(files2["Apps/y/docker-compose.yml"], files["Apps/y/docker-compose.yml"])

    def test_html_and_gif_payloads_are_omitted_and_the_app_is_kept(self):
        html = b"<!DOCTYPE html><html><body>soft 404</body></html>"
        gif = b"GIF89a" + b"\x00" * 32
        with asset_server({
            "/icon.png": (html, "text/html"),
            "/gif.png": (gif, "image/gif"),
            "/empty.png": (b"", "image/png"),
        }) as base:
            files, rep = run_merge({"a": src(
                ("htmlapp", {"icon": f"{base}/icon.png"}),
                ("gifapp", {"icon": f"{base}/gif.png"}),
                ("emptyapp", {"icon": f"{base}/empty.png"}),
            )})
        self.assertEqual(sorted(a["id"] for a in rep["apps"]), ["emptyapp", "gifapp", "htmlapp"])
        for app in ("htmlapp", "gifapp", "emptyapp"):
            self.assertFalse(
                any(p.rsplit("/", 1)[-1].startswith("icon.") for p in files if p.startswith(f"Apps/{app}/")),
                app,
            )
            self.assertEqual(yaml.safe_load(files[f"Apps/{app}/docker-compose.yml"])["x-casaos"]["icon"], "")

    def test_malformed_icon_url_keeps_the_app(self):
        try:
            files, rep = run_merge({"a": src(("immich", {"icon": "http://127.0.0.1:abc/icon.png"}))})
        except Exception as e:
            self.fail(f"merge aborted: {type(e).__name__}: {e}")
        self.assertEqual([a["id"] for a in rep["apps"]], ["immich"])
        self.assertFalse(any(p.rsplit("/", 1)[-1].startswith("icon.") for p in files if p.startswith("Apps/immich/")))
        self.assertEqual(yaml.safe_load(files["Apps/immich/docker-compose.yml"])["x-casaos"]["icon"], "")

    def test_dropped_icon_and_screenshots_do_not_keep_previous_files(self):
        shot = b"\x89PNG\r\n\x1a\n" + b"shot"
        with asset_server({"/icon.png": (PNG, "image/png"), "/shot.png": (shot, "image/png")}) as base:
            files, _ = run_merge({"a": src(("immich", {
                "icon": f"{base}/icon.png",
                "screenshots": [f"{base}/shot.png"],
            }))})
        self.assertIn("Apps/immich/icon.png", files)
        self.assertIn("Apps/immich/screenshot-1.png", files)
        prev = {
            "manifest": {"apps": [{"id": "immich", "source": "a"}]},
            "files": files,
        }
        files2, rep = run_merge({"a": src(("immich", {}))}, prev)
        self.assertEqual([a["id"] for a in rep["apps"]], ["immich"])
        self.assertNotIn("Apps/immich/icon.png", files2)
        self.assertNotIn("Apps/immich/screenshot-1.png", files2)

    def test_revision_starts_at_1_and_bumps_when_compose_hash_changes(self):
        files, rep = run_merge({"a": src(("immich", {}))})
        index = build.store_index(files, rep)
        self.assertEqual(index["apps"][0]["revision"], 1)
        prev = {
            "manifest": {"apps": [{"id": "immich", "source": "a"}]},
            "files": {**files, "store-index.json": build.dump(index)},
        }
        files2, rep2 = run_merge({"a": src(("immich", {}))}, prev)
        self.assertEqual(build.store_index(files2, rep2, prev)["apps"][0]["revision"], 1)
        files3, rep3 = run_merge({"a": src(("immich", {"image": "nginx:2"}))}, prev)
        self.assertEqual(build.store_index(files3, rep3, prev)["apps"][0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
