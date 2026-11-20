import os
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import build_order_snapshot, customer_order_fields, customer_order_markup, customer_order_wechat_text, reprice
from core import steel_engine
from quote_store import get_quote, get_quote_drawing, get_scrap_review, list_quotes, save_scrap_review


ROOT = Path(__file__).resolve().parents[1]


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()


def number(app, label):
    return next(field for field in app.number_input if field.label == label)


def order_snapshot(app):
    return build_order_snapshot(list(app.session_state["order_items"].values()), app.session_state["order_issued"])


class OrderItemEditTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_STORAGE_MODE": "single_user", "DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))

    def two_items(self, *, pad_margin=12):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        if any(button.label == "放弃并新建" for button in app.button):
            click(app, "放弃并新建")
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        click(app, "加入本单")
        app.button(key="PRESET_1_FAST_QUOTE").click().run()
        if pad_margin != 12:
            app.slider(key="target_margin").set_value(pad_margin).run()
        click(app, "加入本单")
        self.assertFalse(app.exception)
        return app, list(app.session_state["order_items"])

    def edit(self, app, line_id):
        app.button(key=f"edit-order-{line_id}").click().run()
        self.assertNotIn("drawing_run", app.session_state)
        self.assertNotIn("更新本项", [button.label for button in app.button])

    def history(self, quote_id):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(quote_id)})
        return app.run()

    def test_confirm_then_replace_at_original_id_and_position(self):
        app, line_ids = self.two_items()
        before = deepcopy(app.session_state["order_items"])
        pictures = deepcopy(app.session_state["order_drawings"])
        self.edit(app, line_ids[1])
        number(app, "采购数量（件）").set_value(12).run()
        self.assertEqual(app.session_state["order_items"], before)
        self.assertEqual(order_snapshot(app)["totals"], {
            "方案A_理计不含税自提价_元": "6261.21", "方案B_过磅含13专票价_元": "6746.33",
        })
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["order_items"], before)
        self.assertNotIn("加入本单", [button.label for button in app.button])
        click(app, "更新本项")
        self.assertFalse(app.exception)
        self.assertNotIn("order_edit", app.session_state)
        self.assertEqual(list(app.session_state["order_items"]), line_ids)
        self.assertEqual(app.session_state["order_items"][line_ids[0]], before[line_ids[0]])
        updated = app.session_state["order_items"][line_ids[1]]
        expected, _ = reprice({**before[line_ids[1]]["inquiry"], "quantity": 12}, 12)
        self.assertEqual(updated["quote"], expected)
        self.assertEqual(app.session_state["order_drawings"], pictures)
        snapshot = order_snapshot(app)
        self.assertEqual(snapshot["totals"], {
            "方案A_理计不含税自提价_元": "7026.92", "方案B_过磅含13专票价_元": "7568.30",
        })
        self.assertEqual(snapshot["valid_until"], min(item["valid_until"] for item in snapshot["items"]))

    def test_cancel_and_missing_parameters_keep_last_confirmed_order(self):
        app, line_ids = self.two_items()
        click(app, "保存本单")
        saved = deepcopy(app.session_state["order_save"])
        before = deepcopy(app.session_state["order_items"])
        self.edit(app, line_ids[1])
        number(app, "采购数量（件）").set_value(12).run()
        number(app, "板厚（mm）").set_value(0.0).run()
        click(app, "确认参数并核价")
        self.assertNotIn("drawing_run", app.session_state)
        self.assertNotIn("更新本项", [button.label for button in app.button])
        self.assertEqual(app.session_state["order_items"], before)
        self.assertEqual(app.session_state["order_save"], saved)
        output = customer_order_wechat_text(customer_order_fields(order_snapshot(app)))
        self.assertIn("8 件", output)
        self.assertNotIn("12 件", output)
        self.assertIn("6,261.21", output)
        click(app, "取消编辑")
        self.assertEqual(app.session_state["order_items"], before)
        self.assertNotIn("order_edit", app.session_state)
        self.assertNotIn("drawing_run", app.session_state)
        self.assertEqual(app.session_state["order_save"], saved)

    def test_other_row_removal_does_not_change_target_and_removed_target_cannot_update(self):
        app, line_ids = self.two_items()
        self.edit(app, line_ids[1])
        app.button(key=f"remove-order-{line_ids[0]}").click().run()
        self.assertEqual(app.session_state["order_edit"]["line_id"], line_ids[1])
        number(app, "采购数量（件）").set_value(12).run()
        click(app, "确认参数并核价")
        click(app, "更新本项")
        self.assertEqual(list(app.session_state["order_items"]), [line_ids[1]])
        self.assertEqual(app.session_state["order_items"][line_ids[1]]["inquiry"]["quantity"], 12)
        app, line_ids = self.two_items()
        first = deepcopy(app.session_state["order_items"][line_ids[0]])
        self.edit(app, line_ids[1])
        app.button(key=f"remove-order-{line_ids[1]}").click().run()
        self.assertTrue(any("编辑目标已不存在" in warning.value for warning in app.warning))
        number(app, "采购数量（件）").set_value(12).run()
        click(app, "确认参数并核价")
        self.assertTrue(next(button for button in app.button if button.label == "更新本项").disabled)
        self.assertEqual(app.session_state["order_items"], {line_ids[0]: first})
        click(app, "取消编辑")
        self.assertEqual(app.session_state["order_items"], {line_ids[0]: first})

    def test_duplicate_and_late_update_callbacks_do_not_append_or_overwrite(self):
        app = AppTest.from_string(dedent('''
            import streamlit as st
            from copy import deepcopy
            from app import edit_order_item, update_order_item
            from test_scrap_reviews import demo_snapshot
            old = demo_snapshot("TP-002")
            new = demo_snapshot("TP-002", quantity=12)
            st.session_state.order_items = {"flange": demo_snapshot("TP-001"), "pad": old}
            st.session_state.order_edit = {"line_id": "pad", "token": "edit-one", "filename": "TP-002.png"}
            st.session_state.drawing_run = {"run_id": "run-one", "order_edit_token": "edit-one", "result": {"status": "SUCCESS"}}
            st.session_state.drawing_review = {"confirmed": new["inquiry"]}
            st.session_state.target_margin = 12
            results = [update_order_item("pad", "edit-one", "run-one", new, None)]
            confirmed = deepcopy(st.session_state.order_items)
            results.append(update_order_item("pad", "edit-one", "run-one", new, None))
            edit_order_item("flange")
            current_token = st.session_state.order_edit["token"]
            results.append(update_order_item("pad", "edit-one", "run-one", old, None))
            assert st.session_state.order_edit["token"] == current_token
            assert st.session_state.order_items == confirmed
            assert list(st.session_state.order_items) == ["flange", "pad"]
            st.session_state.results = results
        ''')).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["results"], [True, False, False])

    def test_late_customer_callback_after_last_row_removed_cannot_recreate_old_customer(self):
        app = AppTest.from_string(dedent('''
            import streamlit as st
            from app import customer_info_changed
            from quote_store import clean_customer_info
            st.session_state.order_customer_revision = 1
            st.session_state["order_customer_0_short_name"] = "旧客户"
            customer_info_changed("order_customer", "order_customer_0", "short_name")
            assert "order_customer" not in st.session_state
            st.session_state.order_customer = {"fields": clean_customer_info({"short_name": "新客户"}), "delivery_confirmed": False}
            customer_info_changed("order_customer", "order_customer_0", "short_name")
            assert st.session_state.order_customer["fields"]["short_name"] == "新客户"
            st.session_state["order_customer_1_short_name"] = "当前客户"
            customer_info_changed("order_customer", "order_customer_1", "short_name")
            assert st.session_state.order_customer["fields"]["short_name"] == "当前客户"
        ''')).run()
        self.assertFalse(app.exception)

    def test_saved_versions_keep_customer_images_validity_and_reviews_independent(self):
        app, line_ids = self.two_items()
        for label, value in (("客户简称", "张老板"), ("项目名称", "法兰与垫板")):
            next(field for field in app.text_input if field.label == label and field.key.startswith("order_customer_")).set_value(value).run()
        click(app, "保存本单")
        old_id = app.session_state["order_save"]["quote_id"]
        original = get_quote(old_id)
        pictures = [get_quote_drawing(old_id, position) for position in (1, 2)]
        save_scrap_review(old_id, 1, "确认可供", 14, note="模拟回填，仅用于验收")
        review = get_scrap_review(old_id, 1)
        self.edit(app, line_ids[1])
        number(app, "采购数量（件）").set_value(12).run()
        click(app, "确认参数并核价")
        click(app, "更新本项")
        click(app, "保存本单")
        new_id = app.session_state["order_save"]["quote_id"]
        new = get_quote(new_id)
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(new["source_quote_id"], old_id)
        self.assertEqual(new["snapshot"]["customer"], original["snapshot"]["customer"])
        self.assertEqual(get_quote(old_id), original)
        self.assertEqual(get_scrap_review(old_id, 1), review)
        self.assertIsNone(get_scrap_review(new_id, 1))
        self.assertIsNone(get_scrap_review(new_id, 2))
        for quote_id, pad_quantity, amount in ((old_id, 8, "6,261.21"), (new_id, 12, "7,026.92")):
            record = get_quote(quote_id)
            for position in (1, 2):
                self.assertEqual(get_quote_drawing(quote_id, position), pictures[position - 1])
            self.assertEqual(record["snapshot"]["items"][1]["inquiry"]["quantity"], pad_quantity)
            fields = customer_order_fields(record["snapshot"], quote_id)
            for output in (customer_order_wechat_text(fields), customer_order_markup(fields)):
                self.assertIn(amount, output)
                self.assertIn(record["snapshot"]["valid_until"], output)
                for internal in ("order_edit", "待更新", "模拟回填", "DN-001"):
                    self.assertNotIn(internal, output)
            with patch.object(steel_engine, "load_material_rule", side_effect=AssertionError("history repriced")):
                history = self.history(quote_id)
            self.assertFalse(history.exception)
        self.assertEqual(len(list_quotes()), 2)

    def test_recognition_failure_keeps_order_and_successful_replacement_updates_picture(self):
        app, line_ids = self.two_items()
        before = deepcopy(app.session_state["order_items"])
        before_images = deepcopy(app.session_state["order_drawings"])
        self.edit(app, line_ids[1])
        data = (ROOT / "data/Test_Images/TP-001.png").read_bytes()
        app.file_uploader[0].upload("new-drawing.png", data, "image/png").run()
        with patch("core.vision_parser.recognize_new_drawing", side_effect=ValueError("模拟接口超时")):
            click(app, "识别图纸参数")
        self.assertTrue(any("模拟接口超时" in warning.value for warning in app.warning))
        self.assertEqual(app.session_state["order_items"], before)
        self.assertEqual(app.session_state["order_drawings"], before_images)
        self.assertNotIn("drawing_run", app.session_state)
        self.assertNotIn("更新本项", [button.label for button in app.button])
        click(app, "取消编辑")
        self.edit(app, line_ids[1])
        app.file_uploader[0].upload("TP-001.png", data, "image/png").run()
        click(app, "识别图纸参数")
        click(app, "确认参数并核价")
        click(app, "更新本项")
        self.assertEqual(list(app.session_state["order_items"]), line_ids)
        self.assertEqual(app.session_state["order_drawings"][line_ids[1]]["data"], data)
        self.assertEqual(app.session_state["order_items"][line_ids[0]], before[line_ids[0]])

    def test_edit_prefills_the_line_margin_and_dimensions(self):
        app, line_ids = self.two_items(pad_margin=18)
        original = deepcopy(app.session_state["order_items"])
        app.slider(key="target_margin").set_value(12).run()
        self.edit(app, line_ids[1])
        self.assertEqual(app.slider(key="target_margin").value, 18)
        self.assertEqual(app.text_input[0].value, "304")
        self.assertEqual(next(field for field in app.selectbox if field.label == "零件形状").value, "plate")
        for label, value in (("长度（mm）",1200),("宽度（mm）",600),("板厚（mm）",2),("采购数量（件）",8)):
            self.assertEqual(number(app, label).value, value)
        self.assertEqual(app.session_state["inquiry_drawing"]["filename"], "TP-002.png")
        click(app, "取消编辑")
        self.assertEqual(app.session_state["order_items"], original)
