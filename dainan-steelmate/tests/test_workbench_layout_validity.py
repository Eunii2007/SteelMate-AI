import os
import unittest
from copy import deepcopy
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import (
    DAINAN_TIME, QUOTE_OPTIONS, QUOTE_VALIDITY_TERM, build_order_snapshot,
    customer_quotation_markup, customer_quote_fields, customer_wechat_text,
    quotation_csv, quotation_markup, quote_valid_until,
)
from quote_store import get_quote, load_work_draft, save_quote, save_work_draft
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception, app.exception


def quote_html(app):
    return "\n".join(element.proto.body for element in app.get("html") if 'class="quote-card' in element.proto.body)


class WorkbenchLayoutValidityTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))

    def workbench(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        self.assertFalse(app.exception)
        return app

    def sample(self, app, preset="PRESET_3_SCRAP_MATCH"):
        app.button(key=preset).click().run()
        self.assertFalse(app.exception)

    def deadline(self, app, days, at=time(15, 30, 25)):
        date = (datetime.now(DAINAN_TIME) + timedelta(days=days)).date()
        app.date_input[0].set_value(date).run()
        app.time_input[0].set_value(at).run()
        self.assertFalse(app.exception)
        return f"{date.isoformat()} {at.isoformat()}"

    def resume(self):
        app = self.workbench()
        click(app, "继续上次询盘")
        self.assertNotIn("drawing_run", app.session_state)
        return app

    def test_inquiry_details_default_open_and_stock_is_top_dialog_only(self):
        app = self.workbench()
        self.sample(app)
        for label in ("处理详情", "内部核价明细"):
            panel = next(panel for panel in app.expander if panel.label == label)
            self.assertTrue(panel.proto.expanded)
        self.assertFalse(any(panel.label == "余料库" for panel in app.expander))
        before = deepcopy(app.session_state["drawing_run"])
        click(app, "余料库")
        self.assertTrue(app.get("dialog"))
        self.assertEqual(len(app.dataframe), 2)
        self.assertEqual(app.session_state["drawing_run"], before)
        click(app, "关闭余料库")
        self.assertEqual(app.session_state["drawing_run"], before)

    def test_custom_deadline_is_identical_in_snapshot_and_all_exports(self):
        app = self.workbench()
        self.sample(app)
        run = app.session_state["drawing_run"]
        self.assertEqual(run["valid_until"], quote_valid_until(run["issued"]))
        before = quote_html(app)
        expected = self.deadline(app, 2)
        self.assertEqual(quote_html(app), before)
        click(app, "保存询盘")
        snapshot = get_quote(app.session_state["quote_save"]["quote_id"])["snapshot"]
        self.assertEqual(snapshot["valid_until"], expected)
        self.assertEqual(snapshot["quote"][QUOTE_OPTIONS[0][1]], 4729.79)
        self.assertEqual(snapshot["quote"][QUOTE_OPTIONS[1][1]], 5102.40)
        self.assertIn(["有效期至（北京时间）", expected, ""], snapshot["rows"])
        fields = customer_quote_fields(snapshot)
        outputs = (
            quotation_csv(snapshot["rows"]).decode("utf-8-sig"), quotation_markup(snapshot["rows"]),
            customer_quotation_markup(fields), customer_wechat_text(fields),
        )
        for output in outputs:
            self.assertIn(expected, output)
            self.assertNotIn("自报价起7天", output)
            self.assertNotIn("报价自出具起 7 天内有效", output)
        self.assertIn(QUOTE_VALIDITY_TERM, outputs[1])
        self.assertIn(QUOTE_VALIDITY_TERM, outputs[2])

    def test_past_or_incomplete_deadline_blocks_delivery_without_clearing_prices(self):
        app = self.workbench()
        self.sample(app)
        before = quote_html(app)
        yesterday = (datetime.now(DAINAN_TIME) - timedelta(days=1)).date()
        app.date_input[0].set_value(yesterday).run()
        self.assertFalse(app.exception)
        self.assertFalse(any("复制并重新核价" in warning.value for warning in app.warning))
        for label in ("保存询盘", "加入本单"):
            self.assertTrue(next(button for button in app.button if button.label == label).disabled)
        self.assertFalse(any(button.label in ("下载客户报价单", "下载 CSV", "下载 HTML（可打印）")
                             for button in app.get("download_button")))
        self.assertFalse(any("复制微信报价" in element.proto.body for element in app.get("html")))
        self.assertEqual(quote_html(app), before)
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "SUCCESS")
        app.time_input[0].set_value(None).run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.time_input[0].value)
        self.assertIsNone(app.session_state["drawing_run"]["valid_until"])
        draft = load_work_draft()["payload"]["inquiry"]["validity"]
        self.assertEqual(draft["date"], yesterday.isoformat())
        self.assertIsNone(draft["time"])
        self.assertEqual(quote_html(app), before)

    def test_custom_and_incomplete_inputs_survive_draft_restore(self):
        app = self.workbench()
        self.sample(app)
        expected = self.deadline(app, 3)
        app = self.resume()
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["valid_until"], expected)
        app.time_input[0].set_value(None).run()
        date = app.date_input[0].value
        app = self.resume()
        click(app, "确认参数并核价")
        self.assertEqual(app.date_input[0].value, date)
        self.assertIsNone(app.time_input[0].value)
        self.assertTrue(next(button for button in app.button if button.label == "保存询盘").disabled)

    def test_legacy_draft_without_validity_gets_fresh_default_after_confirmation(self):
        app = self.workbench()
        self.sample(app)
        self.deadline(app, 2)
        stored = load_work_draft()
        stored["payload"]["inquiry"].pop("validity")
        save_work_draft(stored["payload"], stored["drawings"])
        app = self.resume()
        click(app, "确认参数并核价")
        run = app.session_state["drawing_run"]
        self.assertEqual(run["valid_until"], quote_valid_until(run["issued"]))

    def test_saved_deadline_is_immutable_and_copy_uses_new_default(self):
        app = self.workbench()
        self.sample(app)
        original_deadline = self.deadline(app, 2)
        click(app, "保存询盘")
        original_id = app.session_state["quote_save"]["quote_id"]
        original = get_quote(original_id)
        new_deadline = self.deadline(app, 4)
        click(app, "保存询盘")
        new_id = app.session_state["quote_save"]["quote_id"]
        self.assertNotEqual(new_id, original_id)
        self.assertEqual(get_quote(new_id)["source_quote_id"], original_id)
        self.assertEqual(get_quote(new_id)["snapshot"]["valid_until"], new_deadline)
        self.assertEqual(get_quote(original_id), original)
        history = AppTest.from_file(str(ROOT / "app.py"))
        history.query_params.update({"view": "records", "quote": str(original_id)})
        history.run()
        self.assertFalse(history.exception)
        self.assertFalse(history.date_input)
        self.assertIn(original_deadline, "\n".join(element.proto.body for element in history.get("html")))
        click(history, "复制改价")
        self.assertNotIn("drawing_run", history.session_state)
        click(history, "确认参数并核价")
        run = history.session_state["drawing_run"]
        self.assertEqual(run["valid_until"], quote_valid_until(run["issued"]))
        self.assertEqual(get_quote(original_id), original)

    def test_order_earliest_deadline_and_line_edit_preserve_last_confirmed_values(self):
        app = self.workbench()
        self.sample(app)
        first_deadline = self.deadline(app, 12)
        click(app, "加入本单")
        self.sample(app, "PRESET_1_FAST_QUOTE")
        click(app, "加入本单")
        line_ids = list(app.session_state["order_items"])
        original = deepcopy(app.session_state["order_items"])
        snapshot = build_order_snapshot(list(original.values()), app.session_state["order_issued"])
        self.assertEqual(snapshot["valid_until"], original[line_ids[1]]["valid_until"])
        app.button(key=f"edit-order-{line_ids[0]}").click().run()
        click(app, "确认参数并核价")
        self.assertEqual(app.session_state["drawing_run"]["valid_until"], first_deadline)
        app.date_input[0].set_value((datetime.now(DAINAN_TIME) - timedelta(days=1)).date()).run()
        self.assertTrue(next(button for button in app.button if button.label == "更新本项").disabled)
        self.assertEqual(app.session_state["order_items"], original)
        expected = self.deadline(app, 1)
        click(app, "更新本项")
        current = app.session_state["order_items"]
        self.assertEqual(list(current), line_ids)
        self.assertEqual(current[line_ids[1]], original[line_ids[1]])
        self.assertEqual(current[line_ids[0]]["valid_until"], expected)
        click(app, "保存本单")
        record = get_quote(app.session_state["order_save"]["quote_id"])
        self.assertEqual(record["snapshot"]["valid_until"], expected)
        self.assertEqual(record["snapshot"]["totals"], {QUOTE_OPTIONS[0][1]: "6261.21", QUOTE_OPTIONS[1][1]: "6746.33"})

    def test_history_missing_deadline_stays_unknown(self):
        snapshot = demo_snapshot("TP-001")
        snapshot.pop("valid_until")
        quote_id = save_quote("legacy-without-deadline", snapshot)
        original = get_quote(quote_id)
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(quote_id)})
        app.run()
        self.assertFalse(app.exception)
        self.assertIn("有效期待核对", "\n".join(element.proto.body for element in app.get("html")))
        self.assertEqual(get_quote(quote_id), original)


if __name__ == "__main__":
    unittest.main()
