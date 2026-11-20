import base64
import csv
import hashlib
import io
import json
import math
import sqlite3
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode
from uuid import uuid4
from zipfile import BadZipFile

import pandas as pd
import streamlit as st
from PIL import Image, ImageOps

from core.agent_pipeline import run_pipeline
from core.reply_parser import CHANGED, MAX_REPLY_LENGTH, NEEDS_REVIEW, ReplyItem, recognize_quote_reply
from core.steel_engine import (
    DEFAULT_MATERIAL_RULES, SCRAP_REVIEW_STATUS, calculate_stainless_quote, load_material_rule,
)
from core.vision_parser import (
    BUILTIN_DRAWING_SPECS, DEMO_PRESETS, DRAWING_FIELDS, parse_drawing_image, recognize_new_drawing,
)
from quote_store import (
    CUSTOMER_INFO_FIELDS, SCRAP_REVIEW_STATUSES, clean_customer_info, clear_work_draft, delete_quote, get_quote,
    get_quote_drawing, get_scrap_review, list_quotes, parse_quote_deadline, quote_deadline_text, quote_number, quote_validity,
    load_work_draft, save_quote, save_scrap_review, save_work_draft, validate_drawing_image,
)


DATA_DIR = Path(__file__).resolve().parent / "data"
UI_STYLES = Path(__file__).with_name("workbench.css")
DRAWINGS = {f"{drawing_id}.png": DATA_DIR / "Test_Images" / f"{drawing_id}.png"
            for drawing_id in BUILTIN_DRAWING_SPECS}
SCRIPTS = (
    ("PRESET_1_FAST_QUOTE", "304 垫板（TP-002）"),
    ("PRESET_2_MISSING_THICK", "手绘法兰缺板厚（TP-015）"),
    ("PRESET_3_SCRAP_MATCH", "316L 法兰（TP-001）"),
)
AGENTS = ("Agent 1 · 询盘解析风控员", "Agent 2 · 戴南资深核价师", "Agent 3 · 产业带余料撮合员")
SHAPES = {"flange": "法兰/圆片", "plate": "矩形板", "round_bar": "圆钢"}
QUOTE_OPTIONS = (
    ("方案A：理计不含税自提价", "方案A_理计不含税自提价_元"),
    ("方案B：过磅含13%专票价", "方案B_过磅含13专票价_元"),
)
CUSTOMER_PLAN_LABELS = ("方案 A（理计不含税自提）", "方案 B（过磅含 13% 专票）")
ORDER_PLAN_LABELS = tuple(label.replace("（", " 合计（", 1) for label in CUSTOMER_PLAN_LABELS)
ORDER_QUOTE_LABEL = "多件报价"
QUOTE_SETTLEMENT_TERMS = (
    "方案 A 按订单理论毛重计料，报价不含税，交付方式为自提。",
    "方案 B 按含下差的预估过磅重核价，交付按实际过磅重量确认结算，含 13% 增值税专用发票。",
)
QUOTE_VALIDITY_TERM = "有效期以本单标注的截止时间为准。图纸、材质、数量或加工要求变更时重新核价。"
INTERNAL_QUOTE_TERMS = (
    *QUOTE_SETTLEMENT_TERMS,
    "表内下差系数取自计价规则。实物规格及允许偏差以双方确认的图纸或订单为准。",
    QUOTE_VALIDITY_TERM,
)
CUSTOMER_QUOTE_TITLE = "客户报价单"
FLANGE_CUTTING_NOTE = "加工费按外圆及中心孔（如有）估算，附加孔及特殊工艺另行核价"
SCRAP_TITLE = "周边 3 km 余料"
SCRAP_CUT_NOTE = "余料池为演示数据。可切件数按库存尺寸预估，以工厂复核为准。"
RECOVERY_TITLE = "内孔圆芯回收"
DEMO_QUOTE_TITLE = "参赛演示报价"
DAINAN_TIME = timezone(timedelta(hours=8))
DEFAULT_TARGET_MARGIN = 12
REVIEW_DIMENSIONS = {
    "outer_dim_mm": "外径或矩形长度",
    "thickness_mm": "板厚或圆钢轴向长度",
    "width_mm": "矩形宽度",
    "inner_dim_mm": "法兰中心孔内径",
}
SHAPE_DIMENSIONS = {
    "plate": (("outer_dim_mm", "长度（mm）"), ("width_mm", "宽度（mm）"), ("thickness_mm", "板厚（mm）")),
    "flange": (("outer_dim_mm", "外径（mm）"), ("inner_dim_mm", "中心孔内径（mm）"), ("thickness_mm", "板厚（mm）")),
    "round_bar": (("outer_dim_mm", "直径（mm）"), ("thickness_mm", "轴向长度（mm）")),
}
SEAL_TITLE = "兴化市戴南镇钢算AI精密加工报价专用章"
SEAL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 144 144">
  <defs><path id="seal-arc" d="M 18,72 A 54,54 0 0 1 126,72"/></defs>
  <circle cx="72" cy="72" r="66" fill="none" stroke="#bc2029" stroke-width="4"/>
  <text fill="#bc2029" font-weight="bold" font-size="12" font-family="'Microsoft YaHei',sans-serif">
    <textPath href="#seal-arc" startOffset="50%" text-anchor="middle">兴化市戴南镇钢算AI精密加工</textPath>
  </text>
  <path d="M72 49 77 64 93 64 80 73 85 88 72 79 59 88 64 73 51 64 67 64Z" fill="#bc2029"/>
  <text x="72" y="111" text-anchor="middle" fill="#bc2029" font-weight="bold"
    font-size="15" font-family="'Microsoft YaHei',sans-serif">报价专用章</text>
