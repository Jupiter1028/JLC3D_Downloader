"""离线单元测试：解析、文件名、原子写入、配置迁移、取消机制。

运行：python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import JLC3DDownload as app  # noqa: E402


class ModelParsingTests(unittest.TestCase):
    def test_reads_svgnode_model(self):
        shape = "SVGNODE~" + json.dumps({
            "attrs": {"c_etype": "outline3D", "uuid": "abc123", "title": "R0603"},
        })
        package = {
            "title": "R0603",
            "dataStr": json.dumps({
                "head": {"c_para": {"3DModel": "R0603"}},
                "shape": ["PAD~1", shape],
            }),
        }
        model, name = app.find_package_model({"packageDetail": package})
        self.assertEqual(model, "abc123")
        self.assertEqual(name, "R0603")

    def test_accepts_already_parsed_document(self):
        package = {"dataStr": {"shape": [
            "SVGNODE~" + json.dumps({"attrs": {"c_etype": "outline3D", "uuid": "dict-id"}}),
        ]}}
        self.assertEqual(app.find_package_model({"packageDetail": package})[0], "dict-id")

    def test_ignores_non_3d_nodes(self):
        package = {"dataStr": {"shape": [
            "SVGNODE~" + json.dumps({"attrs": {"c_etype": "outline", "uuid": "not-a-model"}}),
        ]}}
        self.assertEqual(app.find_package_model({"packageDetail": package}), (None, None))

    def test_rejects_invalid_model_ids(self):
        package = {"dataStr": {"shape": [
            "SVGNODE~" + json.dumps({"attrs": {"c_etype": "outline3D", "uuid": "../../etc/passwd"}}),
        ]}}
        self.assertEqual(app.find_package_model({"packageDetail": package}), (None, None))

    def test_reads_library_id_from_package_head(self):
        package = {"dataStr": {"head": {"uuid_3d": "lib-1"}}}
        self.assertEqual(app.find_library_id({"packageDetail": package}), "lib-1")

    def test_reads_library_id_from_symbol_document(self):
        result = {"dataStr": json.dumps({"head": {"uuid_3d": "lib-2"}})}
        self.assertEqual(app.find_library_id(result), "lib-2")

    def test_missing_package_is_not_an_error(self):
        self.assertEqual(app.find_package_model({}), (None, None))
        self.assertIsNone(app.find_library_id({}))


class FilenameTests(unittest.TestCase):
    def test_keeps_normal_code(self):
        self.assertEqual(app.safe_filename("C8734"), "C8734")

    def test_strips_path_separators(self):
        cleaned = app.safe_filename("..\\..\\evil")
        self.assertNotIn("\\", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertTrue(cleaned)

    def test_escapes_windows_device_names(self):
        self.assertEqual(app.safe_filename("CON"), "_CON")


class AtomicWriteTests(unittest.TestCase):
    def test_writes_and_replaces_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "C8734.step"
            app.atomic_write(target, b"first")
            self.assertEqual(target.read_bytes(), b"first")
            app.atomic_write(target, b"second")
            self.assertEqual(target.read_bytes(), b"second")
            leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
            self.assertEqual(leftovers, [])

    def test_reports_unusable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(app.JlcError):
                app.atomic_write(blocker / "C8734.step", b"data")


class CodeValidationTests(unittest.TestCase):
    def test_rejects_non_lcsc_code_before_network(self):
        client = app.JlcClient()
        with self.assertRaises(app.JlcError):
            client.resolve_model("STM32")
        with self.assertRaises(app.JlcError):
            client.resolve_model("")


class CancellationTests(unittest.TestCase):
    class SlowResponse:
        status_code = 200
        headers = {"Content-Length": "1000"}

        def iter_content(self, size):
            for _ in range(20):
                time.sleep(0.02)
                yield b"x" * 50

        def close(self):
            pass

    def test_download_stops_when_cancelled(self):
        client = app.JlcClient()
        cancelled = threading.Event()
        info = app.ModelInfo(code="C1", title="t", model_id="model-1")

        def fake_send(url, method="GET", data=None, stream=False):
            return self.SlowResponse()

        with mock.patch.object(client, "_send", side_effect=fake_send):
            timer = threading.Timer(0.1, cancelled.set)
            timer.start()
            with self.assertRaises(app.Cancelled):
                client.download_step(info, cancelled=cancelled)
            timer.cancel()


class SettingsTests(unittest.TestCase):
    def test_roundtrip_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            app_dir = tmp / "app"
            config = app_dir / "config.ini"
            legacy = tmp / "legacy" / "config.ini"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("[PATH]\nDownloadPath = " + str(tmp) + "\n", encoding="utf-8")

            with mock.patch.object(app, "APP_DIR", app_dir), \
                    mock.patch.object(app, "CONFIG_FILE", config), \
                    mock.patch.object(app, "LEGACY_CONFIG", legacy):
                settings = app.load_settings()
                self.assertEqual(settings["download_path"], str(tmp))
                self.assertTrue(config.exists())

                app.save_settings(tmp / "other")
                self.assertEqual(app.load_settings()["download_path"], str(tmp / "other"))


if __name__ == "__main__":
    unittest.main()
