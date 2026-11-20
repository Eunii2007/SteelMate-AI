import hashlib
import json
import os
import secrets
import sqlite3
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app import customer_quote_fields, customer_quotation_markup, customer_wechat_text, quotation_csv
from quote_store import (
    clear_work_draft, database_path, delete_quote, get_quote, get_quote_drawing, get_scrap_review,
    list_quotes, load_work_draft, save_quote, save_scrap_review, save_work_draft, validate_drawing_image,
)
from test_scrap_reviews import demo_snapshot
from visitor_context import (
    COOKIE_DAYS, COOKIE_KEY, VisitorAccessError, make_access, new_credential,
    sync_session_owner, verify_credential, visitor_scope,
)


ROOT = Path(__file__).resolve().parents[1]


class VisitorIsolationTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.directory / "visitors"
        self.key = secrets.token_urlsafe(48)
        self.a, self.b = new_credential(self.key), new_credential(self.key)
        self.shared = self.directory / "legacy.sqlite3"
        # A real pre-existing shared database is a sentinel, never a fallback.
        with closing(sqlite3.connect(self.shared)) as connection, connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('legacy-business-data')")
        self.shared_hash = hashlib.sha256(self.shared.read_bytes()).digest()
        self.enterContext(patch.dict(os.environ, {
            "DAINAN_STORAGE_MODE": "visitor", "DAINAN_VISITOR_SIGNING_KEY": self.key,
            "DAINAN_VISITOR_DATA_ROOT": str(self.root), "DAINAN_QUOTE_DB": str(self.shared),
            "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "VISION_MODEL": "",
        }))
        self.snapshot = json.loads(json.dumps(demo_snapshot("TP-001")))
        self.snapshot["customer"] = {"short_name": "访客 A", "project_name": "A 的询盘", "delivery_note": ""}
        self.drawing = validate_drawing_image((ROOT / "data/Test_Images/TP-001.png").read_bytes(), "A-原图.png")

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.shared.read_bytes()).digest(), self.shared_hash)

    def scope(self, credential):
        return visitor_scope(credential, self.key, self.root)

    def app(self, credential=None, *, view="records", quote=None):
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.query_params["view"] = view
        if quote is not None:
            app.query_params["quote"] = str(quote)
        if credential is not None:
            app.session_state[COOKIE_KEY] = {"observation": {"credential": credential, "available": True}}
        app.run()
        self.assertFalse(app.exception, app.exception)
        return app

    def test_history_search_detail_image_export_copy_delete_are_scoped(self):
        with self.scope(self.a):
            a_id = save_quote("a", self.snapshot, drawings={1: self.drawing})
            self.assertEqual(len(list_quotes("A 的询盘")), 1)
        with self.scope(self.b):
            self.assertEqual(list_quotes(), [])
            self.assertEqual(list_quotes("A 的询盘"), [])
            self.assertIsNone(get_quote(a_id))
            self.assertIsNone(get_quote_drawing(a_id))
            self.assertFalse(delete_quote(a_id))
            with self.assertRaises(ValueError):
                save_scrap_review(a_id, 1, "无法供料")
        # Direct detail links receive no record, so no copy or export entrance.
        app = self.app(self.b, quote=a_id)
        self.assertTrue(any("找不到这条报价" in message.value for message in app.warning))
        self.assertNotIn("复制改价", [button.label for button in app.button])
        self.assertFalse(app.get("download_button"))
        self.assertFalse(any("data:image" in element.proto.body for element in app.get("html")))
        with self.scope(self.a):
            record = get_quote(a_id)
            fields = customer_quote_fields(record["snapshot"], a_id)
            self.assertIn("访客 A", customer_wechat_text(fields))
            self.assertIn("访客 A", customer_quotation_markup(fields))
            self.assertIn("4,729.79", customer_quotation_markup(fields))
            self.assertTrue(quotation_csv(record["snapshot"]["rows"]))
            self.assertEqual(get_quote_drawing(a_id)["data"], self.drawing["data"])

    def test_identical_numeric_ids_never_select_other_visitors_snapshots(self):
        with self.scope(self.a):
            a_id = save_quote("same-save-key", self.snapshot, drawings={1: self.drawing})
        other = deepcopy(self.snapshot)
        other["filename"] = "B-自己的询盘.png"
        other["customer"]["short_name"] = "访客 B"
        with self.scope(self.b):
            b_id = save_quote("same-save-key", other)
            self.assertEqual(a_id, b_id)
            self.assertEqual(get_quote(a_id)["snapshot"], other)
            self.assertIsNone(get_quote_drawing(a_id))
            save_scrap_review(b_id, 1, "无法供料")
            self.assertTrue(delete_quote(a_id))
        with self.scope(self.a):
            self.assertEqual(get_quote(a_id)["snapshot"], self.snapshot)
            self.assertIsNone(get_scrap_review(a_id, 1))

    def test_work_drafts_order_snapshots_drawings_reviews_and_copy_versions_are_independent(self):
        order = {"kind": "order", "filename": "多件报价", "issued": self.snapshot["issued"],
                 "items": [self.snapshot, json.loads(json.dumps(demo_snapshot("TP-002")))]}
        draft = {"version": 1, "inquiry": {"filename": "A-未完成.png"},
                 "order": {"items": [{"line_id": "stable-line", "snapshot": self.snapshot}]}}
        with self.scope(self.a):
            a_id = save_quote("order", order, drawings={1: self.drawing})
            save_work_draft(draft, {"inquiry": self.drawing, "line:stable-line": self.drawing})
            save_scrap_review(a_id, 1, "确认可供", 14, note="模拟回填")
        with self.scope(self.b):
            self.assertIsNone(load_work_draft())
            self.assertIsNone(get_scrap_review(a_id, 1))
            clear_work_draft()
            b_id = save_quote("b", self.snapshot)
            save_work_draft({"version": 1, "inquiry": {"filename": "B-未完成.png"}})
            save_scrap_review(b_id, 1, "无法供料", note="模拟回填")
        with self.scope(self.a):
            restored = load_work_draft()
            self.assertEqual(restored["payload"], draft)
            self.assertEqual(restored["drawings"]["line:stable-line"]["data"], self.drawing["data"])
            self.assertEqual(get_scrap_review(a_id, 1)["confirmed_quantity"], 14)
            new_id = save_quote("copy", self.snapshot, a_id, drawings={1: self.drawing})
            self.assertEqual(get_quote(new_id)["source_quote_id"], a_id)
            self.assertIsNone(get_scrap_review(new_id, 1))
            self.assertEqual(get_quote(a_id)["snapshot"], order)

    def test_concurrent_storage_scopes_keep_callbacks_in_their_databases(self):
        barrier = Barrier(2)

        def callback(credential, name):
            with self.scope(credential):
                barrier.wait(timeout=10)
                for index in range(5):
                    item = {**self.snapshot, "filename": f"{name}-{index}.png"}
                    quote_id = save_quote(f"key-{index}", item)
                    save_scrap_review(quote_id, 1, "无法供料", note="模拟回填")
                    save_work_draft({"version": 1, "inquiry": {"filename": name}})
                return database_path(), [row["snapshot"]["filename"] for row in list_quotes()]

        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(callback, self.a, "A")
            b = workers.submit(callback, self.b, "B")
            a_path, a_names = a.result(timeout=30)
            b_path, b_names = b.result(timeout=30)
        self.assertNotEqual(a_path, b_path)
        self.assertEqual(set(a_names), {f"A-{index}.png" for index in range(5)})
        self.assertEqual(set(b_names), {f"B-{index}.png" for index in range(5)})
        with self.assertRaises(VisitorAccessError):
            list_quotes()  # No context survives the worker threads.

    def test_missing_tampered_expired_keys_and_paths_fail_closed(self):
        expired = new_credential(self.key, now=int(time.time()) - COOKIE_DAYS * 86400)
        for value in (None, "", "../../legacy.sqlite3", self.a[:-1] + ("0" if self.a[-1] != "0" else "1"), expired):
            with self.subTest(kind=type(value).__name__), self.assertRaises(VisitorAccessError):
                with self.scope(value):
                    list_quotes()
        self.assertFalse(self.root.exists())
        with self.assertRaises(VisitorAccessError):
            database_path()
        with patch.dict(os.environ, {"DAINAN_VISITOR_SIGNING_KEY": ""}):
            app = self.app(view="workbench")
            self.assertTrue(any("DAINAN_VISITOR_SIGNING_KEY" in item.value for item in app.error))
        pending = self.app(view="records")
        self.assertNotIn("查找询盘", [field.label for field in pending.text_input])
        self.assertFalse(self.root.exists())
        # Key rotation neither grants the old directory nor removes it.
        with self.scope(self.a):
            old_path = database_path()
            save_quote("keep", self.snapshot)
        with self.assertRaises(VisitorAccessError):
            verify_credential(self.a, secrets.token_urlsafe(48))
        self.assertTrue(old_path.exists())

    def test_same_signed_cookie_finds_history_in_fresh_session(self):
        with self.scope(self.a):
            quote_id = save_quote("refresh", self.snapshot)
        first = self.app(self.a, quote=quote_id)
        reopened = self.app(self.a, quote=quote_id)
        self.assertEqual(first.session_state["_visitor_owner"], reopened.session_state["_visitor_owner"])
        self.assertTrue(any("4,729.79" in element.proto.body for element in reopened.get("html")))
        self.assertNotEqual(make_access(self.a, self.key, self.root).database_path(),
                            make_access(self.b, self.key, self.root).database_path())

    def test_identity_change_clears_every_business_and_download_reference(self):
        state = {COOKIE_KEY: {"observation": {"credential": self.b}}, "_visitor_owner": "old-owner",
                 "drawing_run": self.snapshot, "inquiry_drawing": self.drawing, "formal_report": "A HTML",
                 "drawing_review": {"fields": {}}, "order_items": {"line": self.snapshot},
                 "order_drawings": {"line": self.drawing}, "quote_save": {"quote_id": 1},
                 "work_draft_offer": {"private": "A"}, "customer-quote-file": "A download"}
        owner = make_access(self.b, self.key, self.root).owner
        self.assertTrue(sync_session_owner(state, owner))
        self.assertEqual(set(state), {COOKIE_KEY, "_visitor_owner"})

    def test_old_save_callback_is_blocked_before_main_on_identity_change(self):
        app = self.app(self.a, view="workbench")
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.slider(key="target_margin").value, 12)
        old_save = next(button for button in app.button if button.label == "保存询盘")
        # This imitates a new bridge observation arriving with an old queued click.
        app.session_state[COOKIE_KEY] = {"observation": {"credential": self.b, "available": True}}
        old_save.click().run()
        self.assertFalse(app.exception)
        for credential in (self.a, self.b):
            with self.scope(credential):
                self.assertEqual(list_quotes(), [])
        self.assertNotIn("drawing_run", app.session_state)

    def test_session_only_choice_is_independent_and_requires_configuration(self):
        app = self.app(view="workbench")
        next(button for button in app.button if button.label == "仅本次会话使用").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("仅本次会话" in message.value for message in app.warning))
        self.assertIn("PRESET_3_SCRAP_MATCH", [button.key for button in app.button])
        app.button(key="PRESET_3_SCRAP_MATCH").click().run()
        next(button for button in app.button if button.label == "保存询盘").click().run()
        ephemeral = app.session_state["_visitor_session_only"]
        with self.scope(ephemeral):
            self.assertEqual(len(list_quotes()), 1)
        with self.scope(self.a):
            self.assertEqual(list_quotes(), [])

    def test_previous_session_only_identity_is_not_reused_after_cookie_identity_change(self):
        state = {"_visitor_owner": make_access(self.a, self.key, self.root).owner,
                 "_visitor_session_only": self.a, COOKIE_KEY: {"observation": {"credential": self.b}}}
        sync_session_owner(state, make_access(self.b, self.key, self.root).owner)
        self.assertNotIn("_visitor_session_only", state)
        sync_session_owner(state, None)
        self.assertNotIn("_visitor_session_only", state)

    def test_live_scope_expires_before_any_further_database_connection(self):
        with self.scope(self.a):
            save_quote("before-expiry", self.snapshot)
            path = database_path()
            original = path.read_bytes()
            with patch("visitor_context.time.time", return_value=time.time() + COOKIE_DAYS * 86400 + 1):
                with self.assertRaises(VisitorAccessError):
                    delete_quote(1)
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_browser_observation_never_opens_storage(self):
        for credential in ({"database": str(self.shared)}, [self.a], "x" * 600):
            app = self.app(credential)
            self.assertNotIn("查找询盘", [field.label for field in app.text_input])
            self.assertFalse(self.root.exists())

    def test_private_uploaded_image_is_inline_and_only_saved_for_its_visitor(self):
        app = self.app(self.a, view="workbench")
        app.file_uploader[0].upload("A-private.png", self.drawing["data"], "image/png").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.get("imgs"))
        self.assertTrue(any('src="data:image/png;base64,' in item.proto.body for item in app.get("html")))
        next(button for button in app.button if button.label == "识别图纸参数").click().run()
        # No API is configured: retain original bytes, then use human confirmation.
        next(field for field in app.text_input if field.label == "材质牌号").set_value("316L").run()
        next(field for field in app.selectbox if field.label == "零件形状").set_value("flange").run()
        for label, value in (("外径（mm）", 280.0), ("中心孔内径（mm）", 120.0),
                             ("板厚（mm）", 12.0), ("采购数量（件）", 20)):
            next(field for field in app.number_input if field.label == label).set_value(value).run()
        self.assertNotIn("保存询盘", [button.label for button in app.button])
        next(button for button in app.button if button.label == "确认参数并核价").click().run()
        next(button for button in app.button if button.label == "保存询盘").click().run()
        self.assertFalse(app.exception)
        with self.scope(self.a):
            self.assertEqual(get_quote_drawing(1)["data"], self.drawing["data"])
        with self.scope(self.b):
            self.assertIsNone(get_quote_drawing(1))


if __name__ == "__main__":
    unittest.main()
