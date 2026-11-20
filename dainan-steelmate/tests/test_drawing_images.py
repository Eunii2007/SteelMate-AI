import hashlib
import io
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from streamlit.testing.v1 import AppTest

from app import (
    build_order_snapshot, customer_quotation_markup, customer_quote_fields, customer_wechat_text,
)
from core.vision_parser import BUILTIN_DRAWING_SPECS
from quote_store import (
    connect_database, delete_quote, get_quote, get_quote_drawing, list_quotes, save_quote, validate_drawing_image,
)
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]


def drawing(sample):
    path = ROOT / "data" / "Test_Images" / f"{sample}.png"
    return validate_drawing_image(path.read_bytes(), path.name)


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()


class DrawingImagesTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = self.directory / "quotes.sqlite3"
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(self.database)}))

    def workbench(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        return app.run()

    def history(self, quote_id):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(quote_id)})
        return app.run()

    def upload(self, app, sample, filename=None):
        image = drawing(sample)
        app.file_uploader[0].upload(filename or image["filename"], image["data"], "image/png").run()

    def test_validated_original_bytes_survive_reopen_and_idempotent_save(self):
        raw = drawing("TP-001")
        original = demo_snapshot("TP-001")
        quote_id = save_quote("one", original, drawings={1: raw})
        snapshot_before = get_quote(quote_id)
        with closing(sqlite3.connect(self.database)) as reopened:
            blob, = reopened.execute("SELECT data FROM drawing_assets WHERE sha256 = ?", (raw["sha256"],)).fetchone()
        self.assertEqual(blob, raw["data"])
        self.assertEqual(hashlib.sha256(blob).hexdigest(), raw["sha256"])
        self.assertEqual(get_quote_drawing(quote_id), raw)
        self.assertEqual(save_quote("one", original, drawings={1: drawing("TP-002")}), quote_id)
        self.assertEqual(get_quote_drawing(quote_id), raw)
        self.assertEqual(get_quote(quote_id), snapshot_before)
        legacy_id = save_quote("legacy", original)
        save_quote("legacy", original, drawings={1: raw})
        self.assertIsNone(get_quote_drawing(legacy_id))

    def test_image_validation_checks_format_and_preserves_jpeg_bytes(self):
        output = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(output, format="JPEG", comment=b"original-data")
        data = output.getvalue()
        checked = validate_drawing_image(data, r"C:\incoming\small-labels.jpg")
        self.assertEqual(checked["filename"], "small-labels.jpg")
        self.assertEqual(checked["mime_type"], "image/jpeg")
        self.assertEqual(checked["data"], data)
        for invalid in (b"", b"<svg></svg>", data[:20]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                save_quote("invalid", demo_snapshot("TP-001"), drawings={1: {"data": invalid, "filename": "bad.png"}})
        self.assertEqual(list_quotes(), [])

    def test_quote_and_all_images_roll_back_together(self):
        first, second = drawing("TP-001"), drawing("TP-002")
        source_id = save_quote("source", demo_snapshot("TP-001"), drawings={1: first})
        original = get_quote(source_id)
        with connect_database() as connection:
            connection.execute("""CREATE TRIGGER fail_second_image BEFORE INSERT ON quote_drawings
                WHEN NEW.line_position = 2 BEGIN SELECT RAISE(ABORT, 'simulated image write failure'); END""")
        order = build_order_snapshot([demo_snapshot("TP-001"), demo_snapshot("TP-002")], original["snapshot"]["issued"])
        with self.assertRaises(sqlite3.IntegrityError):
            save_quote("order", order, drawings={1: first, 2: second})
        self.assertEqual(list_quotes(), [original])
        with connect_database() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM drawing_assets").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quote_drawings").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            save_quote("invalid-position", demo_snapshot("TP-001"), drawings={2: first})

    def test_upload_copy_and_replacing_same_filename_keep_correct_originals(self):
        app = self.workbench()
        self.upload(app, "TP-001")
        click(app, "识别图纸参数")
        next(field for field in app.number_input if field.label == "板厚（mm）").set_value(9.0).run()
        self.assertNotIn("drawing_run", app.session_state)
        click(app, "确认参数并核价")
        click(app, "保存询盘")
        first_id = app.session_state["quote_save"]["quote_id"]
        original = get_quote(first_id)
        self.assertEqual(get_quote_drawing(first_id), drawing("TP-001"))
        copied = self.history(first_id)
        click(copied, "复制改价")
        self.assertEqual(copied.session_state["inquiry_drawing"], drawing("TP-001"))
        self.assertNotIn("drawing_run", copied.session_state)
        click(copied, "确认参数并核价")
        click(copied, "保存询盘")
        second_id = copied.session_state["quote_save"]["quote_id"]
        self.assertEqual(get_quote_drawing(second_id), drawing("TP-001"))
        replacement = self.history(second_id)
        click(replacement, "复制改价")
        self.upload(replacement, "TP-002", filename="TP-001.png")
        self.assertNotIn("drawing_run", replacement.session_state)
        recognition = {"fields": BUILTIN_DRAWING_SPECS["TP-002"], "source": "模拟识图"}
        with patch("core.vision_parser.recognize_new_drawing", return_value={**recognition, "error": None}):
            click(replacement, "识别图纸参数")
        click(replacement, "确认参数并核价")
        click(replacement, "保存询盘")
        third_id = replacement.session_state["quote_save"]["quote_id"]
        self.assertEqual(get_quote(third_id)["source_quote_id"], second_id)
        self.assertEqual(get_quote_drawing(third_id)["data"], drawing("TP-002")["data"])
        self.assertEqual(get_quote_drawing(first_id), drawing("TP-001"))
        self.assertEqual(get_quote_drawing(second_id), drawing("TP-001"))
        self.assertEqual(get_quote(first_id), original)
        self.assertFalse(replacement.exception)

    def test_order_freezes_each_image_and_history_selects_the_right_line(self):
        app = self.workbench()
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        click(app, "加入本单")
        first_line_id = next(iter(app.session_state["order_items"]))
        app.button(key="PRESET_1_FAST_QUOTE").click().run()
        click(app, "加入本单")
        self.assertEqual(app.session_state["order_drawings"][first_line_id], drawing("TP-001"))
        click(app, "保存本单")
        quote_id = app.session_state["order_save"]["quote_id"]
        self.assertEqual(get_quote_drawing(quote_id, 1), drawing("TP-001"))
        self.assertEqual(get_quote_drawing(quote_id, 2), drawing("TP-002"))
        history = self.history(quote_id)
        next(field for field in history.selectbox if field.label == "选择图纸明细").set_value(2).run()
        viewer = "\n".join(item.proto.body for item in history.get("html") if "drawing-view-history" in item.proto.body)
        self.assertIn("TP-002.png", viewer)
        self.assertNotIn("TP-001.png", viewer)
        self.assertFalse(history.exception)
        copied_id = save_quote("copied-line", demo_snapshot("TP-002"), quote_id,
                               drawings={1: get_quote_drawing(quote_id, 2)})
        delete_quote(quote_id)
        self.assertEqual(get_quote_drawing(copied_id), drawing("TP-002"))

    def test_legacy_and_manual_no_image_do_not_guess_from_filename(self):
        legacy_id = save_quote("legacy", demo_snapshot("TP-001"))
        history = self.history(legacy_id)
        self.assertTrue(any(caption.value == "无留存图纸" for caption in history.caption))
        self.assertEqual(len(history.get("download_button")), 3)
        click(history, "复制改价")
        self.assertIsNone(history.session_state["inquiry_drawing"])
        self.assertNotIn("drawing_run", history.session_state)
        click(history, "确认参数并核价")
        click(history, "保存询盘")
        self.assertIsNone(get_quote_drawing(history.session_state["quote_save"]["quote_id"]))
        click(history, "新询盘")
        click(history, "手动录入")
        self.assertTrue(any(caption.value == "无留存图纸" for caption in history.caption))

    def test_viewer_and_image_storage_do_not_enter_customer_exports(self):
        app = self.workbench()
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        current = app.session_state["inquiry_drawing"]
        self.assertIn("showModal()", "\n".join(item.proto.body for item in app.get("html")))
        click(app, "保存询盘")
        saved = get_quote(app.session_state["quote_save"]["quote_id"])
        serialized = json.dumps(saved)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("data:image/png", serialized)
        fields = customer_quote_fields(saved["snapshot"], saved["id"])
        output = customer_wechat_text(fields) + customer_quotation_markup(fields)
        for internal in (current["sha256"], "drawing_assets", "drawing-view", "data:image/png"):
            self.assertNotIn(internal, output)
