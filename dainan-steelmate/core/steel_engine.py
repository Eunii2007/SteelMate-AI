import math
import os
from typing import Dict, Any, List
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECTANGULAR_AREA_FIELD = "可用矩形已标注"
SCRAP_REVIEW_STATUS = "有候选，需人工复核"

DEFAULT_MATERIAL_RULES = {
    "304": {"density": 7.93, "base_price": 12.30, "tolerance": 0.94, "cut_rate_per_m": 7.50, "vat_rate": 1.13},
    "316L": {"density": 7.98, "base_price": 25.13, "tolerance": 0.95, "cut_rate_per_m": 9.38, "vat_rate": 1.13},
    "201": {"density": 7.93, "base_price": 7.61, "tolerance": 0.92, "cut_rate_per_m": 6.50, "vat_rate": 1.13},
    "2205": {"density": 7.80, "base_price": 27.43, "tolerance": 0.96, "cut_rate_per_m": 13.00, "vat_rate": 1.13},
}

DEFAULT_SCRAP_POOL = [
    {"工厂编号": "DN-001", "脱敏厂名": "兴化市鑫源精密不锈钢制品厂", "所在园区_镇街": "张郭镇工业集中区", "距中心距离_km": 1.8, "材质牌号": "316L", "余料形态": "板材边角料", "可用长_mm": 650, "可用宽_mm": 420, "厚度_mm": 12.0, "余料特惠单价_元每公斤": 18.8, RECTANGULAR_AREA_FIELD: "是"},
    {"工厂编号": "DN-002", "脱敏厂名": "戴南镇宏达激光切割加工部", "所在园区_镇街": "戴南镇科技园区", "距中心距离_km": 0.9, "材质牌号": "304", "余料形态": "盲板套切圆芯", "可用长_mm": 320, "可用宽_mm": 320, "厚度_mm": 10.0, "余料特惠单价_元每公斤": 9.6, RECTANGULAR_AREA_FIELD: "否"},
    {"工厂编号": "DN-003", "脱敏厂名": "兴化市永固法兰制造有限公司", "所在园区_镇街": "戴南镇董北工业区", "距中心距离_km": 2.4, "材质牌号": "304", "余料形态": "矩形余料板", "可用长_mm": 850, "可用宽_mm": 520, "厚度_mm": 16.0, "余料特惠单价_元每公斤": 9.8, RECTANGULAR_AREA_FIELD: "是"},
]


def _resolve_data_path(filename: str) -> str:
    p1 = os.path.join(BASE_DIR, "data", filename)
    if os.path.exists(p1):
        return p1
    p2 = os.path.join("data", filename)
    return p2


def load_material_rule(material: str) -> Dict[str, float]:
    """读取材质计价规则，优先使用 data/Dainan_Pricing_Rules.xlsx 首表"""
    mat_key = str(material).strip().upper()
    excel_path = _resolve_data_path("Dainan_Pricing_Rules.xlsx")
    if os.path.exists(excel_path):
        try:
            df = pd.read_excel(excel_path, sheet_name=0)
            matched = df[df["材质牌号"].astype(str).str.upper() == mat_key]
            if not matched.empty:
                r = matched.iloc[0]
                return {
                    "density": float(r["密度_g每cm3"]),
                    "base_price": float(r["当日基准单价_元每公斤"]),
                    "tolerance": float(r["常规下差系数"]),
                    "cut_rate_per_m": float(r["激光切割费_元每米"]),
                    "vat_rate": float(r["专票税点系数"]),
                }
        except Exception:
            pass
    return DEFAULT_MATERIAL_RULES.get(mat_key, DEFAULT_MATERIAL_RULES["304"])


