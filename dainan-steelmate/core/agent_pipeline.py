from typing import Dict, Any, List
from core.steel_engine import calculate_stainless_quote, match_local_scrap


def run_pipeline(inquiry_input: Dict[str, Any]) -> Dict[str, Any]:
    """三智能体标准协同流水线唯一入口。"""
    agent_thoughts: List[Dict[str, str]] = []

    missing_fields = []
    if not inquiry_input.get("material"):
        missing_fields.append("材质牌号(如304/316L)")
    if not inquiry_input.get("thickness_mm") or float(inquiry_input.get("thickness_mm", 0)) <= 0:
        missing_fields.append("板材厚度(mm)")
    if not inquiry_input.get("outer_dim_mm") or float(inquiry_input.get("outer_dim_mm", 0)) <= 0:
        missing_fields.append("主尺寸/外径(mm)")

    if missing_fields:
        missing_str = "、".join(missing_fields)
        followup_msg = (
            f"老板您好，刚看了一下您发的图纸，上面漏标了【{missing_str}】。"
            f"咱们戴南这边不同厚度下差和激光费差不少，您看是按国标足厚还是常规下差板给您核价？"
        )
        agent_thoughts.append({
            "agent": "Agent 1 · 询盘解析风控员",
            "status": "拦截追问",
            "detail": f"检测到关键参数缺失：{missing_str}，已暂停算料并生成微信追问话术。"
        })
        return {
            "status": "NEED_INFO",
            "missing_fields": missing_fields,
            "followup_msg": followup_msg,
            "quote_result": None,
            "scrap_result": None,
            "agent_thoughts": agent_thoughts,
        }

    mat = str(inquiry_input["material"]).upper()
    shape = inquiry_input.get("shape", "flange")
    outer_d = float(inquiry_input["outer_dim_mm"])
    inner_d = float(inquiry_input.get("inner_dim_mm") or 0.0)
    width_d = float(inquiry_input.get("width_mm") or 0.0)
    thick = float(inquiry_input["thickness_mm"])
    qty = int(inquiry_input.get("quantity") or 1)

    agent_thoughts.append({
        "agent": "Agent 1 · 询盘解析风控员",
        "status": "校验通过",
        "detail": f"已完成图纸结构化提取：材质 {mat}，形状 {shape}，主尺寸 {outer_d}mm，厚度 {thick}mm，数量 {qty} 件。"
    })

    quote_res = calculate_stainless_quote(
        material=mat, shape=shape, thickness_mm=thick,
        quantity=qty, outer_dim_mm=outer_d, width_mm=width_d, inner_dim_mm=inner_d
    )
    agent_thoughts.append({
        "agent": "Agent 2 · 戴南资深核价师",
        "status": "核算完成",
        "detail": (
            f"已调用钢算引擎完成双轨核价：理计不含税价 ¥{quote_res['方案A_理计不含税自提价_元']}，"
            f"过磅含13%专票价 ¥{quote_res['方案B_过磅含13专票价_元']}（材料利用率 {quote_res['材料利用率_%']}%）。"
        )
    })

    req_w = width_d if (shape == "plate" and width_d > 0) else outer_d
    scrap_res = match_local_scrap(
        material=mat, thickness_mm=thick,
        req_length_mm=outer_d, req_width_mm=req_w, quantity=qty
    )
    agent_thoughts.append({
        "agent": "Agent 3 · 产业带余料撮合员",
        "status": scrap_res["撮合状态"],
        "detail": scrap_res["撮合播报话术"]
    })

    return {
        "status": "SUCCESS",
        "missing_fields": [],
        "followup_msg": "",
        "quote_result": quote_res,
        "scrap_result": scrap_res,
        "agent_thoughts": agent_thoughts,
    }


if __name__ == "__main__":
    res = run_pipeline({"material": "316L", "shape": "flange", "outer_dim_mm": 280, "inner_dim_mm": 120, "thickness_mm": 12, "quantity": 20})
    print("Pipeline status:", res["status"])
    for t in res["agent_thoughts"]:
        print(f"[{t['agent']}] ({t['status']}): {t['detail']}")
