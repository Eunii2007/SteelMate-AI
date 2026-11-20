import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openai import APITimeoutError
from streamlit.testing.v1 import AppTest

from app import DEFAULT_TARGET_MARGIN, quote_rows, quote_valid_until, reprice
from core.agent_pipeline import run_pipeline
from core.reply_parser import CHANGED, NEEDS_REVIEW, UNCHANGED, validate_reply_suggestions
from core.vision_parser import BUILTIN_DRAWING_SPECS
from quote_store import get_quote, list_quotes, save_quote


ROOT = Path(__file__).resolve().parents[1]
FLANGE = BUILTIN_DRAWING_SPECS["TP-001"]
REPLY = "改成 30 件，厚度还是 12，要开专票"


def response_fields(*, quantity=(), thickness=(), invoice=(), other=()):
    return {
        "quantity": {"evidence": list(quantity), "uncertain": False},
        "thickness_mm": {"evidence": list(thickness), "uncertain": False},
        "invoice": list(invoice),
        "other": list(other),
    }


def api_response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def quantity_input(app: AppTest):
    return next(item for item in app.number_input if item.label.startswith("采购数量"))


class CustomerReplyTest(unittest.TestCase):
    def open_copy(self) -> AppTest:
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "DAINAN_STORAGE_MODE": "single_user",
            "DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3"), "OPENAI_API_KEY": "fixture-key",
        }))
        issued = "2026-11-17 10:00:00"
        quote, costs = reprice(FLANGE, DEFAULT_TARGET_MARGIN)
        scrap = run_pipeline(FLANGE)["scrap_result"]
        snapshot = {
            "filename": "TP-001.png", "inquiry": FLANGE, "demo": True, "quote": quote, "costs": costs,
            "issued": issued, "valid_until": quote_valid_until(issued), "tolerance": 0.95,
            "gross_margin_percent": DEFAULT_TARGET_MARGIN, "scrap": scrap,
        }
        snapshot["rows"] = quote_rows(snapshot, quote, scrap, 0.95, DEFAULT_TARGET_MARGIN)
        self.assertEqual(save_quote("original", snapshot), 1)
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "records"
        app.query_params["quote"] = "1"
        app.run()
        self.assertFalse(app.text_area)
        button(app, "复制改价").click().run()
        self.assertFalse(app.exception)
        return app

    def test_validates_evidence_amounts_units_conflicts_and_shape(self):
        items = validate_reply_suggestions(REPLY, FLANGE, response_fields(
            quantity=["改成 30 件"], thickness=["厚度还是 12"], invoice=["要开专票"],
        ))
        self.assertEqual([(row.field, row.status, row.value) for row in items], [
            ("quantity", CHANGED, 30), ("thickness_mm", UNCHANGED, 12.0), ("invoice", NEEDS_REVIEW, None),
        ])
        cases = (
            (FLANGE, "厚度改薄一点", "thickness", "厚度改薄一点", NEEDS_REVIEW, None),
            (FLANGE, "改 30 件，不，先报 40 件", "quantity", "先报 40 件", NEEDS_REVIEW, None),
            (FLANGE, "板厚改到 0.9 厘米", "thickness", "板厚改到 0.9 厘米", CHANGED, 9.0),
            (FLANGE, "板厚改成 9", "thickness", "板厚改成 9", NEEDS_REVIEW, None),
            (FLANGE, "板厚改到 9 mm，板厚要 8 mm", "thickness", "板厚要 8 mm", NEEDS_REVIEW, None),
            (FLANGE, "数量改到 -3 件", "quantity", "数量改到 -3 件", NEEDS_REVIEW, None),
            (FLANGE, "数量改到 2.5 件", "quantity", "数量改到 2.5 件", NEEDS_REVIEW, None),
            (FLANGE, "每件加开 4 个孔", "quantity", "4 个", NEEDS_REVIEW, None),
            (FLANGE, "带中心孔的法兰改成 30 件", "quantity", "改成 30 件", CHANGED, 30),
            (BUILTIN_DRAWING_SPECS["TP-010"], "轴向长度改到 200 mm", "thickness", "轴向长度改到 200 mm", NEEDS_REVIEW, None),
        )
        for original, reply, field, evidence, status, value in cases:
            with self.subTest(reply=reply):
                item, = validate_reply_suggestions(reply, original, response_fields(**{field: [evidence]}))
                self.assertEqual((item.status, item.value), (status, value))
        with self.assertRaisesRegex(ValueError, "格式异常"):
            validate_reply_suggestions(REPLY, FLANGE, response_fields(quantity=["改为 99 件"]))
        payload = response_fields(quantity=["改成30件"], thickness=["厚度不变"])
        payload["thickness_mm"]["uncertain"] = True
        items = validate_reply_suggestions("改成30件，厚度不变", FLANGE, payload)
        self.assertEqual((items[1].status, items[1].value), (UNCHANGED, 12.0))
        uncertain = response_fields(thickness=["厚度不变"])
        uncertain["thickness_mm"]["uncertain"] = True
        items = validate_reply_suggestions("厚度不变？", FLANGE, uncertain)
        self.assertEqual(items[0].status, NEEDS_REVIEW)
        ambiguous = response_fields(thickness=["厚度不变", "厚度改薄一点"])
        items = validate_reply_suggestions("厚度不变？厚度改薄一点", FLANGE, ambiguous)
        self.assertEqual(items[0].status, NEEDS_REVIEW)

    @patch("openai.OpenAI")
    def test_recognize_apply_confirm_and_save_are_separate(self, client):
        app = self.open_copy()
        original = get_quote(1)
        create = client.return_value.__enter__.return_value.chat.completions.create
        create.return_value = api_response(response_fields(
            quantity=["改成 30 件"], thickness=["厚度还是 12"], invoice=["要开专票"],
        ))
        app.text_area[0].set_value(REPLY).run()
        self.assertEqual(create.call_count, 0)
        self.assertEqual(quantity_input(app).value, 20)
        button(app, "识别修改项").click().run()
        self.assertEqual(create.call_count, 1)
        messages = create.call_args.kwargs["messages"]
        self.assertTrue(all(isinstance(message["content"], str) for message in messages))
        self.assertEqual(json.loads(messages[1]["content"]), {
            "customer_reply": REPLY, "original": {"shape": "flange", "quantity": 20, "plate_thickness_mm": 12.0},
        })
        self.assertEqual(quantity_input(app).value, 20)
        self.assertNotIn("drawing_run", app.session_state)
        button(app, "确认参数并核价").click().run()
        self.assertNotIn("drawing_run", app.session_state)
        button(app, "应用到草稿").click().run()
        self.assertEqual(quantity_input(app).value, 30)
        self.assertNotIn("drawing_run", app.session_state)
        self.assertFalse(app.get("download_button"))
        button(app, "确认参数并核价").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 30)
        button(app, "保存询盘").click().run()
        new, old = list_quotes()
        self.assertEqual(old, original)
        self.assertEqual(new["source_quote_id"], 1)
        self.assertNotIn(REPLY, json.dumps(new, ensure_ascii=False))
        self.assertEqual(create.call_count, 1)

    @patch("openai.OpenAI")
    def test_failed_recognition_leaves_reply_and_manual_quote_available(self, client):
        app = self.open_copy()
        create = client.return_value.__enter__.return_value.chat.completions.create
        app.text_area[0].set_value("请报价 30 件").run()
        create.side_effect = APITimeoutError(request=None)
        button(app, "识别修改项").click().run()
        self.assertTrue(any("超时" in warning.value for warning in app.warning))
        self.assertEqual(app.text_area[0].value, "请报价 30 件")
        create.side_effect = None
        create.return_value = api_response({"unexpected": []})
        button(app, "识别修改项").click().run()
        self.assertTrue(any("格式异常" in warning.value for warning in app.warning))
        self.assertEqual(app.text_area[0].value, "请报价 30 件")
        self.assertNotIn("应用到草稿", [item.label for item in app.button])
        quantity_input(app).set_value(30)
        button(app, "确认参数并核价").click().run()
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 30)
        self.assertEqual(create.call_count, 2)

    @patch("openai.OpenAI")
    def test_additional_machining_requires_manual_review(self, client):
        app = self.open_copy()
        create = client.return_value.__enter__.return_value.chat.completions.create
        create.return_value = api_response(response_fields(other=["再给每件加开 4 个孔"]))
        app.text_area[0].set_value("再给每件加开 4 个孔").run()
        button(app, "识别修改项").click().run()
        self.assertTrue(any("其他改价要求" in item.proto.body for item in app.get("html")))
        self.assertNotIn("应用到草稿", [item.label for item in app.button])
        button(app, "确认参数并核价").click().run()
        self.assertNotIn("drawing_run", app.session_state)
        app.checkbox[0].check()
        button(app, "确认参数并核价").click().run()
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["quantity"], 20)

    @patch("openai.OpenAI")
    def test_apply_converted_thickness_leaves_other_parameters_for_review(self, client):
        app = self.open_copy()
        create = client.return_value.__enter__.return_value.chat.completions.create
        create.return_value = api_response(response_fields(
            thickness=["板厚改成 0.9 厘米"], other=["材质换 304"],
        ))
        app.text_area[0].set_value("板厚改成 0.9 厘米，材质换 304").run()
        button(app, "识别修改项").click().run()
        thickness = next(field for field in app.number_input if field.label.startswith("板厚"))
        self.assertEqual(thickness.value, 12.0)
        button(app, "应用到草稿").click().run()
        thickness = next(field for field in app.number_input if field.label.startswith("板厚"))
        self.assertEqual(thickness.value, 9.0)
        self.assertEqual(app.text_input[0].value, "316L")
        self.assertEqual(quantity_input(app).value, 20)
        self.assertNotIn("drawing_run", app.session_state)
