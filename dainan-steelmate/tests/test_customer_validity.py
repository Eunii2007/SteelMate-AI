import json
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import (
    build_order_snapshot, customer_order_fields, customer_order_markup, customer_order_wechat_text,
    customer_quotation_markup, customer_quote_fields, customer_wechat_text, quote_rows,
)
from core import steel_engine
from quote_store import CUSTOMER_INFO_FIELDS, clean_customer_info, get_quote, list_quotes, quote_deadline_text, quote_validity, save_quote
from test_scrap_reviews import demo_snapshot


ROOT = Path(__file__).resolve().parents[1]
CUSTOMER = {"short_name": "张老板", "project_name": "泵站法兰", "delivery_note": "图纸确认后 7 天交付"}


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()


def text_input(app, label, prefix=""):
    return next(field for field in app.text_input if field.label == label and (field.key or "").startswith(prefix))


class CustomerValidityTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = self.directory / "quotes.sqlite3"
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(self.database)}))

    def open_record(self, quote_id):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params.update({"view": "records", "quote": str(quote_id)})
        return app.run()

    def test_customer_round_trip_search_and_customer_whitelist(self):
        snapshot = {**demo_snapshot("TP-001"), "customer": CUSTOMER}
        quote_id = save_quote("customer", snapshot)
        original = get_quote(quote_id)
        with closing(sqlite3.connect(self.database)) as reopened:
            payload, = reopened.execute("SELECT snapshot FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        self.assertEqual(json.loads(payload)["customer"], CUSTOMER)
        save_quote("legacy", demo_snapshot("TP-002"))
        for search in ("张老板", "泵站法兰"):
            self.assertEqual(list_quotes(search), [original])
        self.assertEqual(list_quotes("' OR 1=1 --"), [])
        fields = customer_quote_fields(snapshot, quote_id)
        outputs = customer_wechat_text(fields) + customer_quotation_markup(fields)
        for value in CUSTOMER.values():
            self.assertIn(value, outputs)
        for internal in ("售价毛利率", "DN-001", "余料供应方", "760.37"):
            self.assertNotIn(internal, outputs)
        self.assertEqual(get_quote(quote_id), original)

    def test_customer_validation_and_html_escaping(self):
        for field, (_, limit) in CUSTOMER_INFO_FIELDS.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                save_quote(field, {**demo_snapshot("TP-001"), "customer": {field: "字" * (limit + 1)}})
        for invalid in ({"short_name": 42}, {"delivery_note": "明天\n立即付款"}, "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                clean_customer_info(invalid)
        customer = {**CUSTOMER, "short_name": "<script>alert(1)</script>", "private": "do-not-export"}
        fields = customer_quote_fields({**demo_snapshot("TP-001"), "customer": customer})
        markup = customer_quotation_markup(fields)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)
        self.assertNotIn("do-not-export", markup)
        self.assertEqual(clean_customer_info({"short_name": " 张老板 "})["short_name"], "张老板")

    def test_copy_keeps_independent_customer_information_and_requires_note_confirmation(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        for field, (label, _) in CUSTOMER_INFO_FIELDS.items():
            text_input(app, label).set_value(CUSTOMER[field]).run()
        self.assertTrue(next(button for button in app.button if button.label == "保存询盘").disabled)
        self.assertFalse(any(button.label == "下载客户报价单" for button in app.get("download_button")))
        next(box for box in app.checkbox if box.label == "已核对交期备注").check().run()
        click(app, "保存询盘")
        original_id = app.session_state["quote_save"]["quote_id"]
        original = get_quote(original_id)
        self.assertEqual(original["snapshot"]["customer"], CUSTOMER)
        history = self.open_record(original_id)
        click(history, "复制改价")
        for field, (label, _) in CUSTOMER_INFO_FIELDS.items():
            self.assertEqual(text_input(history, label).value, CUSTOMER[field])
        self.assertNotIn("drawing_run", history.session_state)
        self.assertFalse(next(box for box in history.checkbox if box.label == "已核对交期备注").value)
        text_input(history, "客户简称").set_value("张工").run()
        text_input(history, "项目名称").set_value("泵站二期").run()
        text_input(history, "交期备注").set_value("双方确认后 10 天").run()
        click(history, "确认参数并核价")
        self.assertTrue(next(button for button in history.button if button.label == "保存询盘").disabled)
        next(box for box in history.checkbox if box.label == "已核对交期备注").check().run()
        click(history, "保存询盘")
        new_id = history.session_state["quote_save"]["quote_id"]
        self.assertNotEqual(new_id, original_id)
        self.assertEqual(get_quote(new_id)["source_quote_id"], original_id)
        self.assertEqual(get_quote(new_id)["snapshot"]["customer"]["project_name"], "泵站二期")
        self.assertEqual(get_quote(original_id), original)
        self.assertFalse(history.exception)

    def test_order_uses_its_own_customer_information_once(self):
        first = {**demo_snapshot("TP-001"), "customer": {**CUSTOMER, "short_name": "单件客户"}}
        order = build_order_snapshot([first, demo_snapshot("TP-002")], first["issued"])
        order["customer"] = CUSTOMER
        quote_id = save_quote("order", order)
        fields = customer_order_fields(get_quote(quote_id)["snapshot"], quote_id)
        for output in (customer_order_wechat_text(fields), customer_order_markup(fields)):
            for value in CUSTOMER.values():
                self.assertEqual(output.count(value), 1)
            self.assertNotIn("单件客户", output)
            self.assertIn("6,261.21", output)
            self.assertIn("6,746.33", output)
        history = self.open_record(quote_id)
        self.assertFalse(history.exception)
        self.assertEqual(get_quote(quote_id)["snapshot"], json.loads(json.dumps(order)))

    def test_beijing_expiry_boundary_and_legacy_date_formats(self):
        beijing = timezone(timedelta(hours=8))
        now = datetime(2026, 11, 18, 12, 0, tzinfo=beijing)
        cases = (
            ("2026-11-18 12:00:00.000001", "有效期内"),
            ("2026-11-18 12:00:00", "已过期"),
            ("2026-11-18 11:59:59", "已过期"),
            ("2026-11-18T04:00:00Z", "已过期"),
            ("2026-11-18T12:00:01+08:00", "有效期内"),
            ("2026/11/18 12:00:01", "有效期内"),
            ("2026/11/18 11:59", "已过期"),
            ("2026年11月18日 12:00:00", "已过期"),
            ("2026-11-18 12:00:00（北京时间）", "已过期"),
            ("2026-11-18", "有效期内"),
            ("2026/11/18", "有效期内"),
            ("2026年11月18日", "有效期内"),
            (None, "有效期待核对"), ("", "有效期待核对"),
            ("2026-02-30", "有效期待核对"), ("日期不详", "有效期待核对"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(quote_validity(value, now), expected)
        self.assertEqual(quote_validity("2026-11-18 12:00:00", now.replace(tzinfo=None)), "已过期")
        self.assertEqual(quote_validity("2026-11-18 12:00:00", now.astimezone(timezone.utc)), "已过期")
        self.assertEqual(quote_validity("2026-11-18", datetime(2026, 11, 19, tzinfo=beijing)), "已过期")
        self.assertEqual(quote_deadline_text("2026-11-18T04:00:00Z"), "2026-11-18 12:00:00")
        self.assertEqual(quote_deadline_text("2026/11/18"), "2026/11/18")
        self.assertEqual(quote_deadline_text(None), "未记录")

    def test_old_history_expiry_is_display_only_and_copy_generates_new_validity(self):
        for value, status in (("2099/11/18 12:00:00", "有效期内"), ("2000-01-08 10:00:00", "已过期"),
                              (None, "有效期待核对"), ("日期不详", "有效期待核对")):
            with self.subTest(value=value):
                snapshot = demo_snapshot("TP-001")
                snapshot["issued"] = "2000-01-01 10:00:00"
                snapshot["rows"] = quote_rows(snapshot, snapshot["quote"], snapshot["scrap"], .95, 12)
                if value is None:
                    snapshot.pop("valid_until")
                else:
                    snapshot["valid_until"] = value
                quote_id = save_quote(f"legacy:{value}", snapshot)
                original = get_quote(quote_id)
                with patch.object(steel_engine, "load_material_rule", side_effect=AssertionError("history repriced")):
                    app = self.open_record(quote_id)
                self.assertFalse(app.exception)
                markup = "\n".join(item.proto.body for item in app.get("html"))
                self.assertIn(status, markup)
                fields = customer_quote_fields(original["snapshot"], quote_id)
                outputs = customer_wechat_text(fields) + customer_quotation_markup(fields)
                self.assertIn("4,729.79", outputs)
                if status == "已过期":
                    self.assertIn("已过期，仅供历史参考", outputs)
                    click(app, "复制并重新核价")
                    self.assertNotIn("drawing_run", app.session_state)
                    click(app, "确认参数并核价")
                    run = app.session_state["drawing_run"]
                    self.assertGreater(run["issued"], snapshot["issued"])
                    click(app, "保存询盘")
                    revised = get_quote(app.session_state["quote_save"]["quote_id"])["snapshot"]
                    self.assertEqual(quote_validity(revised["valid_until"]), "有效期内")
                elif status == "有效期待核对":
                    self.assertIn("有效期待核对", outputs)
                else:
                    self.assertNotIn("已过期，仅供历史参考", outputs)
                self.assertEqual(get_quote(quote_id), original)

    def test_expired_order_copies_selected_line_and_its_quote_baseline(self):
        first, second = demo_snapshot("TP-001"), demo_snapshot("TP-002")
        for item in (first, second):
            item.update(issued="2000-01-01 10:00:00", valid_until="2000-01-08 10:00:00")
            item["rows"] = quote_rows(item, item["quote"], item["scrap"], item["tolerance"], 12)
        order = build_order_snapshot([first, second], first["issued"])
        order["customer"] = CUSTOMER
        quote_id = save_quote("expired-order", order)
        original = get_quote(quote_id)
        fields = customer_order_fields(original["snapshot"], quote_id)
        self.assertIn("已过期，仅供历史参考", customer_order_wechat_text(fields))
        self.assertIn("已过期，仅供历史参考", customer_order_markup(fields))
        app = self.open_record(quote_id)
        next(field for field in app.selectbox if field.label == "选择重新核价的明细").set_value(2).run()
        click(app, "复制并重新核价")
        self.assertNotIn("drawing_run", app.session_state)
        self.assertEqual(text_input(app, "客户简称").value, CUSTOMER["short_name"])
        self.assertEqual(next(field for field in app.number_input if field.label == "长度（mm）").value, 1200)
        click(app, "确认参数并核价")
        self.assertFalse(app.exception)
        comparison = "\n".join(item.proto.body for item in app.get("html") if "quote-changes" in item.proto.body)
        self.assertIn("1,531.42", comparison)
        self.assertNotIn("4,729.79", comparison)
        next(box for box in app.checkbox if box.label == "已核对交期备注").check().run()
        click(app, "保存询盘")
        revised = get_quote(app.session_state["quote_save"]["quote_id"])
        self.assertEqual(revised["source_quote_id"], quote_id)
        self.assertEqual(revised["snapshot"]["source_line_position"], 2)
        self.assertEqual(quote_validity(revised["snapshot"]["valid_until"]), "有效期内")
        self.assertEqual(get_quote(quote_id), original)
        self.assertFalse(self.open_record(revised["id"]).exception)

    def test_order_editor_saves_whole_quote_metadata_and_repeated_save_is_idempotent(self):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = "workbench"
        app.run()
        for preset in ("PRESET_3_SCRAP_MATCH", "PRESET_1_FAST_QUOTE"):
            app.button(key=preset).click().run()
            click(app, "加入本单")
        for field, (label, _) in CUSTOMER_INFO_FIELDS.items():
            text_input(app, label, "order_customer_").set_value(CUSTOMER[field]).run()
        self.assertTrue(next(button for button in app.button if button.label == "保存本单").disabled)
        next(box for box in app.checkbox if box.label == "已核对交期备注" and box.key.startswith("order_customer_")).check().run()
        click(app, "保存本单")
        quote_id = app.session_state["order_save"]["quote_id"]
        saved = get_quote(quote_id)
        self.assertEqual(saved["snapshot"]["customer"], CUSTOMER)
        self.assertEqual(save_quote(app.session_state["order_save"]["save_key"], saved["snapshot"]), quote_id)
        self.assertEqual(len(list_quotes()), 1)
        self.assertEqual(saved["snapshot"]["valid_until"], min(item["valid_until"] for item in saved["snapshot"]["items"]))
        self.assertFalse(app.exception)