def calculate_stainless_quote(
    material: str,
    shape: str,
    thickness_mm: float,
    quantity: int,
    outer_dim_mm: float,
    width_mm: float = 0.0,
    inner_dim_mm: float = 0.0,
    profit_margin: float = 0.12
) -> Dict[str, Any]:
    """戴南不锈钢非标件算料与双轨核价工具（税前单价与13%专票自动分离）。"""
    rule = load_material_rule(material)
    density, base_price_ex, tolerance = rule["density"], rule["base_price"], rule["tolerance"]
    cut_rate = rule["cut_rate_per_m"] * (1 + max(0, thickness_mm - 6) * 0.08)
    scrap_recovery_card = None

    if shape == "flange":
        blank_area = outer_dim_mm * outer_dim_mm
        gross_single = (blank_area * thickness_mm * density) / 1_000_000.0
        net_area = math.pi * ((outer_dim_mm / 2.0) ** 2 - (inner_dim_mm / 2.0) ** 2)
        net_single = (net_area * thickness_mm * density) / 1_000_000.0
        cut_m = (math.pi * (outer_dim_mm + inner_dim_mm)) / 1000.0
        pierce_fee = 2.0 if inner_dim_mm > 0 else 1.0

        if inner_dim_mm >= 80:
            core_d = round(inner_dim_mm - 4.0, 1)
            core_kg = (math.pi * ((core_d / 2.0) ** 2) * thickness_mm * density / 1_000_000.0) * quantity
            scrap_recovery_card = {
                "产生余料规格": f"{material.upper()} 实心圆芯 Ø{core_d}mm × 厚{thickness_mm}mm × {quantity}块",
                "可回收总重_kg": round(core_kg, 2),
                "预计回血金额_元": round(core_kg * base_price_ex * 0.78, 2),
            }
    elif shape == "plate":
        w = width_mm if width_mm > 0 else outer_dim_mm
        gross_single = (outer_dim_mm * w * thickness_mm * density) / 1_000_000.0
        net_single = gross_single
        cut_m = 2.0 * (outer_dim_mm + w) / 1000.0
        pierce_fee = 1.0
    else:
        area = math.pi * ((outer_dim_mm / 2.0) ** 2)
        gross_single = (area * thickness_mm * density) / 1_000_000.0
        net_single = gross_single
        cut_m = 0.0
        pierce_fee = round(outer_dim_mm * 0.05, 2)

    total_gross_kg = gross_single * quantity
    total_actual_kg = total_gross_kg * tolerance
    total_cut_cost = (cut_m * cut_rate + pierce_fee) * quantity

    quote_ex_tax = (total_gross_kg * base_price_ex + total_cut_cost) * (1 + profit_margin)
    quote_inc_vat = (total_actual_kg * base_price_ex + total_cut_cost) * (1 + profit_margin) * rule["vat_rate"]

    return {
        "材质": material.upper(),
        "单件理论毛重_kg": round(gross_single, 3),
        "订单总毛重_理计_kg": round(total_gross_kg, 2),
        "订单预估过磅重_含下差_kg": round(total_actual_kg, 2),
        "材料利用率_%": round((net_single / gross_single) * 100, 1),
        "当日税前基准钢价_元每kg": base_price_ex,
        "当日含税现货钢价_元每kg": round(base_price_ex * rule["vat_rate"], 2),
        "激光加工费_元": round(total_cut_cost, 2),
        "方案A_理计不含税自提价_元": round(quote_ex_tax, 2),
        "方案B_过磅含13专票价_元": round(quote_inc_vat, 2),
        "单件含税均价_元": round(quote_inc_vat / quantity, 2),
        "余料回流卡片": scrap_recovery_card,
    }


