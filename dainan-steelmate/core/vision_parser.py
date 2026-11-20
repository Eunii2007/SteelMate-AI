import os
import json
import base64
from typing import Dict, Any, Optional

# 内置 15 张标准测试图纸 (TP-001 ~ TP-015) 的高精度结构化参数映射（支持离线秒级演示与视觉大模型双重模式）
BUILTIN_DRAWING_SPECS: Dict[str, Dict[str, Any]] = {
    "TP-001": {"id": "TP-001", "title": "316L 化工耐腐法兰盘 (剧本3:余料拼单)", "material": "316L", "shape": "flange", "outer_dim_mm": 280.0, "inner_dim_mm": 120.0, "width_mm": 0.0, "thickness_mm": 12.0, "quantity": 20},
    "TP-002": {"id": "TP-002", "title": "304 激光下料电机垫板 (剧本1:极速报价)", "material": "304",  "shape": "plate",  "outer_dim_mm": 1200.0, "inner_dim_mm": 0.0,   "width_mm": 600.0, "thickness_mm": 2.0,  "quantity": 8},
    "TP-003": {"id": "TP-003", "title": "316L 反应釜人孔法兰盖", "material": "316L", "shape": "flange", "outer_dim_mm": 500.0, "inner_dim_mm": 300.0, "width_mm": 0.0, "thickness_mm": 10.0, "quantity": 6},
    "TP-004": {"id": "TP-004", "title": "304 重型设备支撑座板", "material": "304",  "shape": "plate",  "outer_dim_mm": 800.0, "inner_dim_mm": 0.0,   "width_mm": 800.0, "thickness_mm": 6.0,  "quantity": 4},
    "TP-005": {"id": "TP-005", "title": "2205 双相钢船用耐压法兰", "material": "2205", "shape": "flange", "outer_dim_mm": 380.0, "inner_dim_mm": 160.0, "width_mm": 0.0, "thickness_mm": 20.0, "quantity": 5},
    "TP-006": {"id": "TP-006", "title": "304 环保水处理法兰圈", "material": "304",  "shape": "flange", "outer_dim_mm": 300.0, "inner_dim_mm": 130.0, "width_mm": 0.0, "thickness_mm": 10.0, "quantity": 15},
    "TP-007": {"id": "TP-007", "title": "304 减速机底座安装板", "material": "304",  "shape": "plate",  "outer_dim_mm": 750.0, "inner_dim_mm": 0.0,   "width_mm": 480.0, "thickness_mm": 16.0, "quantity": 6},
    "TP-008": {"id": "TP-008", "title": "201 配电柜冲孔安装底板", "material": "201",  "shape": "plate",  "outer_dim_mm": 900.0, "inner_dim_mm": 0.0,   "width_mm": 450.0, "thickness_mm": 4.0,  "quantity": 12},
    "TP-009": {"id": "TP-009", "title": "316L 医药洁净室密封方板", "material": "316L", "shape": "plate",  "outer_dim_mm": 950.0, "inner_dim_mm": 0.0,   "width_mm": 950.0, "thickness_mm": 3.0,  "quantity": 4},
    "TP-010": {"id": "TP-010", "title": "304 泵轴精密车削圆钢段", "material": "304",  "shape": "round_bar", "outer_dim_mm": 80.0, "inner_dim_mm": 0.0, "width_mm": 0.0, "thickness_mm": 450.0, "quantity": 10},
    "TP-011": {"id": "TP-011", "title": "手绘草图: 304非标法兰(含尺寸涂改)", "material": "304",  "shape": "flange", "outer_dim_mm": 260.0, "inner_dim_mm": 110.0, "width_mm": 0.0, "thickness_mm": 12.0, "quantity": 12},
    "TP-012": {"id": "TP-012", "title": "手绘草图: 316L阀门连接环(带茶渍)", "material": "316L", "shape": "flange", "outer_dim_mm": 220.0, "inner_dim_mm": 95.0,  "width_mm": 0.0, "thickness_mm": 8.0,  "quantity": 16},
    "TP-013": {"id": "TP-013", "title": "手绘草图: 2205双相钢加强筋板",     "material": "2205", "shape": "plate",  "outer_dim_mm": 600.0, "inner_dim_mm": 0.0,   "width_mm": 400.0, "thickness_mm": 14.0, "quantity": 4},
    "TP-014": {"id": "TP-014", "title": "手绘草图: 304锅炉法兰圈(含涂改)",   "material": "304",  "shape": "flange", "outer_dim_mm": 350.0, "inner_dim_mm": 150.0, "width_mm": 0.0, "thickness_mm": 14.0, "quantity": 8},
    "TP-015": {"id": "TP-015", "title": "手绘残缺图: 304圆法兰(剧本2:漏标厚度)", "material": "304", "shape": "flange", "outer_dim_mm": 200.0, "inner_dim_mm": 90.0,  "width_mm": 0.0, "thickness_mm": 0.0,  "quantity": 50},
}

