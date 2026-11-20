import json
import os
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import reprice
from core.reply_parser import CHANGED, ReplyItem
from quote_store import (
    clear_work_draft, get_quote, get_quote_drawing, list_quotes, load_work_draft,
    save_quote, save_work_draft, validate_drawing_image,
)
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception, app.exception


def number(app, label):
    return next(field for field in app.number_input if field.label == label)


class WorkDraftTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = self.directory / "quotes.sqlite3"
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(self.database)}))

    def workbench(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        self.assertFalse(app.exception)
        return app

    def resume(self):
        before = load_work_draft()
        app = self.workbench()
        self.assertIn("继续上次询盘", [button.label for button in app.button])
        app.run()
        self.assertEqual(load_work_draft(), before)
        click(app, "继续上次询盘")
        self.assertNotIn("drawing_run", app.session_state)
        return app

    def two_items(self):
        app = self.workbench()
        for preset in ("PRESET_3_SCRAP_MATCH", "PRESET_1_FAST_QUOTE"):
            app.button(key=preset).click().run()
            click(app, "加入本单")
        return app, list(app.session_state["order_items"])

    def test_uploaded_pending_values_shapes_and_original_bytes_survive_new_session(self):
        app = self.workbench()
        data = (ROOT / "data/Test_Images/TP-001.png").read_bytes()
        app.file_uploader[0].upload("TP-001.png", data, "image/png").run()
        click(app, "识别图纸参数")
        number(app, "采购数量（件）").set_value(23).run()
        number(app, "板厚（mm）").set_value(9.0).run()
        next(field for field in app.selectbox if field.label == "零件形状").set_value("round_bar").run()
        number(app, "直径（mm）").set_value(66.0).run()
        number(app, "轴向长度（mm）").set_value(280.0).run()
        next(field for field in app.selectbox if field.label == "零件形状").set_value("flange").run()
        app.slider(key="target_margin").set_value(18).run()
        app = self.resume()
        self.assertEqual(app.session_state["inquiry_drawing"]["data"], data)
        self.assertEqual(number(app, "采购数量（件）").value, 23)
        self.assertEqual(number(app, "板厚（mm）").value, 9.0)
        self.assertEqual(number(app, "外径（mm）").value, 280.0)
        self.assertEqual(app.slider(key="target_margin").value, 18)
        self.assertFalse(any("报价" in button.label or "CSV" in button.label or "HTML" in button.label
                             for button in app.get("download_button")))
        self.assertNotIn("保存询盘", [button.label for button in app.button])
        next(field for field in app.selectbox if field.label == "零件形状").set_value("round_bar").run()
        self.assertEqual(number(app, "轴向长度（mm）").value, 280.0)
        next(field for field in app.selectbox if field.label == "零件形状").set_value("flange").run()
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "SUCCESS")
        self.assertTrue(app.session_state["drawing_run"]["demo"])
        click(app, "保存询盘")
        quote_id = app.session_state["quote_save"]["quote_id"]
        self.assertEqual(get_quote_drawing(quote_id)["data"], data)
        self.assertIsNone(load_work_draft())

    def test_two_confirmed_lines_and_third_pending_keep_independent_state(self):
        app, line_ids = self.two_items()
        original = json.loads(json.dumps(app.session_state["order_items"]))
        images = deepcopy(app.session_state["order_drawings"])
        # 不引入报价结果的下一件，也必须被单独留存。
        click(app, "新询盘")
        click(app, "手动录入")
        app.text_input[0].set_value("2205").run()
        next(field for field in app.selectbox if field.label == "零件形状").set_value("plate").run()
        for label, value in (("长度（mm）", 620.0), ("宽度（mm）", 370.0), ("采购数量（件）", 5)):
            number(app, label).set_value(value).run()
        customer = next(field for field in app.text_input if field.key.startswith("order_customer_") and field.label == "客户简称")
        customer.set_value("张老板").run()
        app = self.resume()
        self.assertEqual(list(app.session_state["order_items"]), line_ids)
        self.assertEqual(app.session_state["order_items"], original)
        self.assertEqual(app.session_state["order_drawings"], images)
        self.assertEqual(app.session_state["order_customer"]["fields"]["short_name"], "张老板")
        self.assertEqual(app.text_input[0].value, "2205")
        self.assertEqual(number(app, "长度（mm）").value, 620.0)
        self.assertIsNone(number(app, "板厚（mm）").value)
        click(app, "确认参数并核价")
        self.assertNotIn("drawing_run", app.session_state)
        self.assertEqual(app.session_state["order_items"], original)
        click(app, "保存本单")
        draft = load_work_draft()["payload"]
        self.assertIsNone(draft["order"])
        self.assertEqual(draft["inquiry"]["parameters"]["quantity"], 5)
        self.assertEqual(len(get_quote(app.session_state["order_save"]["quote_id"])["snapshot"]["items"]), 2)

    def test_editing_saved_second_line_restores_stable_id_and_waits_for_update(self):
        app, line_ids = self.two_items()
        click(app, "保存本单")
        original_id = app.session_state["order_save"]["quote_id"]
        original = get_quote(original_id)
        app.button(key=f"edit-order-{line_ids[1]}").click().run()
        number(app, "采购数量（件）").set_value(12).run()
        app = self.resume()
        self.assertEqual(app.session_state["order_edit"]["line_id"], line_ids[1])
        self.assertEqual(app.session_state.get("order_source_quote_id"), original["source_quote_id"])
        self.assertEqual(number(app, "采购数量（件）").value, 12)
        self.assertEqual(app.session_state["order_items"][line_ids[1]]["inquiry"]["quantity"], 8)
        # 移除前一项后，目标仍按 ID 对应原来的垫板。
        app.button(key=f"remove-order-{line_ids[0]}").click().run()
        click(app, "确认参数并核价")
        click(app, "更新本项")
        self.assertEqual(list(app.session_state["order_items"]), [line_ids[1]])
        new_line = app.session_state["order_items"][line_ids[1]]
        self.assertEqual(new_line["inquiry"]["quantity"], 12)
        self.assertEqual(new_line["quote"], reprice(new_line["inquiry"], 12)[0])
        self.assertNotIn("更新本项", [button.label for button in app.button])
        click(app, "保存本单")
        new = get_quote(app.session_state["order_save"]["quote_id"])
        self.assertEqual(new["source_quote_id"], original_id)
        self.assertEqual(get_quote(original_id), original)
        self.assertEqual(get_quote_drawing(new["id"])["filename"], "TP-002.png")
        self.assertIsNone(load_work_draft())

    def test_single_save_preserves_unsaved_order_and_order_save_clears_completed_work(self):
        app, line_ids = self.two_items()
        click(app, "保存询盘")
        record = get_quote(app.session_state["quote_save"]["quote_id"])
        draft = load_work_draft()["payload"]
        self.assertIsNone(draft["inquiry"])
        self.assertEqual([item["line_id"] for item in draft["order"]["items"]], line_ids)
        app = self.resume()
        self.assertNotIn("drawing_review", app.session_state)
        self.assertEqual(len(app.session_state["order_items"]), 2)
        click(app, "保存本单")
        self.assertIsNone(load_work_draft())
        self.assertEqual(get_quote(record["id"]), record)
        self.assertEqual(len(list_quotes()), 2)

    def test_removed_edit_target_and_discard_preserve_other_work_and_formal_quote(self):
        app, line_ids = self.two_items()
        click(app, "保存本单")
        quote_id = app.session_state["order_save"]["quote_id"]
        original, image = get_quote(quote_id), get_quote_drawing(quote_id)
        app.button(key=f"edit-order-{line_ids[1]}").click().run()
        number(app, "采购数量（件）").set_value(12).run()
        app.button(key=f"remove-order-{line_ids[1]}").click().run()
        app = self.resume()
        self.assertTrue(any("编辑目标已不存在" in warning.value for warning in app.warning))
        self.assertEqual(list(app.session_state["order_items"]), [line_ids[0]])
        self.assertEqual(number(app, "采购数量（件）").value, 12)
        click(app, "确认参数并核价")
        self.assertTrue(next(button for button in app.button if button.label == "更新本项").disabled)
        fresh = self.workbench()
        click(fresh, "放弃并新建")
        self.assertIsNone(load_work_draft())
        self.assertNotIn("drawing_review", fresh.session_state)
        self.assertNotIn("order_items", fresh.session_state)
        self.assertEqual(get_quote(quote_id), original)
        self.assertEqual(get_quote_drawing(quote_id), image)

    def test_callback_autosave_failure_retry_and_no_writes_on_plain_reruns(self):
        with patch("quote_store.save_work_draft", wraps=save_work_draft) as save:
            app = self.workbench()
            click(app, "手动录入")
            self.assertEqual(save.call_count, 1)
            app.run()
            app.run()
            self.assertEqual(save.call_count, 1)
        before = load_work_draft()
        with patch("quote_store.save_work_draft", side_effect=sqlite3.OperationalError("fixture unwritable")):
            app.text_input[0].set_value("316L").run()
        self.assertEqual(app.text_input[0].value, "316L")
        self.assertTrue(any("草稿尚未保存" in warning.value for warning in app.warning))
        self.assertEqual(load_work_draft(), before)
        click(app, "重试保存草稿")
        self.assertEqual(load_work_draft()["payload"]["inquiry"]["parameters"]["material"], "316L")
        self.assertTrue(any("草稿已保存" in element.proto.body for element in app.get("html")))

    def test_unrecognized_image_can_continue_recognition_after_restore(self):
        app = self.workbench()
        raw = (ROOT / "data/Test_Images/TP-001.png").read_bytes()
        app.file_uploader[0].upload("TP-001.png", raw, "image/png").run()
        app = self.resume()
        self.assertEqual(app.session_state["inquiry_drawing"]["data"], raw)
        click(app, "识别图纸参数")
        self.assertEqual(number(app, "采购数量（件）").value, 20)
        self.assertNotIn("drawing_run", app.session_state)
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["filename"], "TP-001.png")
        self.assertTrue(app.session_state["drawing_run"]["demo"])

    def test_manual_without_image_and_new_item_survive_formal_order_save(self):
        app, line_ids = self.two_items()
        click(app, "新询盘")
        click(app, "手动录入")
        app.text_input[0].set_value("304").run()
        number(app, "采购数量（件）").set_value(7).run()
        click(app, "保存本单")
        original = list_quotes()[0]
        self.assertIsNone(load_work_draft()["payload"]["order"])
        app = self.resume()
        self.assertEqual(number(app, "采购数量（件）").value, 7)
        self.assertIsNone(app.session_state.get("inquiry_drawing"))
        self.assertFalse(app.session_state.get("order_items"))
        self.assertEqual(get_quote(original["id"]), original)

    def test_applied_reply_customer_and_source_restore_without_secrets_or_reply_text(self):
        source = save_quote("source", demo_snapshot("TP-001"))
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(source)})
        app.run()
        click(app, "复制改价")
        reply = "改成30件，厚度不变；仅当前会话保留的原文"
        app.text_area[0].set_value(reply).run()
        app.session_state["api_key"] = "SENSITIVE-TEST-MARKER"
        app.session_state["drawing_review"]["raw_response"] = "SENSITIVE-TEST-MARKER"
        for label, value in (("客户简称", "张老板"), ("项目名称", "泵站法兰"), ("交期备注", "3日内自提")):
            next(field for field in app.text_input if field.label == label).set_value(value).run()
        app.checkbox[0].check().run()
        with patch("core.reply_parser.recognize_quote_reply", return_value=[
            ReplyItem("quantity", "数量", "20 件", "30 件", CHANGED, 30)
        ]):
            click(app, "识别修改项")
        click(app, "应用到草稿")
        encoded = json.dumps(load_work_draft()["payload"], ensure_ascii=False)
        self.assertNotIn(reply, encoded)
        self.assertNotIn("SENSITIVE-TEST-MARKER", encoded)
        app = self.resume()
        self.assertEqual(app.text_area[0].value, "")
        self.assertEqual(number(app, "采购数量（件）").value, 30)
        self.assertEqual(app.session_state["drawing_review"]["source_quote_id"], source)
        self.assertEqual(app.session_state["inquiry_customer"]["fields"]["project_name"], "泵站法兰")
        click(app, "确认参数并核价")
        self.assertNotIn("drawing_run", app.session_state)
        next(field for field in app.checkbox if field.label == "已重新核对客户改价要求").check().run()
        click(app, "确认参数并核价")
        click(app, "保存询盘")
        new = get_quote(app.session_state["quote_save"]["quote_id"])
        self.assertEqual(new["source_quote_id"], source)
        self.assertEqual(new["snapshot"]["inquiry"]["quantity"], 30)
        self.assertEqual(get_quote(source)["snapshot"]["inquiry"]["quantity"], 20)

    def test_draft_writes_are_atomic_deduplicated_and_leave_formal_images_untouched(self):
        app = self.workbench()
        click(app, "手动录入")
        draft = load_work_draft()
        with patch("quote_store.datetime") as clock:
            clock.now.side_effect = AssertionError("unchanged draft must not be rewritten")
            self.assertEqual(save_work_draft(draft["payload"]), draft["updated_at"])
        image_path = ROOT / "data/Test_Images/TP-001.png"
        image = validate_drawing_image(image_path.read_bytes(), image_path.name)
        quote_id = save_quote("formal", demo_snapshot("TP-001"), drawings={1: image})
        save_work_draft(draft["payload"], {"inquiry": image})
        stored = load_work_draft()
        with self.assertRaises(ValueError):
            save_work_draft(draft["payload"], {"inquiry": {"filename": "bad.png", "data": b"invalid"}})
        self.assertEqual(load_work_draft(), stored)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM work_draft").fetchone()[0], 1)
        clear_work_draft()
        self.assertIsNone(load_work_draft())
        self.assertEqual(get_quote_drawing(quote_id), image)


if __name__ == "__main__":
    unittest.main()