def match_local_scrap(
    material: str,
    thickness_mm: float,
    req_length_mm: float,
    req_width_mm: float,
    quantity: int
) -> Dict[str, Any]:
    """检索戴南周边3公里余料共享池，返回最优跨厂拼单方案。"""
    csv_path = _resolve_data_path("Scrap_Inventory.csv")
    df = pd.read_csv(csv_path) if os.path.exists(csv_path) else pd.DataFrame(DEFAULT_SCRAP_POOL)

    rule = load_material_rule(material)
    new_price_ex = rule["base_price"]
    mat_key = str(material).strip().upper()

    factory_fields = ["工厂编号", "脱敏厂名", "所在园区_镇街"]
    stock = df.reindex(columns=[
        *factory_fields, "材质牌号", "距中心距离_km", "厚度_mm", "余料形态",
        RECTANGULAR_AREA_FIELD, "可用长_mm", "可用宽_mm", "余料特惠单价_元每公斤",
    ])
    grades = stock["材质牌号"].astype("string").fillna("").str.strip().str.upper()
    distances = pd.to_numeric(stock["距中心距离_km"], errors="coerce")
    thicknesses = pd.to_numeric(stock["厚度_mm"], errors="coerce")
    lengths = pd.to_numeric(stock["可用长_mm"], errors="coerce")
    widths = pd.to_numeric(stock["可用宽_mm"], errors="coerce")
    prices = pd.to_numeric(stock["余料特惠单价_元每公斤"], errors="coerce")
    shapes = stock["余料形态"].astype("string").fillna("").str.strip().str.lower()

    same_grade = grades.eq(mat_key)
    nearby = distances.between(0, 3)
    same_thickness = (thicknesses - thickness_mm).abs().le(0.5)
    # 已知不符合询盘条件的库存先排除，资料缺失的其余候选留给人工复核。
    candidates = ((same_grade | grades.eq("")) & (nearby | distances.isna())
                  & (same_thickness | thicknesses.isna()))
    rectangular = stock[RECTANGULAR_AREA_FIELD].astype("string").fillna("").str.strip().eq("是")
    known_shape = ~shapes.isin(("", "-", "—", "未知", "不明", "形状不明", "未标注", "待确认", "unknown"))
    qualified = (same_grade & nearby & same_thickness & rectangular & known_shape
                 & lengths.between(0, math.inf, inclusive="neither")
                 & widths.between(0, math.inf, inclusive="neither")
                 & prices.between(0, math.inf, inclusive="left")
                 & stock[factory_fields].notna().all(axis=1))
    fits = qualified & lengths.ge(req_length_mm) & widths.ge(req_width_mm)
    sub = df.loc[fits].assign(**{
        "可用长_mm": lengths.loc[fits], "可用宽_mm": widths.loc[fits],
    })

    if sub.empty:
        if (candidates & ~qualified).any():
            return {
                "是否匹配成功": False,
                "撮合状态": SCRAP_REVIEW_STATUS,
                "撮合播报话术": "演示余料池有候选库存，请工厂复核库存记录、余料形状及实际可用矩形区域。",
            }
        return {
            "是否匹配成功": False,
            "撮合状态": "无匹配余料",
            "撮合播报话术": f"戴南中心 3 公里内暂未找到 {thickness_mm}mm 厚 {mat_key} 匹配余料。",
        }

    matches: List[Dict[str, Any]] = []
    for _, r in sub.iterrows():
        fit_n = int(r["可用长_mm"] // req_length_mm) * int(r["可用宽_mm"] // req_width_mm)
        if fit_n <= 0:
            continue
        stock_blocks = int(r["库存数量_块"]) if "库存数量_块" in r and pd.notna(r["库存数量_块"]) else 1
        max_possible_pcs = fit_n * stock_blocks
        used_n = min(quantity, max_possible_pcs)
        s_price = float(r["余料特惠单价_元每公斤"])
        save_pct = round(((new_price_ex - s_price) / new_price_ex) * 100, 1)
        blank_kg = (req_length_mm * req_width_mm * thickness_mm * rule["density"]) / 1_000_000.0
        saved_rmb = round(blank_kg * used_n * (new_price_ex - s_price), 2)

        matches.append({
            "工厂编号": r["工厂编号"],
            "合作工厂": r["脱敏厂名"],
            "所在位置": r["所在园区_镇街"],
            "直线距离_km": float(r["距中心距离_km"]),
            "余料规格": f"{r['可用长_mm']}×{r['可用宽_mm']}×{r['厚度_mm']}mm ({r['余料形态']})",
            "可拼单件数": used_n,
            "余料特惠价_元每kg": s_price,
            "节省比例_%": save_pct,
            "直接节省金额_元": saved_rmb,
        })

    matches.sort(key=lambda x: (-x["直接节省金额_元"], x["直线距离_km"]))
    best = matches[0]
    return {
        "是否匹配成功": True,
        "撮合状态": "匹配成功",
        "最优拼单推荐": best,
        "撮合播报话术": (
            f"命中戴南余料池！距您 {best['直线距离_km']}km 的【{best['所在位置']}·{best['合作工厂']}】"
            f"现有 {best['余料规格']}，套切本单 {best['可拼单件数']} 件可节省材料费 {best['节省比例_%']}%（立省 {best['直接节省金额_元']} 元）！"
        ),
    }