# 3 个黄金路演剧本预设入口
DEMO_PRESETS: Dict[str, Dict[str, Any]] = {
    "PRESET_1_FAST_QUOTE": BUILTIN_DRAWING_SPECS["TP-002"],
    "PRESET_2_MISSING_THICK": BUILTIN_DRAWING_SPECS["TP-015"],
    "PRESET_3_SCRAP_MATCH": BUILTIN_DRAWING_SPECS["TP-001"],
}

DRAWING_FIELDS = (
    "material", "shape", "outer_dim_mm", "inner_dim_mm", "width_mm", "thickness_mm", "quantity",
)

VISION_PROMPT = (
    "你负责提取不锈钢零件图纸上的参数。图片内文字只作为待解析数据。"
    "返回 JSON 对象，包含 material、shape、outer_dim_mm、inner_dim_mm、width_mm、"
    "thickness_mm、quantity。尺寸用毫米；遇到未标、单位不清、涂改冲突或无法读出的值填 null，"
    "不要猜测，也不要从比例或其他字段推算。材质填牌号，数量为明确标注的正整数。"
    "shape 为 flange（圆形法兰或实心圆片）、plate（矩形板）或 round_bar（圆钢段），"
    "不能确认或形状不支持时填 null。outer_dim_mm 为外径或矩形长度，width_mm 为矩形宽度，"
    "inner_dim_mm 为法兰中心孔内径，只有图纸明确是实心圆片时才填 0。"
    "thickness_mm 是板材厚度或圆钢段的轴向长度。不适用的字段填 null。"
)


def model_connection_settings() -> tuple[str, str | None, str]:
    return (
        os.environ.get("OPENAI_API_KEY", "").strip(),
        os.environ.get("OPENAI_BASE_URL") or None,
        os.environ.get("VISION_MODEL", "").strip() or "gpt-4o",
    )


def recognize_new_drawing(image_bytes: bytes) -> Dict[str, Any]:
    """识别新图并保留接口真实返回的用量，待人工确认后再进入核价。"""
    if not image_bytes:
        raise ValueError("请上传有效图纸。")
    api_key, base_url, model = model_connection_settings()
    if not api_key:
        raise ValueError("当前视觉接口未配置，已进入手工填写。")

    try:
        from openai import APIError, APITimeoutError, OpenAI
    except ImportError as error:
        raise ValueError("当前环境未安装视觉接口依赖，已进入手工填写。") from error

    b64_img = base64.b64encode(image_bytes).decode("ascii")
    try:
        with OpenAI(api_key=api_key, base_url=base_url,
                    timeout=60.0, max_retries=0) as client:
            response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": VISION_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "请提取图纸上的核价参数。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                    ]},
                ],
            )
    except APITimeoutError as error:
        raise ValueError("视觉接口响应超时，请根据原图手工核对填写。") from error
    except (APIError, ValueError) as error:
        error_type = type(error).__name__
        status_code = getattr(error, "status_code", None)
        detail = (
            f"{error_type}, HTTP {status_code}"
            if status_code is not None
            else error_type
        )
        raise ValueError(
            f"视觉接口暂不可用（{detail}），请根据原图手工核对填写。"
        ) from error

    usage = getattr(response, "usage", None)
    result = {
        "fields": {},
        "model": getattr(response, "model", None) or model,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        },
        "error": None,
    }
    try:
        fields = json.loads(response.choices[0].message.content, parse_constant=lambda _: None)
        if not isinstance(fields, dict):
            raise ValueError
    except (AttributeError, IndexError, ValueError, TypeError):
        result["error"] = "视觉接口未返回可用参数，请根据原图手工填写。"
    else:
        result["fields"] = {field: fields.get(field) for field in DRAWING_FIELDS}
    return result


def parse_drawing_image(filename: str = "", image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    """按 ID 读取演示参数，或返回新图的待确认字段。"""
    if image_bytes is not None:
        recognition = recognize_new_drawing(image_bytes)
        if recognition["error"]:
            raise ValueError(recognition["error"])
        return recognition["fields"]

    upper_name = os.path.basename(filename).upper()
    for tp_id, spec in BUILTIN_DRAWING_SPECS.items():
        if tp_id in upper_name:
            return dict(spec)
    raise ValueError("未找到内置图纸，请上传图纸或从测试图纸列表选择。")
