import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import QUOTE_OPTIONS, parameter_changes, reprice
from core import steel_engine
from core.reply_parser import CHANGED, ReplyItem
from quote_store import delete_quote, get_quote, save_quote
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FIELDS = {
    "plate": {"长度（mm）", "宽度（mm）", "板厚（mm）", "采购数量（件）"},
    "flange": {"外径（mm）", "中心孔内径（mm）", "板厚（mm）", "采购数量（件）"},
    "round_bar": {"直径（mm）", "轴向长度（mm）", "采购数量（件）"},
}


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()


def number(app, label):
    return next(field for field in app.number_input if field.label == label)


def html_blocks(app, marker):
    return "\n".join(element.proto.body for element in app.get("html") if marker in element.proto.body)


class ShapeDraftTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_STORAGE_MODE": "single_user", "DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))

    def workbench(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        return app.run()

    def test_shape_switch_restores_separate_dimensions_and_common_fields(self):
        app = self.workbench()
        click(app, "手动录入")
        self.assertEqual([field.label for field in app.number_input], ["采购数量（件）"])
        app.text_input[0].set_value("304").run()
        number(app, "采购数量（件）").set_value(15).run()
        examples = {
            "plate": {"长度（mm）": 300.0, "宽度（mm）": 250.0, "板厚（mm）": 10.0},
            "round_bar": {"直径（mm）": 80.0, "轴向长度（mm）": 450.0},
            "flange": {"外径（mm）": 280.0, "中心孔内径（mm）": 0.0, "板厚（mm）": 12.0},
        }
        for shape, values in examples.items():
            next(field for field in app.selectbox if field.label == "零件形状").set_value(shape).run()
            self.assertEqual({field.label for field in app.number_input}, EXPECTED_FIELDS[shape])
            self.assertEqual(number(app, "采购数量（件）").value, 15)
            self.assertEqual(app.text_input[0].value, "304")
            for label, value in values.items():
                self.assertIsNone(number(app, label).value)
                number(app, label).set_value(value).run()
            click(app, "确认参数并核价")
            self.assertFalse(app.exception)
            run = app.session_state["drawing_run"]
            self.assertEqual(run["inquiry"]["shape"], shape)
            self.assertEqual(run["inquiry"]["width_mm"], 250.0 if shape == "plate" else 0.0)
            self.assertEqual(run["inquiry"]["inner_dim_mm"], 0.0)
        for shape, values in examples.items():
            next(field for field in app.selectbox if field.label == "零件形状").set_value(shape).run()
            for label, value in values.items():
                self.assertEqual(number(app, label).value, value)
            self.assertNotIn("drawing_run", app.session_state)
        click(app, "确认参数并核价")
        number(app, "外径（mm）").set_value(300.0).run()
        self.assertNotIn("drawing_run", app.session_state)
        self.assertFalse(any(button.label in ("下载客户报价单", "下载 HTML（可打印）", "下载 CSV")
                             for button in app.get("download_button")))

    def test_uploaded_presets_enter_the_same_shape_draft(self):
        app = self.workbench()
        for sample, shape in (("TP-001", "flange"), ("TP-002", "plate"), ("TP-010", "round_bar")):
            drawing = ROOT / "data" / "Test_Images" / f"{sample}.png"
            app.file_uploader[0].upload(drawing.name, drawing.read_bytes(), "image/png").run()
            click(app, "识别图纸参数")
            self.assertFalse(app.exception)
            self.assertEqual({field.label for field in app.number_input}, EXPECTED_FIELDS[shape])
            self.assertNotIn("drawing_run", app.session_state)
            click(app, "确认参数并核价")
            self.assertEqual(app.session_state["drawing_run"]["inquiry"]["shape"], shape)

    def test_reply_thickness_stays_in_board_draft_and_never_overwrites_bar_length(self):
        source_id = save_quote("source", demo_snapshot("TP-001"))
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(source_id)})
        app.run()
        click(app, "复制改价")
        next(field for field in app.selectbox if field.label == "零件形状").set_value("round_bar").run()
        number(app, "直径（mm）").set_value(80.0).run()
        number(app, "轴向长度（mm）").set_value(450.0).run()
        app.text_area[0].set_value("改成30件，板厚改为9 mm").run()
        suggestions = [
            ReplyItem("quantity", "数量", "20 件", "30 件", CHANGED, 30),
            ReplyItem("thickness_mm", "板厚", "12 mm", "9 mm", CHANGED, 9.0),
        ]
        with patch("core.reply_parser.recognize_quote_reply", return_value=suggestions):
            click(app, "识别修改项")
        click(app, "应用到草稿")
        self.assertEqual(number(app, "采购数量（件）").value, 30)
        self.assertEqual(number(app, "轴向长度（mm）").value, 450.0)
        self.assertNotIn("drawing_run", app.session_state)
        next(field for field in app.selectbox if field.label == "零件形状").set_value("flange").run()
        click(app, "应用到草稿")
        self.assertEqual(number(app, "板厚（mm）").value, 9.0)
        for shape, label, value in (("round_bar", "轴向长度（mm）", 450.0), ("flange", "板厚（mm）", 9.0)):
            next(field for field in app.selectbox if field.label == "零件形状").set_value(shape).run()
            self.assertEqual(number(app, label).value, value)
        click(app, "确认参数并核价")
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["drawing_run"]["inquiry"]["thickness_mm"], 9.0)
        self.assertEqual(get_quote(source_id)["snapshot"]["inquiry"]["thickness_mm"], 12.0)

    def test_draft_and_saved_price_changes_use_source_snapshot(self):
        source = demo_snapshot("TP-001")
        source_id = save_quote("source", source)
        original = get_quote(source_id)
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(source_id)})
        app.run()
        click(app, "复制改价")
        self.assertEqual({field.label for field in app.number_input}, EXPECTED_FIELDS["flange"])
        number(app, "采购数量（件）").set_value(30).run()
        draft = html_blocks(app, "draft-changes")
        self.assertIn("<th scope=\"row\">数量</th>", draft)
        self.assertIn("20 件", draft)
        self.assertIn("30 件", draft)
        self.assertFalse(html_blocks(app, "quote-changes"))
        click(app, "确认参数并核价")
        for amount in ("4,729.79", "5,102.40", "7,094.68", "7,653.60"):
            self.assertIn(amount, html_blocks(app, "quote-changes"))
        app.slider(key="target_margin").set_value(18).run()
        self.assertIn("售价毛利率", html_blocks(app, "quote-changes"))
        self.assertIn("18 %", html_blocks(app, "quote-changes"))

        rule = {**steel_engine.load_material_rule("316L"), "base_price": 30.0, "tolerance": .93}
        with patch.object(steel_engine, "load_material_rule", return_value=rule):
            app.run()
            comparison = html_blocks(app, "quote-changes")
            self.assertIn("税前基准钢价", comparison)
            self.assertIn("30 元/kg", comparison)
            self.assertIn("下差系数", comparison)
            click(app, "保存询盘")
            new_id = app.session_state["quote_save"]["quote_id"]
            expected, _ = reprice({**source["inquiry"], "quantity": 30}, 18)
        new = get_quote(new_id)
        self.assertEqual(new["snapshot"]["quote"], expected)
        self.assertEqual(new["source_quote_id"], source_id)
        self.assertEqual(get_quote(source_id), original)
        history = AppTest.from_file(str(ROOT / "app.py"))
        history.query_params.update({"view": "records", "quote": str(new_id)})
        with patch.object(steel_engine, "load_material_rule", side_effect=AssertionError("history repriced")):
            history.run()
        self.assertFalse(history.exception)
        for _, field in QUOTE_OPTIONS:
            self.assertIn(f'{expected[field]:,.2f}', html_blocks(history, "quote-changes"))
        delete_quote(source_id)
        history.run()
        self.assertFalse(history.exception)
        self.assertTrue(any("来源报价数据不可用" in caption.value for caption in history.caption))
        self.assertEqual(get_quote(new_id), new)
        self.assertEqual(len(history.get("download_button")), 3)

    def test_unreadable_source_keeps_current_history_available(self):
        source_id = save_quote("source", demo_snapshot("TP-001"))
        current_id = save_quote("current", demo_snapshot("TP-001", quantity=30), source_id)
        current = get_quote(current_id)
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(current_id)})

        def read_record(quote_id):
            if quote_id == source_id:
                raise sqlite3.OperationalError("source unavailable fixture")
            return current

        with patch("quote_store.get_quote", side_effect=read_record):
            app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("来源报价数据不可用" in caption.value for caption in app.caption))
        self.assertEqual(len(app.get("download_button")), 3)

    def test_shape_change_compares_dimensions_by_their_meaning(self):
        original = demo_snapshot("TP-001")["inquiry"]
        current = {**original, "shape": "round_bar", "outer_dim_mm": 80.0, "thickness_mm": 450.0}
        changes = parameter_changes(original, current)
        self.assertIn(("零件形状", "法兰/圆片", "圆钢"), changes)
        self.assertIn(("外径（mm）", "280 mm", "—"), changes)
        self.assertIn(("直径（mm）", "—", "80 mm"), changes)
        self.assertIn(("板厚（mm）", "12 mm", "—"), changes)
        self.assertIn(("轴向长度（mm）", "—", "450 mm"), changes)
