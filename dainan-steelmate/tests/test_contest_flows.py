import csv
import io
import json
import os
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from openai import APITimeoutError
from streamlit.testing.v1 import AppTest

from app import (
    DEFAULT_TARGET_MARGIN, DEMO_QUOTE_TITLE, FLANGE_CUTTING_NOTE, QUOTE_OPTIONS, SCRAP_CUT_NOTE,
    build_order_snapshot, customer_order_fields, customer_order_markup, customer_order_wechat_text,
    customer_quotation_markup, customer_quote_fields, customer_wechat_text,
    quotation_csv, quotation_markup, quote_rows, reprice, scrap_narration,
)
from core import steel_engine
from core.agent_pipeline import run_pipeline
from core.vision_parser import BUILTIN_DRAWING_SPECS, recognize_new_drawing
from quote_store import delete_quote, get_quote, list_quotes, quote_number, save_quote


ROOT = Path(__file__).resolve().parents[1]
BLIND_TEST = ROOT / "data" / "Blind_Test"
QUOTE_DOWNLOADS = ("下载客户报价单", "下载 HTML（可打印）", "下载 CSV")


def open_workbench() -> AppTest:
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.query_params["view"] = "workbench"
    return app.run()


class ContestFlowsTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))

    def test_judge_flow_requires_confirmation_and_opens_scrap_demo(self):
        app = open_workbench()
        app.button(key="judge_start").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="sample").value, "TP-015.png")
        self.assertIn("参数来源 · 预设参数", [item.value for item in app.caption])
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "NEED_INFO")
        self.assertTrue(any("复制追问话术" in item.proto.body and "板材厚度" in item.proto.body
                            for item in app.get("html")))
        self.assertEqual(app.number_input[0].value, 10.0)
        self.assertFalse(app.get("download_button"))
        self.assertFalse(any("复制微信报价" in item.proto.body for item in app.get("html")))
        self.assertNotIn("保存询盘", [item.label for item in app.button])
        self.assertNotIn("查看余料拼单", [item.label for item in app.button])

        app.number_input[0].set_value(0.0)
        next(item for item in app.button if item.label == "确认厚度并生成报价").click().run()
        self.assertFalse(app.get("download_button"))
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "NEED_INFO")
        app.number_input[0].set_value(10.0)
        next(item for item in app.button if item.label == "确认厚度并生成报价").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "SUCCESS")
        self.assertTrue(app.session_state["drawing_run"]["demo"])
        self.assertCountEqual([button.label for button in app.get("download_button")], QUOTE_DOWNLOADS)

        app.button(key="judge_scrap").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="sample").value, "TP-001.png")
        self.assertCountEqual([button.label for button in app.get("download_button")], QUOTE_DOWNLOADS)
        app.button(key="judge_start").click().run()
        self.assertEqual(app.number_input[0].value, 10.0)
        self.assertFalse(app.get("download_button"))

    def test_manual_entry_waits_for_confirmation(self):
        app = open_workbench()
        self.assertNotIn("drawing_run", app.session_state)
        app.button(key="manual_entry_action").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn("drawing_run", app.session_state)
        self.assertEqual(app.text_input[0].value, "")
        app.text_input[0].set_value("304")
        next(item for item in app.selectbox if item.label == "零件形状").set_value("plate").run()
        for label, value in (("长度", 300.0), ("板厚", 10.0), ("宽度", 300.0), ("采购数量", 15)):
            next(item for item in app.number_input if item.label.startswith(label)).set_value(value)
        next(item for item in app.button if item.label == "确认参数并核价").click().run()
        self.assertFalse(app.exception)
        run = app.session_state["drawing_run"]
        self.assertEqual(run["result"]["status"], "SUCCESS")
        self.assertFalse(run["demo"])
        self.assertEqual(run["inquiry"]["width_mm"], 300.0)

    def test_quote_exports_use_selected_margin_and_matched_quantity(self):
        inquiry = BUILTIN_DRAWING_SPECS["TP-001"]
        result = run_pipeline(inquiry)
        scrap = result["scrap_result"]
        narration = scrap_narration(scrap, inquiry["quantity"])
        self.assertIn("距戴南中心 1.8 km", narration)
        self.assertIn("20 件中预计 16 件可用余料，这 16 件预计节省材料费 ¥760.37（25.2%）", narration)
        self.assertIn(SCRAP_CUT_NOTE, narration)
        run = {"filename": "TP-001.png", "inquiry": inquiry, "issued": "2026-11-16 10:00:00", "demo": True}
        for margin in (DEFAULT_TARGET_MARGIN, 20):
            quote, _ = reprice(inquiry, margin)
            rows = quote_rows(run, quote, scrap, 0.95, margin)
            csv_rows = list(csv.reader(io.StringIO(quotation_csv(rows).decode("utf-8-sig"))))
            markup = quotation_markup(rows, DEMO_QUOTE_TITLE)
            self.assertIn(f"<h1>戴南钢算AI · {DEMO_QUOTE_TITLE}</h1>", markup)
            expected = [
                ("报价类型", "参赛演示报价", ""),
                ("订单总件数", "20", "件"),
                ("预计可拼件数", "16", "件"),
                ("售价毛利率", str(margin), "%"),
                ("距戴南中心", "1.8", "km"),
                ("预计节省材料费（按可拼件数）", "760.37", "元"),
                ("材料费节省比例（按可拼件数）", "25.2", "%"),
                ("余料估算说明", SCRAP_CUT_NOTE, ""),
                *((label, f"{quote[key]:.2f}", "元") for label, key in QUOTE_OPTIONS),
            ]
            for label, value, unit in expected:
                self.assertIn([label, value, unit], csv_rows)
                self.assertIn(f'<th scope="row">{label}</th><td>{value}</td><td>{unit}</td>', markup)

    def test_customer_exports_use_confirmed_specs_and_customer_fields_only(self):
        examples = (
            (BUILTIN_DRAWING_SPECS["TP-001"], "法兰", "外径 Φ280 mm、中心孔 Φ120 mm、板厚 12 mm"),
            ({**BUILTIN_DRAWING_SPECS["TP-001"], "inner_dim_mm": 0}, "圆片", "外径 Φ280 mm、板厚 12 mm"),
            (BUILTIN_DRAWING_SPECS["TP-002"], "矩形板", "长度 1200 mm、宽度 600 mm、板厚 2 mm"),
            (BUILTIN_DRAWING_SPECS["TP-010"], "圆钢", "直径 Φ80 mm、长度 450 mm"),
        )
        for inquiry, item, spec in examples:
            with self.subTest(item=item):
                quote, costs = reprice(inquiry, DEFAULT_TARGET_MARGIN)
                snapshot = {
                    "inquiry": inquiry, "quote": quote, "demo": True,
                    "issued": "2026-11-17 10:00:00", "valid_until": "2026-11-24 10:00:00",
                    "gross_margin_percent": DEFAULT_TARGET_MARGIN, "costs": costs,
                    "filename": "private-drawing.png",
                    "rows": [("售价毛利率", 12, "%"), ("余料供应方", "private-supplier", "")],
                    "scrap": {"最优拼单推荐": {"工厂编号": "DN-001", "直接节省金额_元": 760.37}},
                }
                fields = customer_quote_fields(snapshot, 8)
                text = customer_wechat_text(fields)
                markup = customer_quotation_markup(fields)
                self.assertEqual(text.splitlines(), [
                    "戴南钢算AI · 参赛演示报价 · 客户报价单 · Q-000008",
                    f'{quote["材质"]} {item}｜{spec}',
                    f'数量 {inquiry["quantity"]} 件',
                    f'方案 A（理计不含税自提） ¥{quote[QUOTE_OPTIONS[0][1]]:,.2f}',
                    f'方案 B（过磅含 13% 专票） ¥{quote[QUOTE_OPTIONS[1][1]]:,.2f}',
                    "有效期至 2026-11-24 10:00:00（北京时间）",
                ])
                labels = ["报价编号", "品名", "材质牌号", "规格", "订单总件数",
                          "方案 A（理计不含税自提）", "方案 B（过磅含 13% 专票）",
                          "报价时间（北京时间）", "有效期至（北京时间）"]
                if inquiry["shape"] == "flange":
                    labels.append("加工范围说明")
                    self.assertIn(FLANGE_CUTTING_NOTE, markup)
                else:
                    self.assertNotIn(FLANGE_CUTTING_NOTE, markup)
                self.assertEqual(re.findall(r'<th scope="row">([^<]+)</th>', markup), labels)
                self.assertIn(f"<td>{spec}</td>", markup)
                self.assertIn("实际过磅重量确认结算", markup)
                for internal in ("private-drawing", "private-supplier", "售价毛利率", "激光加工费",
                                 "下差系数", "DN-001", "760.37"):
                    self.assertNotIn(internal, text + markup)

    def test_order_totals_use_displayed_cents_and_earliest_validity(self):
        price_a, price_b = (key for _, key in QUOTE_OPTIONS)
        items = [
            {"filename": "later.png", "quote": {price_a: 1.006, price_b: 2.004},
             "valid_until": "2026-11-24 12:00:00", "demo": False},
            {"filename": "earlier.png", "quote": {price_a: 1.006, price_b: 2.004},
             "valid_until": "2026-11-24 10:00:00", "demo": True},
        ]
        order = build_order_snapshot(items, "2026-11-17 12:30:00")
        self.assertEqual(order["totals"], {price_a: "2.02", price_b: "4.00"})
        self.assertEqual(order["valid_until"], "2026-11-24 10:00:00")
        self.assertTrue(order["demo"])

    def test_order_lines_are_frozen_and_survive_saving_to_history(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))
        workbench = open_workbench()

        def action(label):
            return next(button for button in workbench.button if button.label == label)

        workbench.button(key="judge_start").click().run()
        self.assertNotIn("加入本单", [button.label for button in workbench.button])
        workbench.button(key="PRESET_3_SCRAP_MATCH").click().run()
        action("加入本单").click().run()
        self.assertTrue(action("加入本单").proto.disabled)
        first, = workbench.session_state["order_items"].values()
        price_a, price_b = (key for _, key in QUOTE_OPTIONS)
        self.assertEqual(first["quote"][price_a], 4729.79)
        workbench.slider(key="target_margin").set_value(18).run()
        self.assertEqual(next(iter(workbench.session_state["order_items"].values()))["quote"][price_a], 4729.79)
        workbench.slider(key="target_margin").set_value(DEFAULT_TARGET_MARGIN).run()
        workbench.button(key="new-inquiry-action").click().run()
        self.assertEqual(len(workbench.session_state["order_items"]), 1)
        self.assertIsNone(workbench.selectbox(key="sample").value)
        self.assertNotIn("drawing_run", workbench.session_state)
        workbench.button(key="PRESET_1_FAST_QUOTE").click().run()
        action("加入本单").click().run()
        action("移除第 2 项").click().run()
        self.assertEqual(list(workbench.session_state["order_items"].values()), [first])
        action("加入本单").click().run()
        action("保存本单").click().run()
        self.assertFalse(workbench.exception)
        record, = list_quotes()
        snapshot = record["snapshot"]
        self.assertCountEqual([line["filename"] for line in snapshot["items"]], ["TP-001.png", "TP-002.png"])
        self.assertEqual(snapshot["totals"], {price_a: "6261.21", price_b: "6746.33"})
        self.assertEqual(snapshot["valid_until"], min(line["valid_until"] for line in snapshot["items"]))
        self.assertEqual(list_quotes("TP-002"), [record])
        fields = customer_order_fields(snapshot, record["id"])
        text = customer_order_wechat_text(fields)
        markup = customer_order_markup(fields)
        for visible in ("参赛演示报价", "Q-000001", "4,729.79", "1,531.42", "6,261.21", "6,746.33",
                        snapshot["valid_until"]):
            self.assertIn(visible, text)
            self.assertIn(visible, markup)
        for internal in ("售价毛利率", "余料供应方", "预计可拼", "760.37"):
            self.assertNotIn(internal, text + markup)

        history = AppTest.from_file(str(ROOT / "app.py"))
        history.query_params["view"] = "records"
        history.query_params["quote"] = str(record["id"])
        with patch.object(steel_engine, "load_material_rule", side_effect=AssertionError("history recalculated")):
            history.run()
        self.assertFalse(history.exception)
        self.assertEqual([button.label for button in history.get("download_button")], ["下载客户报价单"])
        order_markup = "\n".join(item.proto.body for item in history.get("html"))
        self.assertIn("多件报价", order_markup)
        self.assertIn("¥6,261.21", order_markup)
        self.assertEqual(get_quote(record["id"]), record)

    def test_saved_inquiry_survives_reopen_and_copy_reprices_independently(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3"),
        }):
            workbench = open_workbench()
            workbench.button(key="PRESET_3_SCRAP_MATCH").click().run()
            workbench.slider(key="target_margin").set_value(18).run()
            next(button for button in workbench.button if button.label == "保存询盘").click().run()
            self.assertFalse(workbench.exception)
            saved = workbench.session_state["quote_save"]
            original_id = saved["quote_id"]
            original = get_quote(original_id)
            snapshot = original["snapshot"]
            self.assertEqual(list_quotes("tp-001"), [original])
            self.assertEqual(list_quotes(quote_number(original_id)), [original])
            self.assertEqual(snapshot["gross_margin_percent"], 18)
            self.assertIsNone(original["source_quote_id"])
            self.assertEqual(save_quote(saved["save_key"], snapshot), original_id)
            workbench.run()
            self.assertEqual(list_quotes(), [original])

            history = AppTest.from_file(str(ROOT / "app.py"))
            history.query_params["view"] = "records"
            history.query_params["quote"] = str(original_id)
            with patch.object(steel_engine, "load_material_rule", side_effect=AssertionError("history recalculated")):
                history.run()
            self.assertFalse(history.exception)
            self.assertCountEqual([button.label for button in history.get("download_button")], QUOTE_DOWNLOADS)
            readonly = "\n".join(element.proto.body for element in history.get("html"))
            for _, key in QUOTE_OPTIONS:
                self.assertIn(f'¥{snapshot["quote"][key]:,.2f}', readonly)
            self.assertIn(snapshot["valid_until"], readonly)

            next(button for button in history.button if button.label == "复制改价").click().run()
            self.assertFalse(history.exception)
            self.assertNotIn("drawing_run", history.session_state)
            self.assertFalse(history.get("download_button"))
            self.assertFalse(any("复制微信报价" in element.proto.body for element in history.get("html")))
            self.assertNotIn("保存询盘", [item.label for item in history.button])
            self.assertEqual(history.slider(key="target_margin").value, 18)
            quantity = next(field for field in history.number_input if field.label.startswith("采购数量"))
            self.assertEqual(quantity.value, 20)
            quantity.set_value(30)

            updated_rule = {**steel_engine.load_material_rule("316L"), "base_price": 30.0, "tolerance": 0.93}
            with patch.object(steel_engine, "load_material_rule", return_value=updated_rule):
                next(button for button in history.button if button.label == "确认参数并核价").click().run()
                next(button for button in history.button if button.label == "保存询盘").click().run()
                expected, _ = reprice({**snapshot["inquiry"], "quantity": 30}, 18)
            self.assertFalse(history.exception)
            new, old = list_quotes()
            self.assertEqual(old, original)
            self.assertNotEqual(new["id"], original_id)
            self.assertEqual(new["source_quote_id"], original_id)
            self.assertTrue(new["snapshot"]["demo"])
            self.assertEqual(new["snapshot"]["inquiry"]["quantity"], 30)
            self.assertEqual(new["snapshot"]["quote"], expected)
            self.assertEqual(new["snapshot"]["tolerance"], 0.93)

    def test_delete_legacy_inquiry_preserves_revision_and_reserves_number(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3"),
        }):
            path = os.environ["DAINAN_QUOTE_DB"]
            original = {"filename": "TP-001.png", "issued": "2026-11-17 10:00:00", "inquiry": {"quantity": 20}}
            revision = {**original, "issued": "2026-11-17 11:00:00", "inquiry": {"quantity": 30}}
            with closing(sqlite3.connect(path)) as legacy, legacy:
                legacy.execute("PRAGMA foreign_keys = ON")
                legacy.execute("""
                    CREATE TABLE quotes (
                        id INTEGER PRIMARY KEY, save_key TEXT NOT NULL UNIQUE,
                        source_quote_id INTEGER REFERENCES quotes(id),
                        filename TEXT NOT NULL, issued TEXT NOT NULL, snapshot TEXT NOT NULL
                    )
                """)
                for quote_id, key, source, snapshot in ((1, "original", None, original), (2, "revision", 1, revision)):
                    legacy.execute("INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?)",
                                   (quote_id, key, source, snapshot["filename"], snapshot["issued"], json.dumps(snapshot)))

            self.assertEqual([entry["id"] for entry in list_quotes()], [2, 1])
            self.assertTrue(delete_quote(1))
            self.assertIsNone(get_quote(1))
            self.assertIsNone(save_quote("original", original))
            self.assertEqual(get_quote(2), {"id": 2, "source_quote_id": 1, "snapshot": revision})
            self.assertFalse(delete_quote(1))
            with closing(sqlite3.connect(path)) as reopened:
                self.assertEqual(reopened.execute("SELECT id FROM quotes").fetchall(), [(2,)])
            self.assertTrue(delete_quote(2))
            self.assertEqual(save_quote("new-quote", original), 3)

    def test_quote_regression_and_three_kilometre_scope(self):
        truth = pd.read_excel(ROOT / "data" / "Test_Images_Ground_Truth.xlsx").set_index("图纸编号")
        quote_columns = ("方案A_理计不含税_元", "方案B_过磅含13专票_元")
        for drawing, inquiry in BUILTIN_DRAWING_SPECS.items():
            with self.subTest(drawing=drawing):
                result = run_pipeline(inquiry)
                if drawing == "TP-015":
                    self.assertEqual(result["status"], "NEED_INFO")
                    self.assertIsNone(result["quote_result"])
                    result = run_pipeline({**inquiry, "thickness_mm": 10.0})
                self.assertEqual(result["status"], "SUCCESS")
                for (_, key), column in zip(QUOTE_OPTIONS, quote_columns):
                    self.assertEqual(result["quote_result"][key], truth.loc[drawing, column])
                if result["scrap_result"]["是否匹配成功"]:
                    self.assertLessEqual(result["scrap_result"]["最优拼单推荐"]["直线距离_km"], 3)
                if drawing == "TP-005":
                    self.assertFalse(result["scrap_result"]["是否匹配成功"])
                    self.assertEqual(result["scrap_result"]["撮合状态"], "无匹配余料")
                if drawing == "TP-001":
                    match = result["scrap_result"]["最优拼单推荐"]
                    self.assertEqual((match["工厂编号"], match["可拼单件数"], match["直接节省金额_元"]),
                                     ("DN-001", 16, 760.37))
                    quote, _ = reprice(inquiry, DEFAULT_TARGET_MARGIN)
                    self.assertEqual(tuple(quote[key] for _, key in QUOTE_OPTIONS), (4729.79, 5102.40))

    def test_radius_filter_precedes_ranking_and_includes_boundary(self):
        stock = {"材质牌号": "316L", "厚度_mm": 12, "可用长_mm": 650, "可用宽_mm": 420,
                 "余料形态": "板材边角料", "库存数量_块": 8, "所在园区_镇街": "测试", "脱敏厂名": "测试",
                 steel_engine.RECTANGULAR_AREA_FIELD: "是"}
        inventory = pd.DataFrame([
            {**stock, "工厂编号": "FAR", "距中心距离_km": 3.01, "余料特惠单价_元每公斤": 8},
            {**stock, "工厂编号": "NEAR", "距中心距离_km": 3.0, "余料特惠单价_元每公斤": 18.8},
        ])
        with patch.object(steel_engine.pd, "read_csv", return_value=inventory):
            result = steel_engine.match_local_scrap("316L", 12, 280, 280, 20)
        self.assertEqual(result["最优拼单推荐"]["工厂编号"], "NEAR")

    def test_round_core_and_marked_rectangle_for_square(self):
        circle = pd.read_csv(ROOT / "data" / "Scrap_Inventory.csv").query("工厂编号 == 'DN-002'")
        self.assertEqual(len(circle), 1)
        inquiry = {"shape": "plate", "material": "304", "outer_dim_mm": 300.0, "width_mm": 300.0,
                   "thickness_mm": 10.0, "quantity": 15}
        with patch.object(steel_engine.pd, "read_csv", return_value=circle):
            result = run_pipeline(inquiry)
        scrap = result["scrap_result"]
        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(result["quote_result"])
        self.assertEqual(result["agent_thoughts"][-1]["status"], "有候选，需人工复核")
        self.assertFalse(scrap["是否匹配成功"])
        self.assertNotIn("最优拼单推荐", scrap)
        self.assertNotIn("预计可拼", scrap_narration(scrap, inquiry["quantity"]))
        run = {"filename": "square.png", "inquiry": inquiry, "issued": "2026-11-16 10:00:00"}
        quote, _ = reprice(inquiry, DEFAULT_TARGET_MARGIN)
        rows = quote_rows(run, quote, scrap, 0.94, DEFAULT_TARGET_MARGIN)
        labels = {label for label, _, _ in rows}
        self.assertIn(("余料拼单状态", "有候选，需人工复核", ""), rows)
        self.assertNotIn("预计可拼件数", labels)
        self.assertNotIn("预计节省材料费（按可拼件数）", labels)

        rectangle = circle.assign(**{"工厂编号": "RECT", "余料形态": "矩形余料板", "可用长_mm": 650,
                                    "可用宽_mm": 420, "库存数量_块": 2, steel_engine.RECTANGULAR_AREA_FIELD: "是"})
        inventory = pd.concat([circle, rectangle], ignore_index=True)
        with patch.object(steel_engine.pd, "read_csv", return_value=inventory):
            result = steel_engine.match_local_scrap("304", 10, 300, 300, 15)
        match = result["最优拼单推荐"]
        self.assertEqual((match["工厂编号"], match["可拼单件数"], match["直接节省金额_元"]),
                         ("RECT", 4, 77.08))

    def test_missing_or_invalid_scrap_geometry_needs_review(self):
        stock = pd.DataFrame([steel_engine.DEFAULT_SCRAP_POOL[0]])
        for inventory in (
            stock.drop(columns=[steel_engine.RECTANGULAR_AREA_FIELD]),
            stock.drop(columns=["可用宽_mm"]),
            stock.assign(余料形态="未知"),
            stock.assign(可用长_mm=float("inf")),
            stock.assign(可用宽_mm=0),
        ):
            with self.subTest(inventory=inventory.to_dict("records")):
                with patch.object(steel_engine.pd, "read_csv", return_value=inventory):
                    result = steel_engine.match_local_scrap("316L", 12, 280, 280, 20)
                self.assertEqual(result["撮合状态"], "有候选，需人工复核")
                self.assertNotIn("最优拼单推荐", result)

    def test_fallback_inventory_uses_same_rectangle_requirement(self):
        missing = ROOT / "data" / "__missing_inventory_for_test__.csv"
        with patch.object(steel_engine, "_resolve_data_path", return_value=missing):
            circle = steel_engine.match_local_scrap("304", 10, 300, 300, 15)
            rectangle = steel_engine.match_local_scrap("316L", 12, 280, 280, 20)
        self.assertEqual(circle["撮合状态"], "有候选，需人工复核")
        self.assertNotIn("最优拼单推荐", circle)
        self.assertEqual(rectangle["最优拼单推荐"]["工厂编号"], "DN-001")
        self.assertEqual(rectangle["最优拼单推荐"]["可拼单件数"], 2)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    @patch("openai.OpenAI")
    def test_vision_records_returned_usage_with_invalid_response(self, client):
        api = client.return_value.__enter__.return_value
        api.chat.completions.create.return_value = SimpleNamespace(
            model="test-model", usage=SimpleNamespace(prompt_tokens=30, completion_tokens=8, total_tokens=38),
            choices=[SimpleNamespace(message=SimpleNamespace(content="无可用 JSON"))],
        )
        result = recognize_new_drawing(b"test image")
        self.assertEqual(result["usage"]["total_tokens"], 38)
        self.assertEqual(result["fields"], {})
        self.assertTrue(result["error"])
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.call_args.kwargs["timeout"], 15.0)

    def test_synthetic_cases_use_review_gate(self):
        samples = json.loads((BLIND_TEST / "人工核对基准.json").read_text(encoding="utf-8"))
        app = open_workbench()
        self.assertEqual(len(samples), 6)
        for case in samples:
            with self.subTest(drawing=case["file"]):
                app.file_uploader(key="upload").upload(
                    case["file"], (BLIND_TEST / case["file"]).read_bytes(), "image/png",
                ).run()
                self.assertNotIn("drawing_run", app.session_state)
                recognition = {"fields": case["reference_fields"], "model": "mock",
                               "usage": {"prompt_tokens": 41, "completion_tokens": 19, "total_tokens": 60},
                               "error": None}
                with patch("core.vision_parser.recognize_new_drawing", return_value=recognition):
                    next(button for button in app.button if button.label == "识别图纸参数").click().run()
                    self.assertFalse(app.exception)
                    self.assertNotIn("drawing_run", app.session_state)
                    if case["simulated_reply"]:
                        app.button[-1].click().run()
                        self.assertNotIn("drawing_run", app.session_state)
                        self.assertTrue(app.session_state["drawing_review"]["issues"])
                        for field, label in (("thickness_mm", "板厚"), ("outer_dim_mm", "外径")):
                            if field in case["simulated_reply"]:
                                widget = next(item for item in app.number_input if item.label.startswith(label))
                                widget.set_value(float(case["simulated_reply"][field]))
                    app.button[-1].click().run()
                    self.assertFalse(app.exception)
                    self.assertEqual(app.session_state["drawing_run"]["result"]["status"], "SUCCESS")
                    review = app.session_state["drawing_review"]
                    self.assertGreaterEqual(review["end_to_end_seconds"], review["vision_seconds"])
                    self.assertEqual(review["usage"]["total_tokens"], 60)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""})
    def test_no_key_retains_original_and_opens_manual_review(self):
        app = open_workbench()
        app.file_uploader(key="upload").upload("BL-03.png", (BLIND_TEST / "BL-03.png").read_bytes(), "image/png").run()
        next(button for button in app.button if button.label == "识别图纸参数").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.get("image"))
        self.assertNotIn("drawing_run", app.session_state)
        self.assertTrue(any("未配置" in warning.value for warning in app.warning))
        self.assertEqual(app.session_state["drawing_review"]["fields"], {})

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"})
    @patch("openai.OpenAI")
    def test_timeout_retains_original_and_opens_manual_review(self, client):
        api = client.return_value.__enter__.return_value
        api.chat.completions.create.side_effect = APITimeoutError(request=None)
        app = open_workbench()
        app.file_uploader(key="upload").upload("BL-06.png", (BLIND_TEST / "BL-06.png").read_bytes(), "image/png").run()
        next(button for button in app.button if button.label == "识别图纸参数").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.get("image"))
        self.assertTrue(any("超时" in warning.value for warning in app.warning))
        self.assertNotIn("drawing_run", app.session_state)
