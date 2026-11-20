import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import customer_quotation_markup, customer_quote_fields
from core.vision_parser import BUILTIN_DRAWING_SPECS
from quote_store import get_quote, save_quote
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]


def click(app: AppTest, label: str) -> None:
    next(button for button in app.button if button.label == label).click().run()


class IntegrationStateTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))
        self.old_id = save_quote("original", demo_snapshot("TP-001"))
        self.new_id = save_quote("revision", demo_snapshot("TP-001", quantity=30), self.old_id)

    def open_record(self, quote_id: int) -> AppTest:
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(quote_id)})
        return app.run()

    def test_editing_reply_invalidates_quote_and_old_suggestions(self):
        app = self.open_record(self.old_id)
        original = get_quote(self.old_id)
        click(app, "复制改价")
        quantity = next(field for field in app.number_input if field.label.startswith("采购数量"))
        quantity.set_value(30)
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 30)
        app.session_state["drawing_review"]["reply_analysis"] = {"fingerprint": "old", "items": []}
        app.text_area[0].set_value("改30件，不，先报40件").run()
        self.assertNotIn("drawing_run", app.session_state)
        self.assertNotIn("reply_analysis", app.session_state["drawing_review"])
        self.assertNotIn("confirmed", app.session_state["drawing_review"])
        self.assertFalse(app.get("download_button"))
        self.assertNotIn("保存询盘", [button.label for button in app.button])
        self.assertNotIn("加入本单", [button.label for button in app.button])
        self.assertEqual(get_quote(self.old_id), original)

    def test_changing_history_record_clears_previous_print_preview(self):
        app = self.open_record(self.new_id)
        snapshot = get_quote(self.new_id)["snapshot"]
        app.session_state["formal_report"] = customer_quotation_markup(customer_quote_fields(snapshot, self.new_id))
        app.run()
        self.assertTrue(any("Q-000002" in element.proto.body and "industrial-report" in element.proto.body
                            for element in app.get("html")))
        app.query_params["quote"] = str(self.old_id)
        app.run()
        self.assertFalse(app.exception)
        self.assertNotIn("formal_report", app.session_state)
        self.assertFalse(any("industrial-report" in element.proto.body for element in app.get("html")))
        self.assertTrue(any("¥4,729.79" in element.proto.body for element in app.get("html")))

    def test_repeated_vision_timeout_keeps_last_confirmed_inputs(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        drawing = ROOT / "data" / "Blind_Test" / "BL-06.png"
        app.file_uploader[0].upload(drawing.name, drawing.read_bytes(), "image/png").run()
        recognition = {
            "fields": BUILTIN_DRAWING_SPECS["TP-001"], "error": None, "model": "timeout-fixture",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        with patch("core.vision_parser.recognize_new_drawing", return_value=recognition):
            click(app, "识别图纸参数")
        next(field for field in app.number_input if field.label.startswith("板厚")).set_value(9.0)
        next(field for field in app.number_input if field.label.startswith("采购数量")).set_value(35)
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 35)
        with patch("core.vision_parser.recognize_new_drawing", side_effect=ValueError("视觉接口响应超时")):
            click(app, "识别图纸参数")
        self.assertTrue(app.get("image"))
        self.assertTrue(any("超时" in warning.value for warning in app.warning))
        self.assertNotIn("drawing_run", app.session_state)
        self.assertEqual(next(field for field in app.number_input if field.label.startswith("板厚")).value, 9.0)
        self.assertEqual(next(field for field in app.number_input if field.label.startswith("采购数量")).value, 35)
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "SUCCESS")
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 35)
