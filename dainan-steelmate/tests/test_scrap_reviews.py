import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import (
    DEFAULT_TARGET_MARGIN,
    build_order_snapshot,
    customer_quotation_markup,
    customer_quote_fields,
    customer_wechat_text,
    quote_rows,
    quote_valid_until,
    reprice,
)
from core.agent_pipeline import run_pipeline
from core.steel_engine import load_material_rule
from core.vision_parser import BUILTIN_DRAWING_SPECS
from quote_store import get_quote, get_scrap_review, save_quote, save_scrap_review


def demo_snapshot(sample: str, **changes) -> dict:
    inquiry = {**BUILTIN_DRAWING_SPECS[sample], **changes}
    quote, costs = reprice(inquiry, DEFAULT_TARGET_MARGIN)
    tolerance = load_material_rule(inquiry["material"])["tolerance"]
    scrap = run_pipeline(inquiry)["scrap_result"]
    issued = "2026-11-17 10:00:00"
    snapshot = {
        "filename": f"{sample}.png",
        "inquiry": inquiry,
        "demo": True,
        "quote": quote,
        "costs": costs,
        "issued": issued,
        "valid_until": quote_valid_until(issued),
        "tolerance": tolerance,
        "gross_margin_percent": DEFAULT_TARGET_MARGIN,
        "scrap": scrap,
    }
    snapshot["rows"] = quote_rows(snapshot, quote, scrap, tolerance, DEFAULT_TARGET_MARGIN)
    return snapshot


class ScrapReviewPersistenceTest(unittest.TestCase):
    def test_review_round_trip_keeps_saved_quotes_and_lines_independent(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"DAINAN_QUOTE_DB": str(Path(directory) / "quotes.sqlite3")}))
        flange = demo_snapshot("TP-001")
        pad = demo_snapshot("TP-002")
        quote_id = save_quote("demo-flange", flange)
        original = get_quote(quote_id)
        quote_fields = customer_quote_fields(original["snapshot"], quote_id)
        original_delivery = (customer_wechat_text(quote_fields), customer_quotation_markup(quote_fields))
        self.assertEqual(flange["inquiry"]["quantity"], 20)
        self.assertEqual(flange["scrap"]["最优拼单推荐"]["可拼单件数"], 16)
        self.assertIsNone(get_scrap_review(quote_id, 1))

        save_scrap_review(quote_id, 1, "确认可供", 14, "635 × 412 × 11.9 mm", "模拟回填")
        review = get_scrap_review(quote_id, 1)
        self.assertEqual(review["status"], "确认可供")
        self.assertEqual(review["confirmed_quantity"], 14)
        self.assertEqual(review["measured_dimensions"], "635 × 412 × 11.9 mm")
        self.assertEqual(review["note"], "模拟回填")
        self.assertIsInstance(datetime.fromisoformat(review["reviewed_at"]), datetime)
        self.assertEqual(get_quote(quote_id), original)

        for invalid in (0, 21, 1.5, True):
            with self.subTest(confirmed_quantity=invalid), self.assertRaises(ValueError):
                save_scrap_review(quote_id, 1, "确认可供", invalid)
        self.assertEqual(get_scrap_review(quote_id, 1), review)
        save_scrap_review(quote_id, 1, "无法供料", 14, note="模拟回填，余料已用完")
        unavailable = get_scrap_review(quote_id, 1)
        self.assertEqual((unavailable["status"], unavailable["confirmed_quantity"]), ("无法供料", 0))
        self.assertEqual(get_quote(quote_id), original)
        quote_fields = customer_quote_fields(get_quote(quote_id)["snapshot"], quote_id)
        self.assertEqual((customer_wechat_text(quote_fields), customer_quotation_markup(quote_fields)), original_delivery)

        order = build_order_snapshot([flange, pad], "2026-11-17 11:00:00")
        order_id = save_quote("demo-order", order)
        original_order = get_quote(order_id)
        save_scrap_review(order_id, 1, "确认可供", 20, note="第 1 项模拟回填")
        save_scrap_review(order_id, 2, "确认可供", 5, note="第 2 项模拟回填")
        self.assertEqual(get_scrap_review(order_id, 1)["confirmed_quantity"], 20)
        self.assertEqual(get_scrap_review(order_id, 2)["confirmed_quantity"], 5)
        with self.assertRaises(ValueError):
            save_scrap_review(order_id, 2, "确认可供", 9)
        self.assertEqual(get_scrap_review(order_id, 2)["confirmed_quantity"], 5)
        self.assertEqual(get_scrap_review(quote_id, 1), unavailable)
        self.assertEqual(get_quote(order_id), original_order)

        revision_id = save_quote("demo-revision", demo_snapshot("TP-001", quantity=18), source_quote_id=quote_id)
        self.assertIsNone(get_scrap_review(revision_id, 1))
        unmatched_id = save_quote("demo-unmatched", demo_snapshot("TP-005"))
        with self.assertRaises(ValueError):
            save_scrap_review(unmatched_id, 1, "确认可供", 1)