</svg>"""


REPORT_STYLE = """
<style>
.industrial-report {
  box-sizing: border-box; max-width: 820px; margin: 0 auto; padding: 1.5rem;
  color: #151515; background: #fff; color-scheme: light; font: 14px/1.45 'Microsoft YaHei', sans-serif;
}
.industrial-report.industrial-report :is(h1,h2,p) { color: #151515; margin: 0 0 .45em; padding: 0; }
.industrial-report h1 { font-size: 1.65rem; }
.industrial-report h2 { font-size: 1rem; }
.industrial-report table { width: 100%; border-collapse: collapse; margin: .6rem 0; }
.industrial-report :is(th,td) { padding: 4px 8px; border-bottom: 1px solid #ccc; text-align: left; overflow-wrap: anywhere; }
.industrial-report thead, .industrial-report .total { font-weight: bold; background: #f0f0f0; }
.industrial-report tr { break-inside: avoid; }
.industrial-report .order-report-lines strong { display: block; }
.industrial-report .order-report-lines :is(th,td):nth-child(n+3) { text-align: right; white-space: nowrap; }
.industrial-report.industrial-report .report-validity { color: #983b16; border: 1px solid #c58b6a; padding: .55rem; font-weight: bold; }
.report-terms { break-inside: avoid; }
.report-terms ul { margin: 0; padding-left: 1.2em; }
.seal-row { display: flex; justify-content: flex-end; margin-top: .4rem; break-inside: avoid; }
.industrial-report .seal-row img { width: 118px; height: 118px; }
@page { size: A4; margin: 12mm; }
@media print {
  html:has(.industrial-report), body:has(.industrial-report), .stApp:has(.industrial-report),
  .stApp :is([data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"],
             [data-testid="stVerticalBlock"],[data-testid="stElementContainer"]):has(.industrial-report) {
    display: block !important; overflow: visible !important; position: static !important;
    height: auto !important; min-height: 0 !important; max-height: none !important;
    width: 100% !important; padding: 0 !important; margin: 0 !important; background: #fff !important;
  }
  .stApp:has(.industrial-report) :is(.st-key-workbench,[data-testid="stHeader"],[data-testid="stToolbar"]),
  .print-control { display: none !important; }
  .industrial-report { max-width: none; margin: 0; padding: 0; font-size: 12px; print-color-adjust: exact; }
  .industrial-report :is(th,td) { padding: 2px 6px; }
  .industrial-report .seal-row img { width: 100px; height: 100px; }
}
</style>
"""


def render_home() -> None:
    sample = DEMO_PRESETS["PRESET_3_SCRAP_MATCH"]
    result = run_pipeline(sample)
    quote, _ = reprice(sample, DEFAULT_TARGET_MARGIN)
    price_a, price_b = (quote[key] for _, key in QUOTE_OPTIONS)
    match = result["scrap_result"].get("最优拼单推荐")
    quantity = sample["quantity"]
    drawing = DRAWINGS[f'{sample["id"]}.png']
    image = ""
    if drawing.is_file():
        encoded = base64.b64encode(drawing.read_bytes()).decode("ascii")
        image = f'<img src="data:image/png;base64,{encoded}" alt="TP-001 316L 法兰原图">'
    if match:
        savings = f'<strong>预计节省材料费 <span class="mono">¥{match["直接节省金额_元"]:,.2f}</span></strong>'
        savings += f'<p><span class="mono">{quantity}</span> 件中预计 <span class="mono">{match["可拼单件数"]}</span> 件可拼</p>'
    else:
        savings = f'<p>{escape(result["scrap_result"]["撮合播报话术"])}</p>'
    with st.container(key="home"):
        st.html(f"""
        <header class="steel-nav">
          <a class="wordmark" href="?" target="_self">戴南钢算AI</a>
          <nav aria-label="首页导航"><a href="#product-features" target="_self">产品特点</a></nav>
        </header>
        <main class="home-frame">
          <section class="home-hero" aria-labelledby="home-title">
            <div class="hero-copy">
              <h1 id="home-title">非标不锈钢<br>询盘报价</h1>
              <p>面向不锈钢加工厂。上传微信手绘草图或 CAD 截图，核对规格后生成报价，并查看戴南周边可用余料。</p>
              <a class="primary-link" href="?view=workbench" target="_self">进入报价工作台 <span aria-hidden="true">↗</span></a>
            </div>
            <div class="hero-preview" aria-label="316L 法兰图纸与报价示例">
              <span class="showcase-label">示例</span>
              <figure class="showcase-drawing">{image}<figcaption>316L 法兰 · <span class="mono">TP-001</span> 原图</figcaption></figure>
              <div class="showcase-quote">
                <strong>双轨报价 · <span class="mono">{quantity}</span> 件</strong>
                <div class="showcase-quotes">
                  <div><small>理计不含税</small><b class="mono">¥{price_a:,.2f}</b></div>
                  <div><small>过磅含税</small><b class="mono">¥{price_b:,.2f}</b></div>
                </div>
              </div>
              <div class="showcase-scrap">{savings}</div>
            </div>
          </section>
          <section class="features" id="product-features" aria-labelledby="features-title">
            <h2 id="features-title">工作台能做什么</h2>
            <div class="feature-grid">
              <article class="feature-card">
                <h3>看图识参，缺项先问</h3>
                <p>从图纸提取材质、尺寸和数量。漏标板厚或尺寸有冲突时，先人工核对再出价。</p>
                <div class="feature-foot"><strong class="attention">待确认</strong><span>板厚（mm）</span></div>
              </article>
              <article class="feature-card">
                <h3>一笔询盘，两种报价</h3>
                <p>同时对比理计不含税自提价和过磅含 13% 专票价，查看计价依据并导出报价单。</p>
                <div class="feature-foot"><strong class="mono">CSV</strong><span>· 可打印报价单</span></div>
              </article>
              <article class="feature-card">
                <h3>核价时顺手找余料</h3>
                <p>按材质与规格筛选周边余料，显示预计可拼件数和对应的材料费节省。</p>
                <div class="feature-foot"><strong class="mono">3 km</strong><span>距戴南中心 · 可切件数由工厂复核</span></div>
              </article>
            </div>
          </section>
        </main>
        """)


def render_market() -> None:
    cards = []
    for material in ("304", "316L", "201", "2205"):
        rule = load_material_rule(material)
        source = " · 内置参考" if rule is DEFAULT_MATERIAL_RULES[material] else ""
        cards.append(
            f'<article class="market-card"><span class="market-grade mono">{material}</span>'
            f'<strong class="market-value mono">¥{rule["base_price"]:,.2f} <small>/ kg</small></strong>'
            f'<small>下差系数 <span class="mono">{rule["tolerance"]:.2f}</span>{source}</small></article>'
        )
    st.html(f"""
        <section class="market-board" id="steel-rates" aria-labelledby="market-title">
          <div class="market-heading">
            <h2 id="market-title">钢价基准</h2><small>戴南本地价表 · 税前</small>
          </div>
          <div class="market-grid">{"".join(cards)}</div>
        </section>
    """)


def work_draft_changed(*, inquiry: bool = False, order: bool = False) -> None:
    st.session_state.work_draft_dirty = True
    if inquiry:
        st.session_state.inquiry_draft_completed = False
    if order:
        st.session_state.order_draft_completed = False


def work_draft_customer(key: str) -> dict:
    customer = st.session_state.get(key, {})
    return {"fields": clean_customer_info(customer.get("fields")),
            "delivery_confirmed": bool(customer.get("delivery_confirmed"))}


def unfinished_work() -> tuple[dict | None, dict[str, dict]]:
    """只提取可恢复的业务字段；不序列化整个会话、回复原文或服务端配置。"""
    payload, drawings = {"version": 1, "inquiry": None, "order": None}, {}
    review = st.session_state.get("drawing_review", {})
    run = st.session_state.get("drawing_run", {})
    editing = st.session_state.get("order_edit")
    if (review or run) and not st.session_state.get("inquiry_draft_completed", False):
        fields = review_values(review.get("fields", run.get("inquiry", {})))
        form_key = f'confirm_drawing_{st.session_state.get("review_revision", 0)}'
        if review:
            for field in ("material", "shape", "quantity"):
                fields[field] = st.session_state.get(f"{form_key}_{field}", fields[field])
        shape_drafts = {
            shape: {field: prefill_number(dimensions.get(field)) for field, _ in SHAPE_DIMENSIONS[shape]}
            for shape, dimensions in review.get("shape_drafts", {}).items() if shape in SHAPES
        }
        if shape := fields["shape"]:
            dimensions = shape_drafts.setdefault(shape, {})
            for field, _ in SHAPE_DIMENSIONS[shape]:
                dimensions[field] = prefill_number(st.session_state.get(
                    f"{form_key}_{shape}_{field}", dimensions.get(field, fields[field]))) if review else fields[field]
                fields[field] = dimensions[field]
        drawing = st.session_state.get("inquiry_drawing")
        filename = (drawing or {}).get("filename") or review.get("filename") or run.get("filename", "手动询盘")
        analysis_items = review.get("reply_analysis", {}).get("items", [])
        payload["inquiry"] = {
            "filename": filename, "parameters": review_values(fields), "shape_drafts": shape_drafts,
            "gross_margin_percent": st.session_state.get("target_margin", DEFAULT_TARGET_MARGIN),
            "demo": bool(review.get("demo", run.get("demo", review.get("source") == "预设参数"))),
            "source_quote_id": review.get("source_quote_id", run.get("source_quote_id")),
            "source_line_position": review.get("source_line_position", run.get("source_line_position")),
            "customer": work_draft_customer("inquiry_customer"),
            "editing_line_id": editing["line_id"] if editing else None,
            "reply_requires_review": bool(review.get("restored_reply_review") or any(
                item.field != "invoice" and item.status in (CHANGED, NEEDS_REVIEW) for item in analysis_items)),
        }
        if validity := st.session_state.get("inquiry_validity"):
            payload["inquiry"]["validity"] = {key: validity.get(key) for key in ("date", "time", "custom")}
        if drawing:
            drawings["inquiry"] = drawing
    items = st.session_state.get("order_items", {})
    completed = st.session_state.get("order_draft_completed", False)
    if items and (not completed or editing):
        snapshot_fields = ("filename", "inquiry", "demo", "issued", "valid_until", "gross_margin_percent",
                           "quote", "costs", "tolerance", "scrap", "rows", "customer", "source_line_position")
        payload["order"] = {
            "items": [{"line_id": line_id, "snapshot": {
                key: deepcopy(item[key]) for key in snapshot_fields if key in item}} for line_id, item in items.items()],
            "issued": st.session_state.order_issued, "save_key": st.session_state.order_save_key,
            "source_quote_id": (st.session_state.get("order_save", {}).get("quote_id") if not completed else None)
                               or st.session_state.get("order_source_quote_id"),
            "customer": work_draft_customer("order_customer"),
            "saved_quote": {key: st.session_state["order_save"][key] for key in ("save_key", "quote_id")
                            if key in st.session_state.get("order_save", {})} if completed else {},
        }
        for line_id in items:
            if drawing := st.session_state.get("order_drawings", {}).get(line_id):
                drawings[f"line:{line_id}"] = drawing
    return (payload if payload["inquiry"] or payload["order"] else None), drawings


def restore_work_draft() -> None:
    offer = st.session_state.work_draft_offer
    payload, drawings = offer["payload"], offer["drawings"]
    start_new_inquiry()
    order = payload.get("order") or {}
    st.session_state.order_items = {item["line_id"]: deepcopy(item["snapshot"]) for item in order.get("items", [])}
    st.session_state.order_drawings = {line_id: drawings[f"line:{line_id}"]
                                     for line_id in st.session_state.order_items if f"line:{line_id}" in drawings}
    for key in ("order_save", "order_source_quote_id", "order_customer"):
        st.session_state.pop(key, None)
    st.session_state.order_customer_revision = st.session_state.get("order_customer_revision", 0) + 1
    if order:
        st.session_state.order_issued = order["issued"]
        st.session_state.order_save_key = order["save_key"]
        st.session_state.order_source_quote_id = order.get("source_quote_id")
        st.session_state.order_customer = deepcopy(order["customer"])
        if order.get("saved_quote"):
            st.session_state.order_save = deepcopy(order["saved_quote"])
    st.session_state.order_draft_completed = bool(order.get("saved_quote", {}).get("quote_id"))
    if inquiry := payload.get("inquiry"):
        st.session_state.manual_entry = True
        st.session_state.inquiry_drawing = drawings.get("inquiry")
        st.session_state.target_margin = inquiry["gross_margin_percent"]
        st.session_state.inquiry_customer = deepcopy(inquiry["customer"])
        if isinstance(inquiry.get("validity"), dict):
            st.session_state.inquiry_validity = {key: inquiry["validity"].get(key) for key in ("date", "time", "custom")}
        begin_review(fields=deepcopy(inquiry["parameters"]), source="恢复草稿 · 待人工确认",
                     filename=inquiry["filename"], demo=inquiry["demo"],
                     shape_drafts=deepcopy(inquiry["shape_drafts"]),
                     source_quote_id=inquiry.get("source_quote_id"),
                     source_line_position=inquiry.get("source_line_position"),
                     restored_reply_review=inquiry.get("reply_requires_review", False), restored_draft=True)
        if line_id := inquiry.get("editing_line_id"):
            st.session_state.order_edit = {"line_id": line_id, "token": uuid4().hex, "filename": inquiry["filename"]}
    st.session_state.work_draft_ready = True
    st.session_state.work_draft_dirty = False
    st.session_state.work_draft_saved_at = offer["updated_at"]
    st.session_state.pop("work_draft_offer", None)
    st.session_state.pop("work_draft_error", None)


def discard_work_draft() -> None:
    try:
        clear_work_draft()
    except (OSError, sqlite3.Error):
        st.session_state.work_draft_load_error = "草稿尚未放弃，数据库当前不可写。请重试。"
        return
    start_new_inquiry()
    for key in ("order_items", "order_drawings", "order_save", "order_source_quote_id", "order_customer"):
        st.session_state.pop(key, None)
    st.session_state.order_customer_revision = st.session_state.get("order_customer_revision", 0) + 1
    st.session_state.order_draft_completed = False
    st.session_state.work_draft_ready = True
    st.session_state.work_draft_dirty = False
    for key in ("work_draft_offer", "work_draft_saved_at", "work_draft_load_error", "work_draft_error"):
        st.session_state.pop(key, None)


def render_work_draft_gate() -> bool:
    if st.session_state.get("work_draft_ready"):
        return True
    if "work_draft_offer" not in st.session_state:
        try:
            st.session_state.work_draft_offer = load_work_draft()
        except (OSError, sqlite3.Error, ValueError):
            st.error("未能读取上次草稿，已有数据仍保留。请检查本机数据库后重试。")
            st.button("重新读取草稿")
            return False
    offer = st.session_state.work_draft_offer
    if offer is None:
        st.session_state.work_draft_ready = True
        return True
    inquiry = offer["payload"].get("inquiry") or {}
    count = len((offer["payload"].get("order") or {}).get("items", []))
    with st.container(key="draft-recovery"):
        st.html('<h2 class="panel-heading">继续未完成的询盘</h2>')
        st.markdown(f'更新时间：{quote_deadline_text(offer["updated_at"])}（北京时间）')
        st.text(f'当前询盘：{inquiry.get("filename", "未保存整单")} · 本单清单 {count} 项')
        st.caption("图纸和输入会一起恢复。正在填写或编辑的零件需重新人工确认，确认前不生成报价。")
        resume, discard = st.columns(2, gap="small")
        resume.button("继续上次询盘", type="primary", width="stretch", on_click=restore_work_draft)
        discard.button("放弃并新建", width="stretch", on_click=discard_work_draft)
        if error := st.session_state.get("work_draft_load_error"):
            st.error(error)
    return False


def render_work_draft_status() -> None:
    if st.session_state.get("work_draft_dirty"):
        try:
            payload, drawings = unfinished_work()
            if payload:
                st.session_state.work_draft_saved_at = save_work_draft(payload, drawings)
            else:
                clear_work_draft()
                st.session_state.pop("work_draft_saved_at", None)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            st.session_state.work_draft_error = True
        else:
            st.session_state.pop("work_draft_error", None)
        st.session_state.work_draft_dirty = False
    if st.session_state.get("work_draft_error"):
        st.warning("草稿尚未保存。页面输入仍在，请检查本机数据库是否可写后重试；重试成功前请勿关闭。")
        st.button("重试保存草稿", on_click=work_draft_changed)
    elif updated := st.session_state.get("work_draft_saved_at"):
        st.html(f'<p class="draft-save-status" role="status">草稿已保存 · '
                f'<time>{escape(quote_deadline_text(updated))}</time>（北京时间）</p>')


def margin_changed() -> None:
    st.session_state.pop("formal_report", None)
    work_draft_changed(inquiry=True)


def clear_quote() -> None:
    st.session_state.pop("drawing_run", None)
    st.session_state.pop("formal_report", None)
    st.session_state.pop("quote_save", None)


def reset_inquiry_customer(customer: dict | None = None) -> None:
    st.session_state.inquiry_customer = {"fields": clean_customer_info(customer), "delivery_confirmed": False}
    st.session_state.customer_revision = st.session_state.get("customer_revision", 0) + 1


def reset_inquiry_validity(deadline: object = None, *, custom: bool = False) -> None:
    st.session_state.validity_revision = st.session_state.get("validity_revision", 0) + 1
    st.session_state.pop("inquiry_validity", None)
    if deadline is not None or custom:
        parsed = parse_quote_deadline(deadline)
        st.session_state.inquiry_validity = {
            "date": parsed.date().isoformat() if parsed else None,
            "time": parsed.time().isoformat(timespec="seconds") if parsed else None,
            "custom": custom,
        }


def selected_quote_deadline() -> str | None:
    selected = st.session_state.get("inquiry_validity", {})
    if not selected.get("date") or not selected.get("time"):
        return None
    parsed = parse_quote_deadline(f'{selected["date"]}T{selected["time"]}')
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else None


def quote_deadline_changed(prefix: str) -> None:
    if prefix != f'quote-deadline-{st.session_state.get("validity_revision", 0)}':
        return
    date = st.session_state.get(f"{prefix}-date")
    time = st.session_state.get(f"{prefix}-time")
    st.session_state.inquiry_validity = {
        "date": date.isoformat() if date is not None else None,
        "time": time.isoformat(timespec="seconds") if time is not None else None,
        "custom": True,
    }
    if run := st.session_state.get("drawing_run"):
        if saved_id := st.session_state.get("quote_save", {}).get("quote_id"):
            run["source_quote_id"] = saved_id
            run["source_line_position"] = None
        run["valid_until"] = selected_quote_deadline()
    st.session_state.pop("quote_save", None)
    st.session_state.pop("formal_report", None)
    work_draft_changed(inquiry=True)


def render_quote_deadline(run: dict) -> bool:
    if "inquiry_validity" not in st.session_state:
        reset_inquiry_validity(quote_valid_until(run["issued"]))
    selected = st.session_state.inquiry_validity
    parsed_date = parse_quote_deadline(selected.get("date") + "T00:00:00") if selected.get("date") else None
    parsed_time = parse_quote_deadline("2000-01-01T" + selected["time"]) if selected.get("time") else None
    prefix = f'quote-deadline-{st.session_state.get("validity_revision", 0)}'
    for suffix, value in (("date", parsed_date.date() if parsed_date else None),
                          ("time", parsed_time.time() if parsed_time else None)):
        if f"{prefix}-{suffix}" not in st.session_state:
            st.session_state[f"{prefix}-{suffix}"] = value
    with st.container(key="quote-validity-editor"):
        st.markdown("**报价有效期**")
        date_column, time_column = st.columns(2, gap="small")
        # 以空值为控件默认值，清空输入时不会自动补回旧时间。
        date_column.date_input("截止日期", value=None, key=f"{prefix}-date", format="YYYY-MM-DD",
                               min_value=datetime.min.date(), max_value=datetime.max.date(),
                               on_change=quote_deadline_changed, args=(prefix,))
        time_column.time_input("截止时间", value=None, key=f"{prefix}-time", step=1, format="24h",
                               on_change=quote_deadline_changed, args=(prefix,))
        st.caption("北京时间 · 新询盘默认 7 天，可按本次约定调整。")
        run["valid_until"] = selected_quote_deadline()
        render_validity({"valid_until": run["valid_until"]}, warn_expired=False)
        valid = quote_validity(run["valid_until"]) == "有效期内"
        if not valid:
            st.warning("请选择晚于当前北京时间的完整截止日期和时间，再保存、加入本单或发给客户。")
    return valid


def start_new_inquiry() -> None:
    work_draft_changed(inquiry=True)
    clear_quote()
    st.session_state.pop("order_edit", None)
    reset_inquiry_customer()
    reset_inquiry_validity()
    st.session_state.sample = None
    st.session_state.manual_entry = False
    st.session_state.pop("drawing_review", None)
    st.session_state.pop("inquiry_drawing", None)
    # 清除文件上传控件中的当前图纸，同时保留本单清单。
    st.session_state.upload_revision = st.session_state.get("upload_revision", 0) + 1


def save_run(filename: str, inquiry: dict, *, followed_up: bool = False, demo: bool = False,
             source_quote_id: int | None = None, source_line_position: int | None = None) -> None:
    started = perf_counter()
    result = run_pipeline(inquiry)
    elapsed = perf_counter() - started
    st.session_state.pop("formal_report", None)
    st.session_state.pop("quote_save", None)
    st.session_state.drawing_run = {
        "run_id": uuid4().hex,
        "filename": filename,
        "inquiry": inquiry,
        "result": result,
        "seconds": elapsed,
        "issued": datetime.now(DAINAN_TIME).strftime("%Y-%m-%d %H:%M:%S"),
        "followed_up": followed_up,
        "demo": demo,
        "source_quote_id": source_quote_id,
        "source_line_position": source_line_position,
        "drawing": st.session_state.get("inquiry_drawing"),
        "order_edit_token": st.session_state.get("order_edit", {}).get("token"),
    }
    if not st.session_state.get("inquiry_validity", {}).get("custom"):
        reset_inquiry_validity(quote_valid_until(st.session_state.drawing_run["issued"]))
    st.session_state.drawing_run["valid_until"] = selected_quote_deadline()
    work_draft_changed(inquiry=True)


def select_sample() -> None:
    work_draft_changed(inquiry=True)
    clear_quote()
    st.session_state.pop("order_edit", None)
    reset_inquiry_customer()
    reset_inquiry_validity()
    st.session_state.manual_entry = False
    st.session_state.pop("drawing_review", None)
    filename = st.session_state.sample
    st.session_state.inquiry_drawing = sample_drawing(filename)
    if filename:
        save_run(filename, parse_drawing_image(filename=filename), demo=True)


def select_preset(preset_key: str) -> None:
    st.session_state.pop("order_edit", None)
    reset_inquiry_customer()
    reset_inquiry_validity()
    inquiry = DEMO_PRESETS[preset_key].copy()
    filename = f'{inquiry["id"]}.png'
    st.session_state.inquiry_drawing = sample_drawing(filename)
    st.session_state.sample = filename
    st.session_state.manual_entry = False
    st.session_state.pop("drawing_review", None)
    save_run(filename, inquiry, demo=True)


def upload_changed(upload_key: str) -> None:
    upload = st.session_state.get(upload_key)
    if upload or not st.session_state.get("sample"):
        work_draft_changed(inquiry=True)
        review = st.session_state.get("drawing_review", {})
        context = {key: review[key] for key in ("source_quote_id", "source_line_position", "demo") if key in review}
        if not context.get("source_quote_id"):
            reset_inquiry_customer()
            if not st.session_state.get("order_edit"):
                reset_inquiry_validity()
        st.session_state.pop("inquiry_drawing", None)
        st.session_state.sample = None
        st.session_state.manual_entry = False
        clear_quote()
        st.session_state.pop("drawing_review", None)
        if upload:
            begin_review(**context)


def start_manual_entry() -> None:
    clear_quote()
    st.session_state.pop("order_edit", None)
    reset_inquiry_customer()
    reset_inquiry_validity()
    st.session_state.manual_entry = True
    st.session_state.pop("inquiry_drawing", None)
    begin_review(source="手动录入")


def begin_review(started: float | None = None, **recognition) -> None:
    st.session_state.review_revision = st.session_state.get("review_revision", 0) + 1
    review = {
        "started": perf_counter() if started is None else started,
        "source": "手工填写",
        "fields": {},
        **recognition,
    }
    review["initial_issues"] = drawing_issues(review_values(review["fields"])) if review["fields"] else []
    st.session_state.drawing_review = review
    work_draft_changed(inquiry=True)


def prefill_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 2**53 - 1 else None


def review_values(fields: dict) -> dict:
    material = fields.get("material")
    shape = fields.get("shape")
    quantity = prefill_number(fields.get("quantity"))
    return {
        "material": material.strip().upper() if isinstance(material, str) and len(material.strip()) <= 24 else "",
        "shape": shape if isinstance(shape, str) and shape in SHAPES else None,
        "quantity": int(quantity) if quantity and quantity.is_integer() else None,
        **{field: prefill_number(fields.get(field)) for field in REVIEW_DIMENSIONS},
    }


def drawing_issues(inquiry: dict) -> list[str]:
    material, shape = inquiry["material"], inquiry["shape"]
    issues = []
    if not material or (material not in DEFAULT_MATERIAL_RULES
                        and load_material_rule(material) is DEFAULT_MATERIAL_RULES["304"]):
        issues.append("材质牌号（须有计价规则）")
    if shape not in SHAPES:
        issues.append("零件形状")
    if not inquiry["outer_dim_mm"]:
        issues.append(REVIEW_DIMENSIONS["outer_dim_mm"])
    if not inquiry["thickness_mm"]:
        issues.append(REVIEW_DIMENSIONS["thickness_mm"])
    if not inquiry["quantity"]:
        issues.append("正整数采购数量")
    if shape == "plate" and not inquiry["width_mm"]:
        issues.append(REVIEW_DIMENSIONS["width_mm"])
    if shape == "flange":
        inner, outer = inquiry["inner_dim_mm"], inquiry["outer_dim_mm"]
        if inner is None or (outer and inner >= outer):
            issues.append("法兰中心孔内径（小于外径，实心填 0）")
    return issues


def parse_upload(filename: str, raw_bytes: bytes, preview: Image.Image, image_format: str) -> dict:
    sample = DRAWINGS.get(f"{Path(filename).stem.upper()}{Path(filename).suffix.lower()}")
    if sample and sample.is_file() and raw_bytes == sample.read_bytes():
        return {"fields": parse_drawing_image(filename=sample.name), "source": "预设参数"}
    if image_format != "PNG":
        png = io.BytesIO()
        preview.save(png, format="PNG")
        raw_bytes = png.getvalue()
    started = perf_counter()
    recognition = recognize_new_drawing(raw_bytes)
    return {**recognition, "source": "手工填写" if recognition["error"] else "在线识图",
            "vision_seconds": perf_counter() - started}


def sample_drawing(filename: str | None) -> dict | None:
    path = DRAWINGS.get(filename)
    try:
        return validate_drawing_image(path.read_bytes(), path.name) if path and path.is_file() else None
    except (OSError, ValueError):
        return None


def render_drawing_view(drawing: dict | None, context: str, *, thumbnail: bool = True) -> None:
    if drawing is None:
        st.caption("无留存图纸")
        return
    filename = drawing["filename"]
    if thumbnail:
        st.image(drawing["data"], caption=filename, alt=f"留存原图 {filename}", width=360)
    encoded = base64.b64encode(drawing["data"]).decode("ascii")
    panel_id = f"drawing-view-{context}"
    st.html(f"""
      <div id="{escape(panel_id)}" class="drawing-view">
        <button type="button" data-open>查看大图</button>
        <dialog class="drawing-dialog" aria-label="图纸大图">
          <div class="drawing-toolbar">
            <strong>{escape(filename)}</strong>
            <div class="drawing-tools">
              <button type="button" data-fit>适应窗口</button>
              <button type="button" data-out aria-label="缩小图纸">−</button>
              <output aria-live="polite"></output>
              <button type="button" data-in aria-label="放大图纸">＋</button>
              <button type="button" data-rotate>旋转 90°</button>
              <button type="button" data-close>关闭大图</button>
            </div>
          </div>
          <div class="drawing-viewport" tabindex="0" aria-label="图纸画布">
            <div class="drawing-canvas"><div class="drawing-stage">
              <img src="data:{drawing['mime_type']};base64,{encoded}" alt="{escape(filename)}">
            </div></div>
          </div>
          <p class="drawing-help">放大后可拖动或滚动查看 · Esc 关闭 · 预览操作不改变原图</p>
        </dialog>
      </div>
    """ + r"""
      <script>
        (() => {
          const panel = document.getElementById(__PANEL_ID__);
          const dialog = panel.querySelector('dialog');
          const viewport = panel.querySelector('.drawing-viewport');
          const stage = panel.querySelector('.drawing-stage');
          const img = panel.querySelector('img');
          const output = panel.querySelector('output');
          let scale = 1, angle = 0, fitting = true, drag = null;
          function draw() {
            const rotated = angle % 180 !== 0;
            const width = rotated ? img.naturalHeight : img.naturalWidth;
            const height = rotated ? img.naturalWidth : img.naturalHeight;
            stage.style.width = `${width * scale}px`;
            stage.style.height = `${height * scale}px`;
            img.style.width = `${img.naturalWidth}px`;
            img.style.height = `${img.naturalHeight}px`;
            img.style.transform = `translate(-50%, -50%) rotate(${angle}deg) scale(${scale})`;
            output.textContent = `${Math.round(scale * 100)}% · ${angle}°`;
          }
          function fit() {
            if (!img.naturalWidth || !dialog.open) return;
            fitting = true;
            const rotated = angle % 180 !== 0;
            scale = Math.min(1, (viewport.clientWidth - 32) / (rotated ? img.naturalHeight : img.naturalWidth),
                               (viewport.clientHeight - 32) / (rotated ? img.naturalWidth : img.naturalHeight));
            draw();
            viewport.scrollTo(0, 0);
          }
          panel.querySelector('[data-open]').onclick = () => {
            angle = 0;
            dialog.showModal();
            fit();
          };
          img.onload = fit;
          panel.querySelector('[data-fit]').onclick = fit;
          for (const [action, factor] of [['in', 1.25], ['out', 0.8]]) {
            panel.querySelector(`[data-${action}]`).onclick = () => {
              fitting = false;
              scale = Math.max(0.02, Math.min(8, scale * factor));
              draw();
            };
          }
          panel.querySelector('[data-rotate]').onclick = () => {
            angle = (angle + 90) % 360;
            fitting ? fit() : draw();
          };
          panel.querySelector('[data-close]').onclick = () => dialog.close();
          viewport.onpointerdown = event => {
            if (event.button !== 0) return;
            drag = {x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop};
            viewport.setPointerCapture(event.pointerId);
          };
          viewport.onpointermove = event => {
            if (drag) viewport.scrollTo(drag.left + drag.x - event.clientX, drag.top + drag.y - event.clientY);
          };
          viewport.onpointerup = viewport.onpointercancel = () => { drag = null; };
          img.ondragstart = () => false;
        })();
      </script>
    """.replace("__PANEL_ID__", json.dumps(panel_id)), unsafe_allow_javascript=True)


def render_retained_drawing(quote_id: int, line_position: int = 1) -> None:
    try:
        drawing = get_quote_drawing(quote_id, line_position)
    except (OSError, sqlite3.Error, ValueError):
        st.warning("留存原图暂时无法读取，请稍后重试。")
        return
    render_drawing_view(drawing, f"history-{quote_id}-{line_position}")


def recognize_inquiry_drawing(filename: str, raw_bytes: bytes, preview: Image.Image, image_format: str) -> None:
    if not st.button("识别图纸参数", type="primary", width="stretch"):
        return
    clear_quote()
    work_draft_changed(inquiry=True)
    started = st.session_state.drawing_review["started"]
    try:
        with st.spinner("正在提取图纸参数…"):
            recognition = parse_upload(filename, raw_bytes, preview, image_format)
    except ValueError as error:
        recognition = {"error": str(error)}
    except OSError:
        recognition = {"error": "无法准备图纸，请根据原图手工填写。"}
    if recognition.get("error"):
        # 失败时保留当前表单身份，避免清空人工填写的参数。
        review = st.session_state.drawing_review
        review["error"] = recognition["error"]
        review.pop("confirmed", None)
        review.pop("end_to_end_seconds", None)
    else:
        context = {key: st.session_state.drawing_review[key]
                   for key in ("source_quote_id", "source_line_position", "demo", "filename", "restored_draft",
                               "restored_reply_review")
                   if key in st.session_state.drawing_review}
        if recognition.get("source") == "预设参数":
            context["demo"] = True
        begin_review(started, **recognition, **context)


def render_inputs() -> str | None:
    if st.session_state.get("drawing_run") or st.session_state.get("drawing_review"):
        heading, new = st.columns((2, 1), gap="small", vertical_alignment="center")
        heading.html('<h2 class="panel-heading">图纸与询盘</h2>')
        new.button("新询盘", key="new-inquiry-action", on_click=start_new_inquiry, width="stretch")
    else:
        st.html('<h2 class="panel-heading">图纸与询盘</h2>')
    revision = st.session_state.get("upload_revision", 0)
    upload_key = f"upload_{revision}" if revision else "upload"
    upload = st.file_uploader("上传图纸", type=["png", "jpg", "jpeg", "webp"],
                              key=upload_key, on_change=upload_changed, args=(upload_key,))
    manual_entry = st.session_state.get("manual_entry", False)
    if not upload and not st.session_state.get("sample") and not manual_entry:
        limit_mb = st.get_option("server.maxUploadSize")
        st.caption(f"微信草图或 CAD 截图 · PNG、JPG、WebP · 单张 ≤ {limit_mb} MB")
        st.button("手动录入", key="manual_entry_action", on_click=start_manual_entry, width="stretch")
    with st.expander("使用样例"):
        sample = st.selectbox("选择图纸样例", tuple(DRAWINGS), index=None, key="sample",
                              placeholder="选择 TP-001 至 TP-015", on_change=select_sample)
        for preset_key, label in SCRIPTS:
            key = "judge_start" if preset_key == "PRESET_2_MISSING_THICK" else preset_key
            st.button(label, key=key, on_click=select_preset, args=(preset_key,), width="stretch")

    if sample:
        st.caption("参数来源 · 预设参数")
        if DRAWINGS[sample].is_file():
            st.image(str(DRAWINGS[sample]), caption=sample, alt=f"样例原图 {sample}", width="stretch")
            render_drawing_view(st.session_state.get("inquiry_drawing"), "current", thumbnail=False)
        else:
            st.warning(f"找不到测试图纸 {sample}。")
    elif upload:
        filename = Path(upload.name).name
        raw_bytes = upload.getvalue()
        try:
            current = st.session_state.get("inquiry_drawing")
            if current is None or current["data"] != raw_bytes:
                st.session_state.inquiry_drawing = validate_drawing_image(raw_bytes, filename)
            with Image.open(io.BytesIO(raw_bytes), formats=("PNG", "JPEG", "WEBP")) as drawing:
                image_format = drawing.format
                preview = ImageOps.exif_transpose(drawing).convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            st.session_state.pop("inquiry_drawing", None)
            st.error("无法打开图片，请上传有效的 PNG、JPEG 或 WebP 图纸。")
            return
        st.image(preview, caption=filename, alt=f"上传图纸 {filename}", width="stretch")
        render_drawing_view(st.session_state.inquiry_drawing, "current", thumbnail=False)
        if "drawing_review" not in st.session_state:
            begin_review()
        recognize_inquiry_drawing(filename, raw_bytes, preview, image_format)
        return filename
    elif manual_entry:
        filename = st.session_state.drawing_review.get("filename", "手动询盘")
        if filename != "手动询盘":
            st.html(f'<p class="record-meta">原单图纸 · {escape(filename)}</p>')
        render_drawing_view(st.session_state.get("inquiry_drawing"), "current")
        drawing = st.session_state.get("inquiry_drawing")
        if drawing and st.session_state.drawing_review.get("restored_draft"):
            with Image.open(io.BytesIO(drawing["data"])) as original:
                preview = ImageOps.exif_transpose(original).convert("RGB")
                recognize_inquiry_drawing(filename, drawing["data"], preview, original.format)
        return filename
    return None


def render_copy_message(message: str, label: str, panel_id: str, *, show_text: bool = True) -> None:
    hidden = "" if show_text else " hidden"
    st.html(f"""
      <div id="{escape(panel_id)}" class="followup-copy">
        <p data-copy-text{hidden}>{escape(message)}</p>
        <div class="followup-actions">
          <button type="button">{escape(label)}</button>
          <span role="status"></span>
        </div>
      </div>
      <script>
        (() => {{
          const panel = document.getElementById({json.dumps(panel_id)});
          const text = panel.querySelector("[data-copy-text]");
          const status = panel.querySelector('[role="status"]');
          panel.querySelector("button").onclick = async () => {{
            try {{
              await navigator.clipboard.writeText(text.textContent);
              status.textContent = "已复制";
            }} catch {{
              text.hidden = false;
              status.textContent = "请选中文本后手动复制。";
            }}
          }};
        }})();
      </script>
    """, unsafe_allow_javascript=True)


def render_followup_message(message: str) -> None:
    render_copy_message(message, "复制追问话术", "followup-copy")


def render_reply_comparison(items: list[ReplyItem]) -> None:
    rows = []
    for item in items:
        status_class = "changed" if item.status == CHANGED else "review" if item.status == NEEDS_REVIEW else "same"
        rows.append(
            f'<tr><th scope="row">{escape(item.label)}</th><td>{escape(item.original)}</td>'
            f'<td>{escape(item.suggestion)}</td><td><span class="reply-status {status_class}">'
            f'{escape(item.status)}</span></td></tr>'
        )
    st.html(f"""
      <div class="record-table reply-table" role="region" aria-label="客户回复修改项对照" tabindex="0">
        <table><thead><tr><th scope="col">字段</th><th scope="col">原值</th>
          <th scope="col">建议值</th><th scope="col">状态</th></tr></thead>
          <tbody>{"".join(rows)}</tbody></table>
      </div>
    """)


def invalidate_review_quote() -> None:
    work_draft_changed(inquiry=True)
    clear_quote()
    review = st.session_state.drawing_review
    for field in ("confirmed", "end_to_end_seconds", "issues"):
        review.pop(field, None)


def draft_dimension_changed(shape: str, field: str, key: str) -> None:
    review = st.session_state.drawing_review
    review.setdefault("shape_drafts", {}).setdefault(shape, {})[field] = st.session_state[key]
    invalidate_review_quote()


def customer_reply_changed() -> None:
    invalidate_review_quote()
    st.session_state.drawing_review.pop("reply_analysis", None)


def render_customer_reply(review: dict, draft_keys: dict[str, str], form_key: str,
                           shape: str | None) -> tuple[list[ReplyItem], str]:
    with st.container(key="customer-reply"):
        reply = st.text_area("粘贴客户回复", key=f"{form_key}_reply", max_chars=MAX_REPLY_LENGTH,
                             placeholder="例如 改成 30 件，厚度还是 12，要开专票",
                             on_change=customer_reply_changed).strip()
        st.caption("点击识别时将发送给已配置的模型。工作台仅在当前会话保留回复原文。")
        fingerprint = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        if st.button("识别修改项", key=f"{form_key}_recognize", disabled=not reply):
            clear_quote()
            review.pop("confirmed", None)
            analysis = {"fingerprint": fingerprint, "id": uuid4().hex}
            try:
                with st.spinner("正在核对客户回复…"):
                    analysis["items"] = recognize_quote_reply(reply, review["fields"])
            except ValueError as error:
                analysis["error"] = str(error)
            review["reply_analysis"] = analysis
            work_draft_changed(inquiry=True)

        analysis = review.get("reply_analysis")
        if not analysis or analysis["fingerprint"] != fingerprint:
            if analysis and reply:
                st.caption("回复已修改。可重新识别，或在下方按新回复手动核对。")
            return [], ""
        if "error" in analysis:
            st.warning(analysis["error"])
            return [], ""
        items = analysis["items"]
        if not items:
            st.info("回复中没有识别到改价项。可在下方手动填写。")
            return [], ""
        render_reply_comparison(items)
        if any(item.field == "invoice" for item in items):
            st.caption("已记录开票要求。过磅或理计口径由人工确认，系统继续提供 A/B 双轨报价。")
        if any(item.field == "other" for item in items):
            st.caption("其他改价要求需人工核对。附加孔和特殊工艺费用请另行核价。")
        changes = [item for item in items if item.status == CHANGED and item.field in draft_keys]
        if changes and st.button("应用到草稿", key=f"{form_key}_apply"):
            clear_quote()
            review.pop("confirmed", None)
            for item in changes:
                st.session_state[draft_keys[item.field]] = item.value
                if item.field == "thickness_mm":
                    review["shape_drafts"][shape][item.field] = item.value
            analysis["applied"] = True
            work_draft_changed(inquiry=True)
        if analysis.get("applied"):
            st.success("建议已填入草稿。核对参数后点击「确认参数并核价」。")
        return items, f'{form_key}_reply_checked_{analysis["id"]}'


def source_quote_snapshot(quote_id: int, line_position: int | None = None) -> dict | None:
    try:
        record = get_quote(quote_id)
    except (OSError, sqlite3.Error, ValueError):
        return None
    snapshot = record["snapshot"] if record else None
    if snapshot and snapshot.get("kind") == "order" and type(line_position) is int:
        lines = snapshot.get("items", [])
        snapshot = lines[line_position - 1] if 1 <= line_position <= len(lines) else None
    return snapshot if snapshot and "inquiry" in snapshot and "quote" in snapshot else None


def comparison_parameters(inquiry: dict) -> dict[str, str]:
    quantity = prefill_number(inquiry.get("quantity"))
    facts = {
        "材质牌号": str(inquiry.get("material") or "待填写"),
        "零件形状": SHAPES.get(inquiry.get("shape"), "待选择"),
        "数量": f"{quantity:g} 件" if quantity else "待填写",
    }
    for field, label in SHAPE_DIMENSIONS.get(inquiry.get("shape"), ()):
        value = prefill_number(inquiry.get(field))
        facts[label] = f"{value:g} mm" if value is not None else "待填写"
    return facts


def parameter_changes(original: dict, current: dict) -> list[tuple[str, str, str]]:
    before, after = comparison_parameters(original), comparison_parameters(current)
    labels = dict.fromkeys((*before, *after))
    return [(label, before.get(label, "—"), after.get(label, "—")) for label in labels
            if before.get(label, "—") != after.get(label, "—")]


def render_change_table(rows: list[tuple[str, str, str]], current_label: str, table_class: str) -> None:
    markup = "".join(
        f'<tr><th scope="row">{escape(label)}</th><td class="mono">{escape(before)}</td>'
        f'<td class="mono">{escape(after)}</td></tr>'
        for label, before, after in rows
    )
    st.html(f'<div class="change-table {table_class}" role="region" aria-label="本次改动对照">'
            f'<table><thead><tr><th>字段</th><th>原单</th><th>{current_label}</th></tr></thead>'
            f'<tbody>{markup}</tbody></table></div>')


def render_draft_changes(source_id: int, inquiry: dict, source_line_position: int | None = None) -> None:
    st.html('<h3 class="change-heading">本次改动</h3>')
    source = source_quote_snapshot(source_id, source_line_position)
    if source is None:
        st.caption("来源报价数据不可用。核对当前草稿后可继续核价。")
        return
    rows = parameter_changes(source["inquiry"], inquiry)
    if rows:
        render_change_table(rows, "当前草稿", "draft-changes")
    else:
        st.caption("当前草稿与原单参数一致。")


def render_quote_changes(source_id: int | None, snapshot: dict) -> None:
    if source_id is None:
        return
    st.html('<h3 class="change-heading">原单与本次报价</h3>')
    source = source_quote_snapshot(source_id, snapshot.get("source_line_position"))
    if source is None:
        st.caption("来源报价数据不可用。当前报价可正常查看和导出。")
        return
    rows = parameter_changes(source["inquiry"], snapshot["inquiry"])
    bases = (
        ("售价毛利率", source.get("gross_margin_percent"), snapshot.get("gross_margin_percent"), "%"),
        ("税前基准钢价", source["quote"].get("当日税前基准钢价_元每kg"),
         snapshot["quote"].get("当日税前基准钢价_元每kg"), "元/kg"),
        ("下差系数", source.get("tolerance"), snapshot.get("tolerance"), ""),
    )
    for label, before, after, unit in bases:
        if before != after:
            old = f"{before:g} {unit}".strip() if before is not None else "未记录"
            new = f"{after:g} {unit}".strip() if after is not None else "未记录"
            rows.append((label, old, new))
    rows.append(("报价时间（北京时间）", source["issued"], snapshot["issued"]))
    rows.extend((label, f'¥{source["quote"][field]:,.2f}', f'¥{snapshot["quote"][field]:,.2f}')
                for label, field in QUOTE_OPTIONS)
    render_change_table(rows, "本次", "quote-changes")


def render_review(filename: str) -> None:
    review = st.session_state.drawing_review
    values = review_values(review["fields"])
    form_key = f'confirm_drawing_{st.session_state.review_revision}'
    common_keys = {field: f"{form_key}_{field}" for field in ("material", "shape", "quantity")}
    for field, key in common_keys.items():
        if key not in st.session_state:
            st.session_state[key] = values[field]
    # Streamlit 会清理隐藏控件，另存各形状尺寸以便切回恢复。
    shape_drafts = review.setdefault("shape_drafts", {})
    if values["shape"] and values["shape"] not in shape_drafts:
        shape_drafts[values["shape"]] = {field: values[field] for field, _ in SHAPE_DIMENSIONS[values["shape"]]}
    reply_items, reply_check_key = [], ""
    st.caption(f'参数来源 · {review["source"]}。核对规格后提交。')
    reply_panel = st.container()
    if source_quote_id := review.get("source_quote_id"):
        st.caption(f"来源报价 · {quote_number(source_quote_id)}")
    if review.get("error"):
        st.warning(review["error"])
    field_feedback = {}
    with st.container():
        material_column, shape_column = st.columns(2, gap="small")
        inquiry = {field: None for field in REVIEW_DIMENSIONS}
        with material_column:
            inquiry["material"] = st.text_input("材质牌号", key=common_keys["material"], max_chars=24,
                                                on_change=invalidate_review_quote)
            field_feedback["材质牌号"] = st.empty()
        with shape_column:
            inquiry["shape"] = st.selectbox("零件形状", tuple(SHAPES), index=None, key=common_keys["shape"],
                                            format_func=SHAPES.__getitem__, placeholder="选择形状",
                                            on_change=invalidate_review_quote)
            field_feedback["零件形状"] = st.empty()
        shape = inquiry["shape"]
        dimension_keys = {field: f"{form_key}_{shape}_{field}" for field, _ in SHAPE_DIMENSIONS.get(shape, ())}
        dimensions = shape_drafts.setdefault(shape, {}) if shape else {}
        for field, key in dimension_keys.items():
            if key not in st.session_state:
                st.session_state[key] = dimensions.get(field)
        if source_quote_id:
            applicable = {"quantity": common_keys["quantity"]}
            if shape in ("plate", "flange"):
                applicable["thickness_mm"] = dimension_keys["thickness_mm"]
            with reply_panel:
                reply_items, reply_check_key = render_customer_reply(review, applicable, form_key, shape)
        if shape == "flange":
            st.caption("实心圆片请明确填写中心孔内径 0 mm。")
        elif shape is None:
            st.caption("请先选择零件形状，再填写对应尺寸。")
        dimension_columns = st.columns(2, gap="small")
        for index, (field, label) in enumerate(SHAPE_DIMENSIONS.get(shape, ())):
            with dimension_columns[index % 2]:
                key = dimension_keys[field]
                inquiry[field] = st.number_input(label, min_value=0.0, value=None, key=key,
                                                 on_change=draft_dimension_changed, args=(shape, field, key))
                field_feedback[REVIEW_DIMENSIONS[field]] = st.empty()
        inquiry["quantity"] = st.number_input("采购数量（件）", min_value=1,
                                              value=None, key=common_keys["quantity"],
                                              on_change=invalidate_review_quote)
        field_feedback["采购数量"] = st.empty()
        manual_checks = [item for item in reply_items if item.field != "invoice" and item.status in (CHANGED, NEEDS_REVIEW)]
        reply_checked = bool(manual_checks) and st.checkbox(
            "已核对客户回复中的差异和加工要求，按表单参数报价", key=reply_check_key
        )
        restored_reply_checked = True
        if review.get("restored_reply_review"):
            st.warning("上次客户回复含改价或待核对要求。回复原文仅在原会话保留，请重新核对后确认。")
            restored_reply_checked = st.checkbox("已重新核对客户改价要求", key=f"{form_key}_restored_reply_checked")
        if source_quote_id:
            render_draft_changes(source_quote_id, review_values(inquiry), review.get("source_line_position"))
        submitted = st.button("确认参数并核价", key=f"{form_key}_confirm", type="primary", width="stretch")

    if submitted:
        clear_quote()
        inquiry = review_values(inquiry)
        review.pop("confirmed", None)
        review.pop("end_to_end_seconds", None)
        review["issues"] = drawing_issues(inquiry)
        needs_reply_check = any(
            item.status == NEEDS_REVIEW or inquiry.get(item.field) != item.value
            or (item.field == "thickness_mm" and inquiry["shape"] == "round_bar")
            for item in manual_checks
        )
        if not restored_reply_checked or (needs_reply_check and not reply_checked):
            st.error("请先核对客户回复中的改动、待确认项及加工要求，再确认表单。")
        elif not review["issues"]:
            # 清除与当前形状无关的输入，避免尺寸随旧形状残留。
            if inquiry["shape"] != "plate":
                inquiry["width_mm"] = 0.0
            if inquiry["shape"] != "flange":
                inquiry["inner_dim_mm"] = 0.0
            try:
                save_run(filename, inquiry, demo=review.get("demo", review["source"] == "预设参数"),
                         source_quote_id=review.get("source_quote_id"), source_line_position=review.get("source_line_position"))
            except (ArithmeticError, ValueError, OSError):
                st.error("核价未完成，请核对图纸尺寸及计价数据。")
            else:
                review["confirmed"] = inquiry

    issues = review.get("issues")
    if issues is None:
        issues = drawing_issues(review_values(inquiry)) if review["initial_issues"] else []
        review["issues"] = issues
    if issues:
        missing = "、".join(issues)
        for index, issue in enumerate(issues):
            anchor = next((slot for label, slot in field_feedback.items() if label in issue), st)
            with anchor.container():
                st.html(f'<p class="field-warning">待确认 · {escape(issue)}</p>')
                if index == 0:
                    render_followup_message(f"您好，这张图纸还需确认【{missing}】。麻烦补一下标注，我们核对后给您报价。")


def render_sample_parameters(run: dict) -> None:
    inquiry = run["inquiry"]
    shape = inquiry["shape"]
    values = [
        ("材质牌号", inquiry["material"]),
        ("零件形状", SHAPES[shape]),
        ("长度（mm）" if shape == "plate" else "直径（mm）" if shape == "round_bar" else "外径（mm）",
         f'{inquiry["outer_dim_mm"]:g}'),
    ]
    if shape == "plate":
        values.append(("宽度（mm）", f'{inquiry["width_mm"]:g}'))
    elif shape == "flange":
        values.append(("中心孔内径（mm）", f'{inquiry["inner_dim_mm"]:g}'))
    if inquiry["thickness_mm"]:
        values.append(("轴向长度（mm）" if shape == "round_bar" else "板厚（mm）", f'{inquiry["thickness_mm"]:g}'))
    values.append(("采购数量（件）", str(inquiry["quantity"])))
    rows = "".join(f'<div><dt>{label}</dt><dd class="mono">{escape(value)}</dd></div>' for label, value in values)
    st.html(f'<dl class="parameter-facts">{rows}</dl>')


def render_run_metrics(run: dict, review: dict | None) -> None:
    if "seconds" not in run and not (review and review.get("model")):
        return
    labels = []
    if "seconds" in run:
        labels.extend((f'本地核价 {run["seconds"]:.3f}s', "本地算料 Token 成本 ¥0.000"))
    if review and review.get("model"):
        total = review["usage"]["total_tokens"]
        usage = f"{total} Token" if total is not None else "用量未回传"
        labels.append(f'在线识图 {review["vision_seconds"]:.3f}s · {usage}')
    if review and "end_to_end_seconds" in review:
        labels.append(f'收图至出价 {review["end_to_end_seconds"]:.2f}s（含人工核对）')
    badges = "".join(f'<span class="badge">{label}</span>' for label in labels)
    st.html(f'<div class="run-badges">{badges}</div>')


def scrap_narration(scrap: dict, quantity: int) -> str:
    if scrap.get("撮合状态") == SCRAP_REVIEW_STATUS:
        return scrap["撮合播报话术"]
    match = scrap.get("最优拼单推荐")
    if not match:
        return f'{scrap["撮合播报话术"]}本单共 {quantity} 件，预计可拼 0 件。'
    eligible = match["可拼单件数"]
    return (
        f'距戴南中心 {match["直线距离_km"]:g} km 的【{match["所在位置"]}·{match["合作工厂"]}】'
        f'现有 {match["余料规格"]}。{quantity} 件中预计 {eligible} 件可用余料，'
        f'这 {eligible} 件预计节省材料费 ¥{match["直接节省金额_元"]:,.2f}'
        f'（{match["节省比例_%"]:g}%）。{SCRAP_CUT_NOTE}'
    )


def render_timeline(run: dict, quote: dict, reviewing: bool = False) -> None:
    result = run.get("result", {})
    steps = result.get("agent_thoughts", [])
    cards = []
    for index, name in enumerate(AGENTS):
        if index >= len(steps):
            status, detail, css = "待处理", "等待前序步骤。" if index else "等待图纸或样例。", "pending"
            if reviewing and index == 0:
                status, detail, css = "待人工确认", "请核对原图并提交参数，确认后进入核价。", "waiting"
        else:
            step = steps[index]
            name, status, detail = step["agent"], step["status"], step["detail"]
            css = "waiting" if status in ("拦截追问", SCRAP_REVIEW_STATUS) else "active"
            if index == 1 and quote:
                price_a, price_b = (quote[key] for _, key in QUOTE_OPTIONS)
                detail = (f"算料引擎当前双轨报价：理计不含税 ¥{price_a:,.2f}，"
                          f"过磅含13%专票 ¥{price_b:,.2f}。材料利用率 {quote['材料利用率_%']:g}%。")
            elif index == 2 and quote:
                detail = "撮合结果见「内部核价明细」。"
        cards.append(
            f'<li class="timeline-step {css}" style="animation-delay:{index * 90}ms">'
            f'<div class="step-title"><strong>{escape(name)}</strong><span class="badge">{escape(status)}</span></div>'
            f'<p>{escape(detail)}</p></li>'
        )
    st.html('<ol class="timeline" aria-label="智能体处理时间轴">' + "".join(cards) + "</ol>")


def render_followup(run: dict) -> None:
    result = run["result"]
    with st.container(key="followup-panel"):
        if "板材厚度(mm)" not in result["missing_fields"]:
            st.html('<p class="field-warning">待确认 · 询盘规格</p>')
            render_followup_message(result["followup_msg"])
            return
        st.html('<p class="field-warning">待确认 · 缺少板厚</p>')
        with st.form("thickness_form", border=False, enter_to_submit=False):
            thickness = st.number_input("板厚（mm）", min_value=0.0, value=10.0, step=0.1, format="%.1f")
            render_followup_message(result["followup_msg"])
            submit = st.form_submit_button("确认厚度并生成报价", type="primary", width="stretch")
        if not submit:
            return
        if thickness is None or not math.isfinite(thickness) or thickness <= 0:
            st.error("请输入大于 0 的有效厚度。")
            return
        inquiry = {**run["inquiry"], "thickness_mm": thickness}
        try:
            save_run(run["filename"], inquiry, followed_up=True, demo=run.get("demo", False))
        except (ArithmeticError, ValueError):
            st.error("该厚度未能完成核价，请核对输入值。")
            return
        st.rerun()


def quote_valid_until(issued: str) -> str:
    return (datetime.fromisoformat(issued) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")


def quote_rows(run: dict, quote: dict, scrap: dict, tolerance: float,
               gross_margin_percent: int) -> list[tuple[str, object, str]]:
    inquiry = run["inquiry"]
    shape = inquiry["shape"]
    match = scrap.get("最优拼单推荐")
    rows = [
        ("报价时间（北京时间）", run["issued"], ""),
        ("有效期至（北京时间）", run.get("valid_until", quote_valid_until(run["issued"])), ""),
        ("图纸", run["filename"], ""),
        ("材质牌号", quote["材质"], ""),
        ("零件形状", SHAPES[shape], ""),
        ("订单总件数", inquiry["quantity"], "件"),
        ("售价毛利率", gross_margin_percent, "%"),
        ("长度" if shape == "plate" else "外径", inquiry["outer_dim_mm"], "mm"),
        ("轴向长度" if shape == "round_bar" else "厚度", inquiry["thickness_mm"], "mm"),
    ]
    if run.get("demo"):
        rows.insert(0, ("报价类型", DEMO_QUOTE_TITLE, ""))
    if shape == "plate":
        rows.append(("宽度", inquiry["width_mm"], "mm"))
    if shape == "flange":
        rows.extend((
            ("中心孔内径", inquiry["inner_dim_mm"], "mm"),
            ("加工范围说明", FLANGE_CUTTING_NOTE, ""),
        ))
    rows.extend((
        ("订单理论毛重", quote["订单总毛重_理计_kg"], "kg"),
        ("预估过磅重（含下差）", quote["订单预估过磅重_含下差_kg"], "kg"),
        ("常规下差系数", tolerance, ""),
        ("材料利用率", quote["材料利用率_%"], "%"),
        ("税前基准钢价", quote["当日税前基准钢价_元每kg"], "元/kg"),
        ("激光加工费", f'{quote["激光加工费_元"]:.2f}', "元"),
        ("单件含税均价", f'{quote["单件含税均价_元"]:.2f}', "元/件"),
    ))
    rows.extend((label, f"{quote[key]:.2f}", "元") for label, key in QUOTE_OPTIONS)
    if scrap.get("撮合状态") == SCRAP_REVIEW_STATUS:
        rows.extend((
            ("余料拼单状态", SCRAP_REVIEW_STATUS, ""),
            ("余料复核说明", scrap["撮合播报话术"], ""),
        ))
    else:
        rows.append(("预计可拼件数", match["可拼单件数"] if match else 0, "件"))
    if match:
        rows.extend((
            ("余料供应方", match["合作工厂"], ""),
            ("余料位置", match["所在位置"], ""),
            ("距戴南中心", match["直线距离_km"], "km"),
            ("预计节省材料费（按可拼件数）", f'{match["直接节省金额_元"]:.2f}', "元"),
            ("材料费节省比例（按可拼件数）", match["节省比例_%"], "%"),
            ("余料估算说明", SCRAP_CUT_NOTE, ""),
        ))
    recovery = quote["余料回流卡片"]
    if recovery:
        rows.extend((
            ("圆芯规格", recovery["产生余料规格"], ""),
            ("圆芯可回收总重", recovery["可回收总重_kg"], "kg"),
            ("圆芯预计回血金额", f'{recovery["预计回血金额_元"]:.2f}', "元"),
        ))
    return rows


def quotation_csv(rows: list[tuple[str, object, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("报价项目", "数值", "单位"))
    for label, value, unit in rows:
        # 图纸名及表格中的文本可能触发电子表格公式。
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            value = "'" + value
        writer.writerow((label, value, unit))
    return output.getvalue().encode("utf-8-sig")


def quotation_markup(rows: list[tuple[str, object, str]], title: str = "正式报价单",
                      *, terms: Sequence[str] = INTERNAL_QUOTE_TERMS, detail_markup: str = "") -> str:
    lines = []
    price_labels = {label for label, _ in QUOTE_OPTIONS}.union(CUSTOMER_PLAN_LABELS, ORDER_PLAN_LABELS)
    for label, value, unit in rows:
        highlight = ' class="total"' if label in price_labels else ""
        lines.append(f'<tr{highlight}><th scope="row">{escape(label)}</th>'
                     f'<td>{escape(str(value))}</td><td>{escape(unit)}</td></tr>')
    encoded_seal = base64.b64encode(SEAL_SVG.encode("utf-8")).decode("ascii")
    term_items = "".join(f"<li>{escape(term)}</li>" for term in terms)
    return f"""
    <article class="industrial-report" id="formal-quotation">
      <h1>戴南钢算AI · {escape(title)}</h1>
      {detail_markup}
      <table><thead><tr><th>报价项目</th><th>数值</th><th>单位</th></tr></thead><tbody>{"".join(lines)}</tbody></table>
      <section class="report-terms">
        <h2>报价条款</h2>
        <ul>{term_items}</ul>
      </section>
      <div class="seal-row"><img alt="{SEAL_TITLE}" src="data:image/svg+xml;base64,{encoded_seal}"></div>
    </article>
    """


def printable_quotation(markup: str, title: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <title>戴南钢算AI · {escape(title)}</title>{REPORT_STYLE}
    <button class="print-control" onclick="window.print()">打印 / 另存为 PDF</button>{markup}</html>"""


def customer_quote_fields(snapshot: dict, quote_id: int | None = None) -> dict:
    inquiry, quote = snapshot["inquiry"], snapshot["quote"]
    shape = inquiry["shape"]
    outer, thickness = inquiry["outer_dim_mm"], inquiry["thickness_mm"]
    if shape == "plate":
        item = SHAPES[shape]
        dimensions = [f"长度 {outer:g} mm", f'宽度 {inquiry["width_mm"]:g} mm', f"板厚 {thickness:g} mm"]
    elif shape == "round_bar":
        item = SHAPES[shape]
        dimensions = [f"直径 Φ{outer:g} mm", f"长度 {thickness:g} mm"]
    else:
        inner = inquiry["inner_dim_mm"]
        item = "法兰" if inner else "圆片"
        dimensions = [f"外径 Φ{outer:g} mm"]
        if inner:
            dimensions.append(f"中心孔 Φ{inner:g} mm")
        dimensions.append(f"板厚 {thickness:g} mm")

    # 从已确认规格和双轨金额逐项取值，内部明细不进入交付数据。
    return {
        "title": f"{DEMO_QUOTE_TITLE} · {CUSTOMER_QUOTE_TITLE}" if snapshot["demo"] else CUSTOMER_QUOTE_TITLE,
        "number": quote_number(quote_id) if quote_id is not None else "",
        "item": item, "material": quote["材质"], "spec": "、".join(dimensions),
        "quantity": inquiry["quantity"],
        "plans": tuple((label, f"{quote[key]:,.2f}")
                       for label, (_, key) in zip(CUSTOMER_PLAN_LABELS, QUOTE_OPTIONS)),
        "issued": snapshot["issued"], "valid_until": quote_deadline_text(snapshot.get("valid_until")),
        "validity_status": quote_validity(snapshot.get("valid_until")),
        "customer": clean_customer_info(snapshot.get("customer")),
        "cutting_note": FLANGE_CUTTING_NOTE if shape == "flange" else "",
    }


def customer_wechat_text(fields: dict) -> str:
    heading = " · ".join(part for part in ("戴南钢算AI", fields["title"], fields["number"]) if part)
    return "\n".join((
        heading,
        *customer_validity_lines(fields),
        *(f"{label}：{value}" for label, value, _ in customer_info_rows(fields.get("customer"))),
        f'{fields["material"]} {fields["item"]}｜{fields["spec"]}',
        f'数量 {fields["quantity"]} 件',
        *(f"{label} ¥{amount}" for label, amount in fields["plans"]),
        f'有效期至 {fields["valid_until"]}（北京时间）',
    ))


def customer_quotation_markup(fields: dict) -> str:
    rows = []
    if fields["number"]:
        rows.append(("报价编号", fields["number"], ""))
    rows.extend(customer_info_rows(fields.get("customer")))
    rows.extend((
        ("品名", fields["item"], ""),
        ("材质牌号", fields["material"], ""),
        ("规格", fields["spec"], ""),
        ("订单总件数", fields["quantity"], "件"),
        *((label, amount, "元") for label, amount in fields["plans"]),
        ("报价时间（北京时间）", fields["issued"], ""),
        ("有效期至（北京时间）", fields["valid_until"], ""),
    ))
    if fields["cutting_note"]:
        rows.append(("加工范围说明", fields["cutting_note"], ""))
    return quotation_markup(rows, fields["title"], terms=(*QUOTE_SETTLEMENT_TERMS, QUOTE_VALIDITY_TERM),
                            detail_markup=customer_validity_markup(fields))


def build_order_snapshot(items: Sequence[dict], issued: str) -> dict:
    # 以每项实际显示的两位小数为准，转成整数分分别加总。
    totals = {}
    for _, key in QUOTE_OPTIONS:
        cents = sum(int(format(item["quote"][key], ".2f").replace(".", "")) for item in items)
        whole, fraction = divmod(abs(cents), 100)
        sign = "-" if cents < 0 else ""
        totals[key] = f"{sign}{whole}.{fraction:02d}"
    return {
        "kind": "order",
        "filename": "、".join(dict.fromkeys(item["filename"] for item in items)),
        "items": list(items),
        "totals": totals,
        "issued": issued,
        "valid_until": min(item["valid_until"] for item in items),
        "demo": any(item["demo"] for item in items),
    }


def customer_order_fields(snapshot: dict, quote_id: int | None = None) -> dict:
    title = f"{ORDER_QUOTE_LABEL} · {CUSTOMER_QUOTE_TITLE}"
    return {
        "title": f"{DEMO_QUOTE_TITLE} · {title}" if snapshot["demo"] else title,
        "number": quote_number(quote_id) if quote_id is not None else "",
        "items": [customer_quote_fields(item) for item in snapshot["items"]],
        "plans": tuple((label, f'{Decimal(snapshot["totals"][key]):,.2f}')
                       for label, (_, key) in zip(ORDER_PLAN_LABELS, QUOTE_OPTIONS)),
        "issued": snapshot["issued"],
        "valid_until": quote_deadline_text(snapshot.get("valid_until")),
        "validity_status": quote_validity(snapshot.get("valid_until")),
        "customer": clean_customer_info(snapshot.get("customer")),
    }


def customer_order_wechat_text(fields: dict) -> str:
    heading = " · ".join(part for part in ("戴南钢算AI", fields["title"], fields["number"]) if part)
    lines = [heading]
    lines.extend(customer_validity_lines(fields))
    lines.extend(f"{label}：{value}" for label, value, _ in customer_info_rows(fields.get("customer")))
    for position, item in enumerate(fields["items"], 1):
        lines.append(f'{position}. {item["material"]} {item["item"]}｜{item["spec"]}｜{item["quantity"]} 件')
        lines.append(f'   方案 A ¥{item["plans"][0][1]} · 方案 B ¥{item["plans"][1][1]}')
    lines.extend(f"{label} ¥{amount}" for label, amount in fields["plans"])
    lines.append(f'整单有效期至 {fields["valid_until"]}（北京时间）')
    return "\n".join(lines)


def customer_order_markup(fields: dict) -> str:
    lines = []
    for position, item in enumerate(fields["items"], 1):
        lines.append(
            f'<tr><th scope="row">{position}</th>'
            f'<td><strong>{escape(item["material"])} {escape(item["item"])}</strong>{escape(item["spec"])}</td>'
            f'<td>{item["quantity"]}</td><td>{item["plans"][0][1]}</td><td>{item["plans"][1][1]}</td></tr>'
        )
    detail_markup = f"""
      <table class="order-report-lines" aria-label="报价明细">
        <thead><tr><th>序号</th><th>品名与规格</th><th>数量（件）</th><th>方案 A（元）</th><th>方案 B（元）</th></tr></thead>
        <tbody>{"".join(lines)}</tbody>
      </table>
    """
    rows = [("报价编号", fields["number"], "")] if fields["number"] else []
    rows.extend(customer_info_rows(fields.get("customer")))
    rows.extend((
        *((label, amount, "元") for label, amount in fields["plans"]),
        ("报价时间（北京时间）", fields["issued"], ""),
        ("整单有效期至（北京时间）", fields["valid_until"], ""),
    ))
    terms = (*QUOTE_SETTLEMENT_TERMS, "整单有效期取各项中最早的到期时间。规格或数量变更时重新核价。")
    if any(item["cutting_note"] for item in fields["items"]):
        terms = (*terms, f"法兰和圆片的{FLANGE_CUTTING_NOTE}。")
    return quotation_markup(rows, fields["title"], terms=terms,
                            detail_markup=customer_validity_markup(fields) + detail_markup)


def show_report(markup: str) -> None:
    st.session_state.formal_report = markup


def reprice(inquiry: dict, target_percent: int) -> tuple[dict, tuple[float, float, float]]:
    inputs = dict(
        material=str(inquiry["material"]).upper(), shape=inquiry.get("shape", "flange"),
        thickness_mm=float(inquiry["thickness_mm"]), quantity=int(inquiry.get("quantity") or 1),
        outer_dim_mm=float(inquiry["outer_dim_mm"]), width_mm=float(inquiry.get("width_mm") or 0.0),
        inner_dim_mm=float(inquiry.get("inner_dim_mm") or 0.0),
    )
    # 滑块按售价计算毛利率，现有引擎参数按成本加价。
    quote = calculate_stainless_quote(**inputs, profit_margin=target_percent / (100 - target_percent))
    break_even = calculate_stainless_quote(**inputs, profit_margin=0.0)
    price_a = QUOTE_OPTIONS[0][1]
    cutting = break_even["激光加工费_元"]
    return quote, (round(break_even[price_a] - cutting, 2), cutting, round(quote[price_a] - break_even[price_a], 2))


def render_costs(costs: Sequence[float], quantity: int) -> None:
    total = sum(costs)
    segments, legend = [], []
    for label, amount, color in zip(("材料费", "激光加工费", "预估毛利"), costs, ("#949A85", "#FFC388", "#D8FA91")):
        share = amount / total * 100
        segments.append(f'<span style="flex:{share:.6f};background:{color}"></span>')
        legend.append(f'<span><i style="background:{color}"></i>{label} <span class="mono">{share:.1f}% · ¥{amount:,.2f}</span></span>')
    st.html(f"""
        <section class="quote-costs">
          <strong>预计单件毛利 <span class="mono">¥{costs[-1] / quantity:,.2f}</span></strong>
          <small> · 按方案 A 税前材料与激光加工成本估算</small>
          <div class="cost-bar" aria-hidden="true">{"".join(segments)}</div>
          <div class="cost-legend">{"".join(legend)}</div>
        </section>
    """)


def render_quote_cards(quote: dict, *, totals: bool = False) -> None:
    cards = []
    for option, (label, key) in zip(("方案 A", "方案 B"), QUOTE_OPTIONS):
        price = f"¥{quote[key]:,.2f}" if quote else "—"
        if totals:
            option += " 合计"
        basis = label.split("：", 1)[1]
        cards.append(f'<article class="quote-card"><span class="quote-option">{option}</span>'
                     f'<h3>{basis}</h3><strong class="quote-value">{price}</strong></article>')
    st.html('<div class="quote-grid">' + "".join(cards) + "</div>")


def render_pricing_basis(quote: dict, costs: Sequence[float], tolerance: float, inquiry: dict) -> None:
    utilization = quote["材料利用率_%"]
    st.markdown("**计价依据**")
    st.html(f"""
      <dl class="parameter-facts basis-values">
        <div><dt>税前钢价</dt><dd class="mono">¥{quote["当日税前基准钢价_元每kg"]:,.2f} / kg</dd></div>
        <div><dt>理论毛重</dt><dd class="mono">{quote["订单总毛重_理计_kg"]:g} kg</dd></div>
        <div><dt>预估过磅重</dt><dd class="mono">{quote["订单预估过磅重_含下差_kg"]:g} kg</dd></div>
        <div><dt>下差系数</dt><dd class="mono">{tolerance:.2f}</dd></div>
      </dl>
      <div class="utilization">材料利用率 <span class="mono">{utilization:g}%</span>
        <progress max="100" value="{utilization:g}" aria-label="材料利用率"></progress>
      </div>
    """)
    render_costs(costs, int(inquiry.get("quantity") or 1))


def render_scrap(quote: dict, scrap: dict, quantity: int) -> None:
    match = scrap.get("最优拼单推荐")
    if match:
        eligible = match["可拼单件数"]
        factory_id = match.get("工厂编号")
        factory_number = f' · 工厂编号 <span class="mono">{escape(str(factory_id))}</span>' if factory_id else ""
        st.html(
            f'<article class="benefit-card matched"><h3>{SCRAP_TITLE}</h3>'
            f'<dl class="stock-facts">'
            f'<div><dt>订单件数</dt><dd class="mono">{quantity} 件</dd></div>'
            f'<div><dt>预计可拼件数</dt><dd class="mono">{eligible} 件</dd></div>'
            f'<div class="stock-savings"><dt>这 {eligible} 件预计节省材料费</dt>'
            f'<dd class="mono">¥{match["直接节省金额_元"]:,.2f} '
            f'<small class="stock-rate">（{match["节省比例_%"]:g}%）</small></dd></div>'
            f'</dl>'
            f'<p>{escape(match["所在位置"])} · {escape(match["合作工厂"])}{factory_number}</p>'
            f'<p>{escape(match["余料规格"])} · 距戴南中心 <span class="mono">{match["直线距离_km"]:g} km</span></p>'
            f'<p>{SCRAP_CUT_NOTE}</p></article>'
        )
    else:
        pending_review = scrap.get("撮合状态") == SCRAP_REVIEW_STATUS
        title = SCRAP_REVIEW_STATUS if pending_review else SCRAP_TITLE
        state_class = "review" if pending_review else "empty"
        note = escape(scrap_narration(scrap, quantity))
        st.html(f'<article class="benefit-card {state_class}"><h3>{title}</h3><p>{note}</p></article>')

    recovery = quote["余料回流卡片"]
    if recovery:
        st.html(
            f'<article class="benefit-card recovery"><h3>{RECOVERY_TITLE}</h3>'
            f'<strong class="benefit-value">¥{recovery["预计回血金额_元"]:,.2f}</strong>'
            f'<p>{escape(recovery["产生余料规格"])} · <span class="mono">{recovery["可回收总重_kg"]:g} kg</span></p></article>'
        )


def scrap_review_text(snapshot: dict, quote_id: int, line_position: int) -> str:
    match = snapshot["scrap"]["最优拼单推荐"]
    fields = customer_quote_fields(snapshot)
    prefix = "演示复核 · " if snapshot["demo"] else ""
    lines = [
        f"{prefix}余料复核单 · {quote_number(quote_id)} · 第 {line_position} 项",
        f'{fields["material"]} {fields["item"]}｜{fields["spec"]}',
        f'本项订单数量 {fields["quantity"]} 件',
    ]
    for label in ("工厂编号", "库存批次编号"):
        if value := match.get(label):
            lines.append(f"{label} {value}")
    lines.extend((
        f'候选余料规格 {match["余料规格"]}',
        f'系统预计可拼 {match["可拼单件数"]} 件',
        "请车间确认余料是否仍在库、实测板厚（mm）、实际可用尺寸（长×宽，mm）及本单实际可切件数。谢谢。",
    ))
    return "\n".join(lines)


def render_scrap_review(snapshot: dict, quote_id: int | None, line_position: int = 1) -> None:
    match = snapshot["scrap"].get("最优拼单推荐")
    if quote_id is None or not match:
        return
    key = f"scrap-review-{quote_id}-{line_position}"
    quantity = snapshot["inquiry"]["quantity"]
    with st.container(key=key):
        demo = '<span class="scrap-review-demo">演示复核</span>' if snapshot["demo"] else ""
        st.html(f'<div class="scrap-review-header"><h3 class="scrap-review-heading">余料复核</h3>{demo}</div>')
        try:
            record = get_scrap_review(quote_id, line_position) or {}
        except (OSError, sqlite3.Error):
            st.error("复核记录读取失败，请稍后重试。")
            return

        metrics = st.container()
        render_copy_message(scrap_review_text(snapshot, quote_id, line_position), "复制余料复核话术",
                            f"scrap-review-copy-{quote_id}-{line_position}", show_text=False)
        status = st.radio("复核状态", SCRAP_REVIEW_STATUSES, horizontal=True,
                          index=SCRAP_REVIEW_STATUSES.index(record.get("status", "待复核")), key=f"{key}-status")
        with st.form(f"{key}-{status}", border=False, enter_to_submit=False):
            confirmed = None
            if status == "确认可供":
                previous = record.get("confirmed_quantity") if record.get("status") == status else None
                confirmed = st.number_input("本单确认可用件数（件）", min_value=1, max_value=quantity,
                                             step=1, value=previous, placeholder="填写车间确认件数")
            measured = st.text_input("实测尺寸（选填）", value=record.get("measured_dimensions", ""),
                                     max_chars=120, placeholder="实测板厚及可用长×宽（mm）")
            note = st.text_input("复核备注（选填）", value=record.get("note", ""), max_chars=500)
            submitted = st.form_submit_button("登记复核结果", width="stretch")
        if submitted:
            try:
                save_scrap_review(quote_id, line_position, status, confirmed, measured, note)
                record = get_scrap_review(quote_id, line_position) or {}
            except ValueError as error:
                st.error(str(error))
            except (OSError, sqlite3.Error):
                st.error("复核记录登记失败，请稍后重试。")
            else:
                st.success("复核结果已登记。")

        available = record.get("confirmed_quantity")
        facts = (
            ("系统预计可拼", f'{match["可拼单件数"]} 件'),
            ("人工确认可用", "待确认" if available is None else f"{available} 件"),
            ("待备料件数", "待确认" if available is None else f"{quantity - available} 件"),
        )
        with metrics:
            saved_status = record.get("status", "待复核")
            reviewed_at = f' · {record["reviewed_at"]}（北京时间）' if record else ""
            st.caption(f"已登记状态 · {saved_status}{reviewed_at}" if record else "待复核")
            values = "".join(f'<div><dt>{label}</dt><dd class="mono">{value}</dd></div>' for label, value in facts)
            st.html(f'<dl class="scrap-review-metrics">{values}</dl>')


def customer_info_rows(customer: dict | None = None) -> list[tuple[str, str, str]]:
    return [(CUSTOMER_INFO_FIELDS[field][0], value, "")
            for field, value in clean_customer_info(customer).items() if value]


def customer_validity_lines(fields: dict) -> list[str]:
    status = fields.get("validity_status")
    if status == "已过期":
        return ["已过期，仅供历史参考"]
    return ["有效期待核对"] if status == "有效期待核对" else []


def customer_validity_markup(fields: dict) -> str:
    return "".join(f'<p class="report-validity">{escape(notice)}</p>' for notice in customer_validity_lines(fields))


def validity_badge(valid_until: object) -> str:
    status = quote_validity(valid_until)
    style = {"有效期内": "current", "已过期": "expired", "有效期待核对": "unknown"}[status]
    return f'<span class="validity-badge {style}">{status}</span>'


def render_validity(snapshot: dict, *, order: bool = False, warn_expired: bool = True) -> str:
    deadline = snapshot.get("valid_until")
    label = "整单有效期至" if order else "有效期至"
    st.html(f'<div class="quote-validity" role="status">{validity_badge(deadline)}'
            f'<span>{label} <span class="mono">{escape(quote_deadline_text(deadline))}</span>（北京时间）</span></div>')
    status = quote_validity(deadline)
    if status == "已过期" and warn_expired:
        st.warning("已过期，仅供历史参考。请复制并重新核价后再发给客户。")
    return status


def customer_info_changed(draft_key: str, prefix: str, field: str) -> None:
    draft = st.session_state.get(draft_key)
    revision = "order_customer_revision" if draft_key == "order_customer" else "customer_revision"
    # 清单清空或草稿切换后，忽略已被移除的旧输入框回调。
    if (draft is None or prefix != f'{draft_key}_{st.session_state.get(revision, 0)}'
            or f"{prefix}_{field}" not in st.session_state):
        return
    if field == "delivery_confirmed":
        draft[field] = st.session_state[f"{prefix}_{field}"]
    else:
        draft["fields"][field] = st.session_state[f"{prefix}_{field}"]
        if field == "delivery_note":
            draft["delivery_confirmed"] = False
            st.session_state[f"{prefix}_delivery_confirmed"] = False
    st.session_state.pop("formal_report", None)
    work_draft_changed(inquiry=draft_key == "inquiry_customer", order=draft_key == "order_customer")


def render_customer_editor(draft_key: str, prefix: str) -> tuple[dict, bool]:
    draft = st.session_state.setdefault(draft_key, {"fields": clean_customer_info(), "delivery_confirmed": False})
    with st.container(key=f'{draft_key.replace("_", "-")}-fields'):
        st.markdown("**客户信息（选填）**")
        for field, (label, limit) in CUSTOMER_INFO_FIELDS.items():
            st.text_input(label, value=draft["fields"][field], max_chars=limit, key=f"{prefix}_{field}",
                          on_change=customer_info_changed, args=(draft_key, prefix, field))
        try:
            customer = clean_customer_info(draft["fields"])
        except ValueError as error:
            st.error(str(error))
            return {}, False
        if customer["delivery_note"]:
            st.checkbox("已核对交期备注", value=draft["delivery_confirmed"], key=f"{prefix}_delivery_confirmed",
                        on_change=customer_info_changed, args=(draft_key, prefix, "delivery_confirmed"))
            if not draft["delivery_confirmed"]:
                st.caption("请核对交期备注后再保存或发给客户。")
        return customer, not customer["delivery_note"] or draft["delivery_confirmed"]


def render_customer_information(snapshot: dict) -> None:
    rows = customer_info_rows(snapshot.get("customer"))
    if rows:
        facts = "".join(f'<div><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>'
                        for label, value, _ in rows)
        st.html(f'<dl class="parameter-facts customer-information">{facts}</dl>')


def render_customer_delivery(snapshot: dict, quote_id: int | None) -> None:
    grouped = snapshot.get("kind") == "order"
    if grouped:
        fields = customer_order_fields(snapshot, quote_id)
        markup = customer_order_markup(fields)
        message = customer_order_wechat_text(fields)
    else:
        fields = customer_quote_fields(snapshot, quote_id)
        markup = customer_quotation_markup(fields)
        message = customer_wechat_text(fields)
        if fields["cutting_note"]:
            st.caption(fields["cutting_note"])
    title = fields["title"]
    filename = f'戴南钢算AI-{title.replace(" · ", "-")}'
    if fields["number"]:
        filename += f'-{fields["number"]}'
    prefix = "order-" if grouped else ""
    with st.container(key=f"{prefix}customer-delivery"):
        st.html('<h3 class="customer-delivery-title">发给客户</h3>')
        copy, download = st.columns(2, gap="small")
        with copy:
            render_copy_message(message, "复制微信报价", f"{prefix}customer-quote-copy", show_text=False)
        download.download_button("下载客户报价单", printable_quotation(markup, title), f"{filename}.html",
                                 "text/html", on_click=show_report, args=(markup,), width="stretch",
                                 icon=":material/download:", type="primary", key=f"{prefix}customer-quote-file")


def render_quote_exports(rows: list, demo: bool, *, quote_id: int | None = None,
                         source_quote_id: int | None = None) -> None:
    quote_title = DEMO_QUOTE_TITLE if demo else "正式报价单"
    filename = f"戴南钢算AI-{quote_title}"
    identifiers = []
    if quote_id is not None:
        number = quote_number(quote_id)
        identifiers.append(("报价编号", number, ""))
        filename += f"-{number}"
    if source_quote_id is not None:
        identifiers.append(("来源报价编号", quote_number(source_quote_id), ""))
    rows = identifiers + rows
    markup = quotation_markup(rows, quote_title)
    standalone = printable_quotation(markup, quote_title)
    st.markdown(f"**导出{quote_title}**")
    html_export, csv_export = st.columns(2, gap="small")
    html_export.download_button("下载 HTML（可打印）", standalone, f"{filename}.html",
                                "text/html", on_click=show_report, args=(markup,), width="stretch",
                                icon=":material/download:", type="primary", key="print-quote")
    csv_export.download_button("下载 CSV", quotation_csv(rows), f"{filename}.csv",
                               "text/csv", on_click=show_report, args=(markup,), width="stretch", icon=":material/download:")
    st.caption("导出后可在页面下方预览，按 Ctrl+P 打印或另存为 PDF。")


def save_inquiry(save_key: str, snapshot: dict, source_quote_id: int | None,
                 feedback_key: str = "quote_save", drawings: dict[int, dict] | None = None) -> None:
    if quote_validity(snapshot.get("valid_until")) != "有效期内":
        st.session_state[feedback_key] = {"save_key": save_key, "error": "报价有效期已过或未填写，请重新核对截止时间。"}
        return
    try:
        quote_id = save_quote(save_key, snapshot, source_quote_id, drawings=drawings)
    except (OSError, sqlite3.Error, ValueError):
        st.session_state[feedback_key] = {"save_key": save_key}
    else:
        st.session_state[feedback_key] = {"save_key": save_key, "quote_id": quote_id}
        st.session_state.pop("formal_report", None)
        if quote_id is not None:
            completed_key = "order_draft_completed" if feedback_key == "order_save" else "inquiry_draft_completed"
            st.session_state[completed_key] = True
            work_draft_changed()


def order_changed() -> None:
    work_draft_changed(order=True)
    if saved_id := st.session_state.get("order_save", {}).get("quote_id"):
        st.session_state.order_source_quote_id = saved_id
    st.session_state.order_save_key = f"order:{uuid4().hex}"
    st.session_state.order_issued = datetime.now(DAINAN_TIME).strftime("%Y-%m-%d %H:%M:%S")
    st.session_state.pop("order_save", None)
    st.session_state.pop("formal_report", None)


def add_order_item(run_id: str, snapshot: dict, drawing: dict | None = None) -> None:
    if quote_validity(snapshot.get("valid_until")) != "有效期内":
        return
    items = st.session_state.setdefault("order_items", {})
    if run_id not in items:
        items[run_id] = deepcopy(snapshot)
        if drawing:
            st.session_state.setdefault("order_drawings", {})[run_id] = drawing
        order_changed()
        st.session_state.inquiry_draft_completed = True


def remove_order_item(run_id: str) -> None:
    if st.session_state.get("order_items", {}).pop(run_id, None) is not None:
        st.session_state.get("order_drawings", {}).pop(run_id, None)
        order_changed()
        if not st.session_state.order_items:
            st.session_state.pop("order_source_quote_id", None)
            st.session_state.pop("order_customer", None)
            st.session_state.order_customer_revision = st.session_state.get("order_customer_revision", 0) + 1


def edit_order_item(line_id: str) -> None:
    item = st.session_state.get("order_items", {}).get(line_id)
    if item is None:
        st.session_state.order_edit_feedback = "该明细已不存在，请检查当前本单清单。"
        return
    drawing = st.session_state.get("order_drawings", {}).get(line_id)
    start_new_inquiry()
    st.session_state.order_edit = {"line_id": line_id, "token": uuid4().hex, "filename": item["filename"]}
    st.session_state.manual_entry = True
    st.session_state.inquiry_drawing = drawing
    st.session_state.target_margin = item["gross_margin_percent"]
    reset_inquiry_validity(item.get("valid_until"), custom=True)
    begin_review(fields=deepcopy(item["inquiry"]), source="本单单项编辑", filename=item["filename"], demo=item["demo"])


def cancel_order_edit() -> None:
    start_new_inquiry()
    st.session_state.order_edit_feedback = "已取消编辑，本单保留上次确认的结果。"


def render_order_edit_status() -> None:
    editing = st.session_state.get("order_edit")
    if not editing:
        return
    line_ids = list(st.session_state.get("order_items", {}))
    line_id = editing["line_id"]
    if line_id in line_ids:
        title = f'正在编辑第 {line_ids.index(line_id) + 1} 项 · {editing["filename"]}'
        st.html(f'<div class="order-edit-banner" role="status"><strong>{escape(title)}</strong>'
                '<span>待更新 · 确认参数并核价后，点击「更新本项」。</span></div>')
    else:
        st.warning("编辑目标已不存在：该明细已从本单移除。请取消编辑后继续。")
    st.button("取消编辑", key="cancel-order-edit", on_click=cancel_order_edit, width="stretch")


def update_order_item(line_id: str, token: str, run_id: str, snapshot: dict, drawing: dict | None) -> bool:
    editing = st.session_state.get("order_edit", {})
    items = st.session_state.get("order_items", {})
    run = st.session_state.get("drawing_run", {})
    if (editing.get("line_id") != line_id or editing.get("token") != token or line_id not in items
            or run.get("run_id") != run_id or run.get("order_edit_token") != token
            or run.get("result", {}).get("status") != "SUCCESS"
            or st.session_state.get("drawing_review", {}).get("confirmed") != snapshot["inquiry"]
            or st.session_state.get("target_margin") != snapshot["gross_margin_percent"]
            or quote_validity(snapshot.get("valid_until")) != "有效期内"
            or ("valid_until" in run and run["valid_until"] != snapshot.get("valid_until"))):
        return False
    # 使用稳定 ID 原位替换；编辑会话结束后，重复或迟到的更新请求自动失效。
    items[line_id] = deepcopy(snapshot)
    drawings = st.session_state.setdefault("order_drawings", {})
    if drawing is None:
        drawings.pop(line_id, None)
    else:
        drawings[line_id] = drawing
    order_changed()
    start_new_inquiry()
    st.session_state.order_edit_feedback = "已更新本项，请核对整单合计后保存本单。"
    return True


def render_order_details(snapshot: dict, quote_id: int | None, line_ids: Sequence[str] = (),
                         customer_ready: bool = True) -> None:
    count = sum(item["inquiry"]["quantity"] for item in snapshot["items"])
    demo = f" · {DEMO_QUOTE_TITLE}" if snapshot["demo"] else ""
    st.caption(f'{len(snapshot["items"])} 项 · 共 {count} 件{demo}')
    if not line_ids:
        render_customer_information(snapshot)
        with st.expander("历史原图", expanded=True):
            drawing_position = st.selectbox("选择图纸明细", range(1, len(snapshot["items"]) + 1),
                                            key=f"history-drawing-{quote_id}",
                                            format_func=lambda number: f'第 {number} 项 · {snapshot["items"][number - 1]["filename"]}')
            render_retained_drawing(quote_id, drawing_position)
    for position, item in enumerate(snapshot["items"], 1):
        fields = customer_quote_fields(item)
        pending = bool(line_ids and st.session_state.get("order_edit", {}).get("line_id") == line_ids[position - 1])
        pending_badge = '<span class="order-pending">待更新</span>' if pending else ""
        line_markup = (
            f'<article class="order-line{" pending" if pending else ""}" aria-label="第 {position} 项">'
            f'<div class="order-line-spec"><strong>{position}. {escape(fields["material"])} {escape(fields["item"])}</strong>'
            f'<small>{escape(fields["spec"])}</small>{pending_badge}</div>'
            f'<div><small>数量</small><strong class="mono">{fields["quantity"]} 件</strong></div>'
            f'<div><small>方案 A</small><strong class="mono">¥{fields["plans"][0][1]}</strong></div>'
            f'<div><small>方案 B</small><strong class="mono">¥{fields["plans"][1][1]}</strong></div></article>'
        )
        if line_ids:
            line_id = line_ids[position - 1]
            detail, action = st.columns((8, 1.35), gap="small", vertical_alignment="center")
            detail.html(line_markup)
            action.button("编辑本项", key=f"edit-order-{line_id}", width="stretch",
                          on_click=edit_order_item, args=(line_id,))
            action.button(f"移除第 {position} 项", key=f"remove-order-{line_id}", width="stretch",
                          on_click=remove_order_item, args=(line_id,))
        else:
            st.html(line_markup)
    render_quote_cards({key: Decimal(amount) for key, amount in snapshot["totals"].items()}, totals=True)
    render_validity(snapshot, order=True)
    st.caption("整单有效期取各项中最早的到期时间。")
    if customer_ready:
        render_customer_delivery(snapshot, quote_id)
    with st.expander("单件内部核价明细"):
        position = st.selectbox("查看单件", range(len(snapshot["items"])),
                                format_func=lambda index: f"第 {index + 1} 项 · {snapshot['items'][index]['filename']}")
        item = snapshot["items"][position or 0]
        inquiry = item["inquiry"]
        st.caption(f'售价毛利率 · {item["gross_margin_percent"]}% · 单件有效期至 {quote_deadline_text(item.get("valid_until"))}')
        render_pricing_basis(item["quote"], item["costs"], item["tolerance"], inquiry)
        render_scrap(item["quote"], item["scrap"], inquiry["quantity"])
        render_scrap_review(item, quote_id, (position or 0) + 1)


def render_order_panel() -> None:
    with st.container(key="order-panel"):
        st.html('<h2 id="current-order" class="panel-heading">本单清单</h2>')
        if feedback := st.session_state.pop("order_edit_feedback", None):
            st.info(feedback)
        items = st.session_state.get("order_items", {})
        if not items:
            st.caption("完成任一零件报价后，点击「加入本单」。")
            return
        snapshot = build_order_snapshot(list(items.values()), st.session_state.order_issued)
        customer, customer_ready = render_customer_editor(
            "order_customer", f'order_customer_{st.session_state.get("order_customer_revision", 0)}')
        snapshot["customer"] = customer
        customer_hash = hashlib.sha256(json.dumps(customer, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        save_key = f'{st.session_state.order_save_key}:{customer_hash}'
        feedback = st.session_state.get("order_save", {})
        if feedback.get("save_key") != save_key:
            feedback = {}
        quote_id = feedback.get("quote_id")
        source_quote_id = st.session_state.get("order_source_quote_id")
        saved = st.session_state.get("order_save", {})
        if saved.get("quote_id") and saved.get("save_key") != save_key:
            source_quote_id = saved["quote_id"]
        if st.session_state.get("order_edit"):
            st.caption("编辑草稿尚未写入本单。整单交付使用清单中上次已确认的规格、数量和金额。")
        validity_ready = quote_validity(snapshot.get("valid_until")) == "有效期内"
        render_order_details(snapshot, quote_id, list(items), customer_ready and validity_ready)
        drawings = {position: st.session_state["order_drawings"][line_id]
                    for position, line_id in enumerate(items, 1)
                    if line_id in st.session_state.get("order_drawings", {})}
        st.button("保存本单", key=save_key, disabled="quote_id" in feedback or not customer_ready or not validity_ready,
                  on_click=save_inquiry, args=(save_key, snapshot, source_quote_id, "order_save", drawings))
        if source_quote_id is not None:
            st.caption(f"来源整单 · {quote_number(source_quote_id)}。本次保存生成新版报价，原记录保留。")
        if quote_id is not None:
            st.html(f'<p class="record-meta">已保存 <a class="record-link" href="?view=records&amp;quote={quote_id}" '
                    f'target="_self">{quote_number(quote_id)}</a></p>')
        elif "quote_id" in feedback:
            st.info("该报价记录已删除。调整清单后可重新保存。")
        elif feedback:
            st.error(feedback.get("error", "本单保存失败，请检查本机数据库是否可写后重试。"))


def render_quote(run: dict, reviewing: bool = False, *, customer_panel, validity_panel) -> dict:
    result = run.get("result", {})
    editing = st.session_state.get("order_edit")
    price_cards = st.container()
    internal = st.expander("内部核价明细", expanded=bool(run or reviewing),
                           key=f'quote-internals-{st.session_state.get("customer_revision", 0)}', on_change="rerun")
    delivery = st.container()
    if "target_margin" not in st.session_state:
        st.session_state.target_margin = DEFAULT_TARGET_MARGIN
    with internal:
        target = st.slider("售价毛利率", 5, 30, format="%d%%", key="target_margin",
                           on_change=margin_changed)
    ready = result.get("status") == "SUCCESS" and (not editing or run.get("order_edit_token") == editing["token"])
    quote, costs = reprice(run["inquiry"], target) if ready else ({}, ())
    with price_cards:
        render_quote_cards(quote)
    if not ready:
        delivery.caption("人工确认参数后生成报价。" if reviewing else "录入完整询盘后生成报价。")
        if not editing and st.session_state.get("drawing_review", {}).get("source_quote_id"):
            with customer_panel:
                render_customer_editor("inquiry_customer", f'inquiry_customer_{st.session_state.get("customer_revision", 0)}')
        return quote

    inquiry = run["inquiry"]
    tolerance = load_material_rule(quote["材质"])["tolerance"]
    scrap = result["scrap_result"]

    if editing:
        item = st.session_state.get("order_items", {}).get(editing["line_id"], {})
        customer, customer_ready = clean_customer_info(item.get("customer")), True
        with customer_panel:
            st.markdown("**客户信息**")
            st.caption("整单客户信息可在下方本单清单中填写。")
    else:
        with customer_panel:
            customer, customer_ready = render_customer_editor(
                "inquiry_customer", f'inquiry_customer_{st.session_state.get("customer_revision", 0)}')
    with validity_panel:
        validity_ready = render_quote_deadline(run)
    rows = quote_rows(run, quote, scrap, tolerance, target)
    if customer_ready:
        rows.extend(customer_info_rows(customer))
    snapshot = {
        "filename": run["filename"], "inquiry": inquiry, "demo": run["demo"],
        "issued": run["issued"], "valid_until": run["valid_until"],
        "gross_margin_percent": target, "quote": quote, "costs": costs, "tolerance": tolerance,
        "scrap": scrap, "rows": rows, "customer": customer,
    }
    if run.get("source_line_position") is not None:
        snapshot["source_line_position"] = run["source_line_position"]
    # 同一次核价中，按已显示的快照归并重复保存。
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
    save_key = f'{run["run_id"]}:{hashlib.sha256(payload.encode("utf-8")).hexdigest()}'
    feedback = st.session_state.get("quote_save", {})
    if feedback.get("save_key") != save_key:
        feedback = {}
    quote_id = feedback.get("quote_id")
    source_quote_id = run["source_quote_id"]
    with delivery:
        if editing:
            st.caption("本项重算结果待更新到本单。整单报价仍按清单中已确认的结果生成。")
        elif customer_ready and validity_ready:
            render_customer_delivery(snapshot, quote_id)
    with internal:
        render_quote_changes(source_quote_id, snapshot)
        render_pricing_basis(quote, costs, tolerance, inquiry)
        render_scrap(quote, scrap, inquiry["quantity"])
        if not editing:
            render_scrap_review(snapshot, quote_id)
            if validity_ready:
                render_quote_exports(rows, run["demo"], quote_id=quote_id, source_quote_id=source_quote_id)
    if editing:
        st.button("更新本项", key=f'update-order-{editing["token"]}-{save_key}', type="primary", width="stretch",
                  disabled=editing["line_id"] not in st.session_state.get("order_items", {}) or not validity_ready,
                  on_click=update_order_item,
                  args=(editing["line_id"], editing["token"], run["run_id"], snapshot, run.get("drawing")))
        return quote
    save, add = st.columns(2, gap="small")
    save.button("保存询盘", key=f"save-inquiry-{save_key}", width="stretch",
                disabled="quote_id" in feedback or not customer_ready or not validity_ready,
                on_click=save_inquiry, args=(save_key, snapshot, source_quote_id, "quote_save",
                                           {1: run["drawing"]} if run.get("drawing") else None))
    in_order = run["run_id"] in st.session_state.get("order_items", {})
    add.button("加入本单", key=f"add-order-{save_key}", width="stretch",
               disabled=in_order or not customer_ready or not validity_ready,
               on_click=add_order_item, args=(run["run_id"], snapshot, run.get("drawing")))
    if in_order:
        st.html('<p class="record-meta">已加入 <a class="record-link" href="#current-order" target="_self">本单清单</a></p>')
    if quote_id is not None:
        st.html(f'<p class="record-meta">已保存 <a class="record-link" href="?view=records&amp;quote={quote_id}" target="_self">'
                f'{quote_number(quote_id)}</a></p>')
    elif "quote_id" in feedback:
        st.info("这条报价已删除。重新核价后可保存新记录。")
    elif feedback:
        st.error(feedback.get("error", "询盘保存失败，请检查本机数据库是否可写后重试。"))
    return quote


def copy_quote(record: dict, line_position: int | None = None) -> None:
    snapshot = record["snapshot"]
    drawing = get_quote_drawing(record["id"], line_position or 1)
    customer = snapshot.get("customer")
    if snapshot.get("kind") == "order":
        snapshot = snapshot["items"][line_position - 1]
    start_new_inquiry()
    st.session_state.inquiry_drawing = drawing
    reset_inquiry_customer(customer)
    st.session_state.manual_entry = True
    st.session_state.target_margin = snapshot["gross_margin_percent"]
    begin_review(fields=snapshot["inquiry"], source="复制改价", filename=snapshot["filename"],
                 demo=snapshot["demo"], source_quote_id=record["id"], source_line_position=line_position)
    st.query_params.from_dict({"view": "workbench"})


def render_saved_quote(record: dict, source_available: bool) -> None:
    snapshot = record["snapshot"]
    if snapshot.get("kind") == "order":
        with st.container(key="order-panel"):
            st.html(f'<h2 class="panel-heading">{quote_number(record["id"])} · {ORDER_QUOTE_LABEL}</h2>')
            st.caption(f'报价时间 · {snapshot["issued"]}（北京时间）')
            if quote_validity(snapshot.get("valid_until")) != "有效期内":
                line_position = st.selectbox("选择重新核价的明细", range(1, len(snapshot["items"]) + 1),
                                             format_func=lambda number: f'第 {number} 项 · {snapshot["items"][number - 1]["filename"]}')
                st.button("复制并重新核价", type="primary", on_click=copy_quote, args=(record, line_position))
                st.caption("多件报价请逐项核对后加入本单，重新保存整单。")
            render_order_details(snapshot, record["id"])
        return
    inquiry, quote = snapshot["inquiry"], snapshot["quote"]
    quote_id, source_quote_id = record["id"], record["source_quote_id"]
    number = quote_number(quote_id)
    kind = DEMO_QUOTE_TITLE if snapshot["demo"] else "正式报价单"
    st.html(f'<h2 class="panel-heading">{number} · {kind}</h2>'
            f'<p class="record-meta">图纸或询盘 · {escape(snapshot["filename"])}</p>')
    if source_quote_id is not None:
        source_number = quote_number(source_quote_id)
        if source_available:
            st.html(f'<p class="record-meta">来源报价 · <a class="record-link" target="_self" '
                    f'href="?view=records&amp;quote={source_quote_id}">{source_number}</a></p>')
        else:
            st.caption(f"来源报价 · {source_number}（来源数据不可用）")
    status = render_validity(snapshot)
    copy_label = "复制并重新核价" if status == "已过期" else "复制改价"
    st.button(copy_label, type="primary", on_click=copy_quote, args=(record,))
    parameters, delivery = st.columns((1, 1.25), gap="medium")
    with parameters, st.container(key="record-parameters"):
        st.html('<h2 class="panel-heading">已确认规格</h2>')
        render_customer_information(snapshot)
        render_sample_parameters(snapshot)
        with st.expander("历史原图", expanded=True):
            render_retained_drawing(quote_id)
        st.html(f'<dl class="parameter-facts">'
                f'<div><dt>报价时间（北京时间）</dt><dd class="mono">{escape(snapshot["issued"])}</dd></div>'
                f'<div><dt>有效期至</dt><dd class="mono">{escape(quote_deadline_text(snapshot.get("valid_until")))}</dd></div>'
                '</dl>')
    with delivery, st.container(key="delivery-panel"):
        st.html('<h2 class="panel-heading">报价与余料</h2>')
        render_quote_cards(quote)
        render_customer_delivery(snapshot, quote_id)
        with st.expander("内部核价明细"):
            render_quote_changes(source_quote_id, snapshot)
            st.caption(f'售价毛利率 · {snapshot["gross_margin_percent"]}%')
            render_pricing_basis(quote, snapshot["costs"], snapshot["tolerance"], inquiry)
            render_scrap(quote, snapshot["scrap"], inquiry["quantity"])
            render_scrap_review(snapshot, quote_id)
            render_quote_exports(snapshot["rows"], snapshot["demo"], quote_id=quote_id, source_quote_id=source_quote_id)


def render_quote_list(records: list[dict], search: str) -> None:
    if not records:
        st.info("未找到匹配的询盘。" if search.strip() else "暂无询盘记录。出价后点击「保存询盘」即可在这里查找。")
        return
    rows = []
    for record in records:
        quote_id, snapshot = record["id"], record["snapshot"]
        number = quote_number(quote_id)
        if snapshot.get("kind") == "order":
            items = snapshot["items"]
            name = f'<strong>{ORDER_QUOTE_LABEL}</strong><small>{len(items)} 项 · {escape(snapshot["filename"])}</small>'
            material = " / ".join(dict.fromkeys(item["quote"]["材质"] for item in items))
            quantity = sum(item["inquiry"]["quantity"] for item in items)
            quote = {key: Decimal(amount) for key, amount in snapshot["totals"].items()}
        else:
            name = escape(snapshot["filename"])
            material = snapshot["quote"]["材质"]
            quantity = snapshot["inquiry"]["quantity"]
            quote = snapshot["quote"]
        prices = "".join(f'<td class="mono">¥{quote[key]:,.2f}</td>' for _, key in QUOTE_OPTIONS)
        demo_label = f"<small>{DEMO_QUOTE_TITLE}</small>" if snapshot["demo"] else ""
        customer = clean_customer_info(snapshot.get("customer"))
        customer_name = escape(customer["short_name"]) or "—"
        project_name = escape(customer["project_name"]) or "—"
        validity = validity_badge(snapshot.get("valid_until"))
        deadline = escape(quote_deadline_text(snapshot.get("valid_until")))
        delete_query = {"view": "records", "remove": quote_id}
        if search:
            delete_query["search"] = search
        delete_url = escape("?" + urlencode(delete_query))
        rows.append(
            f'<tr><th scope="row"><a class="record-link mono" target="_self" href="?view=records&amp;quote={quote_id}">'
            f'{number}</a></th><td class="record-name">{name}{demo_label}</td>'
            f'<td class="record-name">{customer_name}<small>{project_name}</small></td>'
            f'<td>{escape(material)}</td><td class="mono">{quantity}</td>'
            f'{prices}<td class="mono">{escape(snapshot["issued"])}</td>'
            f'<td>{validity}<small class="mono">{deadline}</small><small>北京时间</small></td>'
            f'<td><a class="record-delete" href="{delete_url}" target="_self" aria-label="删除 {number}">删除</a></td></tr>'
        )
    st.html(f"""
        <div class="record-table" role="region" aria-label="询盘记录列表" tabindex="0">
          <table>
            <thead><tr><th scope="col">报价编号</th><th scope="col">图纸或询盘</th><th scope="col">客户 / 项目</th><th scope="col">材质</th>
              <th scope="col">数量（件）</th><th scope="col">方案 A</th><th scope="col">方案 B</th>
              <th scope="col">报价时间（北京时间）</th><th scope="col">有效期</th><th scope="col">操作</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>
    """)


def requested_quote_id(value: str | None) -> int | None:
    if value is None or not (1 <= len(value) <= 19 and value.isascii() and value.isdecimal()):
        return None
    quote_id = int(value)
    return quote_id if 0 < quote_id < 2**63 else None


def render_delete_confirmation(record: dict | None) -> None:
    with st.container(key="quote-delete-confirm"):
        if record is None:
            st.info("这条询盘已删除或不存在。")
        else:
            number = quote_number(record["id"])
            filename = escape(record["snapshot"]["filename"])
            st.html(f'<p class="record-meta">确认删除 <strong>{number}</strong> · {filename}？</p>')
        with st.container(horizontal=True):
            confirmed = record is not None and st.button("确认删除", key=f'delete-quote-{record["id"]}')
            cancelled = st.button("取消", key="cancel-quote-delete")
    if confirmed:
        try:
            deleted = delete_quote(record["id"])
        except (OSError, sqlite3.Error):
            st.error("删除失败，请检查本机数据库是否可写后重试。")
            return
        st.session_state.quote_delete_feedback = (number, deleted)
        st.session_state.pop("formal_report", None)
    if confirmed or cancelled:
        del st.query_params["remove"]
        st.rerun()


def render_quote_history() -> None:
    st.html("""
      <header class="steel-nav">
        <a class="wordmark" href="?" target="_self">戴南钢算AI</a>
        <nav aria-label="询盘记录导航">
          <a href="?view=workbench" target="_self">报价工作台</a>
          <a href="?view=records" target="_self">询盘记录</a>
        </nav>
      </header>
      <h1 class="workbench-title">询盘记录</h1>
    """)
    selected = st.query_params.get("quote")
    try:
        if selected is None:
            search = st.text_input("查找询盘", value=st.query_params.get("search", "")[:255],
                                   placeholder="输入编号、图纸名、客户简称或项目名称", max_chars=255)
            removal = st.query_params.get("remove")
            removal_id = requested_quote_id(removal)
            pending = get_quote(removal_id) if removal_id is not None else None
            records = list_quotes(search)
        else:
            quote_id = requested_quote_id(selected)
            record = get_quote(quote_id) if quote_id is not None else None
            source_id = record["source_quote_id"] if record else None
            source_available = source_id is not None and source_quote_snapshot(
                source_id, record["snapshot"].get("source_line_position")) is not None
    except (OSError, sqlite3.Error, ValueError):
        st.error("询盘记录无法读取，请检查本机数据库。")
        return
    if selected is None:
        if feedback := st.session_state.pop("quote_delete_feedback", None):
            number, deleted = feedback
            if deleted:
                st.success(f"已删除 {number}。")
            else:
                st.info(f"{number} 已删除。")
        if removal is not None:
            render_delete_confirmation(pending)
        render_quote_list(records, search)
    elif record is None:
        st.warning("找不到这条报价，请返回询盘记录查找。")
    else:
        render_saved_quote(record, source_available)


def render_review_record(filename: str, review: dict, quote: dict) -> None:
    if "confirmed" in review and quote and "end_to_end_seconds" not in review:
        review["end_to_end_seconds"] = perf_counter() - review["started"]
    issues = review.get("issues", [])
    record = {
        "drawing": filename,
        "source": review["source"],
        "model": review.get("model"),
        "raw_fields": {field: review["fields"].get(field) for field in DRAWING_FIELDS},
        "confirmed_fields": review.get("confirmed"),
        "status": "SUCCESS" if quote else "NEED_INFO" if issues else "REVIEW",
        "initial_issues": review["initial_issues"],
        "remaining_issues": issues,
        "error": review.get("error"),
        "vision_seconds": review.get("vision_seconds"),
        "end_to_end_seconds": review.get("end_to_end_seconds"),
        "usage": review.get("usage"),
    }
    if quote:
        record["gross_margin_percent"] = st.session_state.target_margin
        record["quote"] = {key: quote[key] for _, key in QUOTE_OPTIONS}
    st.caption("识图核验记录")
    st.json(record, expanded=False)
    st.download_button("下载核验记录", json.dumps(record, ensure_ascii=False, indent=2),
                       "识图核验记录.json", "application/json", on_click="ignore")


@st.dialog("余料库", width="large", on_dismiss="rerun")
def render_reference_data() -> None:
    with st.container(key="stock-dialog"):
        inventory_tab, validation_tab = st.tabs(("余料库存", "图纸核验表"))
        with inventory_tab:
            st.caption(SCRAP_CUT_NOTE)
            try:
                inventory = pd.read_csv(DATA_DIR / "Scrap_Inventory.csv")
                st.caption(f'{inventory["工厂编号"].nunique()} 家工厂 · {len(inventory)} 条库存')
                st.dataframe(inventory, hide_index=True, height=300)
            except (OSError, ValueError, KeyError):
                st.error("余料池无法读取，请检查 Scrap_Inventory.csv。")
        with validation_tab:
            try:
                workbook = pd.read_excel(DATA_DIR / "Test_Images_Ground_Truth.xlsx", sheet_name=None, dtype=str)
                sheet = st.selectbox("核验工作表", tuple(workbook), key="validation_sheet")
                if sheet:
                    st.caption("原始核验表采用 12% 成本加价。工作台采用滑块所选的售价毛利率。")
                    st.dataframe(workbook[sheet], hide_index=True, height=300)
            except (OSError, ValueError, ImportError, BadZipFile):
                st.error("核验表无法读取，请检查 Excel 文件及 openpyxl 环境。")
        if st.button("关闭余料库", key="close-stock-library"):
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="戴南钢算AI", layout="wide")
    st.html(UI_STYLES)
    view = st.query_params.get("view")
    report_context = (view, st.query_params.get("quote"))
    if st.session_state.get("report_context") != report_context:
        st.session_state.pop("formal_report", None)
        st.session_state.report_context = report_context
    if view == "records":
        with st.container(key="workbench"):
            render_quote_history()
        if report := st.session_state.get("formal_report"):
            st.html(REPORT_STYLE + report)
        return
    if view != "workbench":
        render_home()
        return
    with st.container(key="workbench"):
        with st.container(key="workbench-nav", horizontal=True, vertical_alignment="center", gap="medium"):
            st.html('<a class="wordmark" href="?" target="_self">戴南钢算AI</a>', width="stretch")
            st.html('<a class="nav-link" href="#steel-rates" target="_self">钢价基准</a>', width="content")
            stock_requested = st.button("余料库", key="open-stock-library", type="tertiary")
            st.html('<a class="nav-link" href="?view=records" target="_self">询盘记录</a>', width="content")
            st.html('<a class="nav-link" href="#current-order" target="_self">本单清单</a>', width="content")
        if stock_requested:
            render_reference_data()
        st.html('<h1 class="workbench-title">报价工作台</h1>')
        if not render_work_draft_gate():
            return
        draft_status = st.container()
        render_market()
        work_area, delivery = st.columns([2.04, 1.25], gap="medium")
        with work_area:
            inputs, workflow = st.columns([1.0, 1.04], gap="medium")
            with inputs, st.container(key="inquiry-panel"):
                inquiry_name = render_inputs()
            with workflow, st.container(key="workflow-panel"):
                st.html('<h2 class="panel-heading">参数核对</h2>')
                render_order_edit_status()
                if inquiry_name:
                    render_review(inquiry_name)
                elif sample_run := st.session_state.get("drawing_run"):
                    render_sample_parameters(sample_run)
                    if sample_run["result"]["status"] == "NEED_INFO":
                        render_followup(sample_run)
                else:
                    st.html('<p class="empty-hint">上传图纸或手动录入后，核对材质、尺寸和数量。</p>')
                activity = st.container()
            with st.container(key="inquiry-metadata"):
                customer_panel, validity_panel = st.columns(2, gap="medium", vertical_alignment="top")
        run = st.session_state.get("drawing_run", {})
        with delivery, st.container(key="delivery-panel"):
            st.html('<h2 class="panel-heading">报价与余料</h2>')
            quote = render_quote(run, reviewing=inquiry_name is not None,
                                 customer_panel=customer_panel, validity_panel=validity_panel)
        review = st.session_state.drawing_review if inquiry_name else None
        with activity:
            if run.get("followed_up") and quote:
                st.caption(f'已补填厚度 {run["inquiry"]["thickness_mm"]:.1f} mm，续算完成。')
                st.button("查看余料拼单", key="judge_scrap",
                          on_click=select_preset, args=("PRESET_3_SCRAP_MATCH",))
            if run or review is not None:
                with st.expander("处理详情", expanded=True,
                                 key=f'processing-details-{st.session_state.get("customer_revision", 0)}', on_change="rerun"):
                    metrics = st.container()
                    render_timeline(run, quote, reviewing=inquiry_name is not None)
                    if review is not None and review.get("source_quote_id") is None:
                        render_review_record(inquiry_name, review, quote)
                    with metrics:
                        render_run_metrics(run, review)
        render_order_panel()
        with draft_status:
            render_work_draft_status()
    if report := st.session_state.get("formal_report"):
        st.html(REPORT_STYLE + report)


if __name__ == "__main__":
    main()
