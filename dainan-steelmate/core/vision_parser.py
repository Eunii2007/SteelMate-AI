import os
import json
import base64
import io
import math
import warnings
from decimal import Decimal, InvalidOperation
from itertools import chain
from time import monotonic
from typing import Dict, Any, Optional, Iterator

from PIL import Image, ImageOps

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


# 256 KiB 图片经 Base64 编码约为 342 KiB，给网关请求体留下余量。
# 这是保守的发送预算，不代表服务商公布的上限。
VISION_IMAGE_BUDGET = 256 * 1024
VISION_TILE_SIDE = 2048
MAX_VISION_TILES = 8
VISION_TIMEOUT_SECONDS = 20.0


def _encode_vision_image(image: Image.Image, max_side: int = VISION_TILE_SIDE) -> str:
    drawing = image.copy()
    drawing.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    while True:
        buffer = io.BytesIO()
        drawing.save(buffer, format="PNG", optimize=True)
        if buffer.tell() <= VISION_IMAGE_BUDGET:
            return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        for quality in (90, 80, 70):
            buffer = io.BytesIO()
            drawing.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= VISION_IMAGE_BUDGET:
                return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        longest = max(drawing.size)
        if longest <= 768:
            raise ValueError("图纸局部压缩后仍过大，请裁剪所需零件区域，或根据原图手工填写。")
        next_side = max(768, int(longest * 0.8))
        drawing.thumbnail((next_side, next_side), Image.Resampling.LANCZOS)


def _drawing_views(image_bytes: bytes) -> Iterator[tuple[str, str]]:
    if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > 200 * 1024 * 1024:
        raise ValueError("图纸须为不超过 200 MB 的有效图片。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes), formats=("PNG", "JPEG", "WEBP")) as source:
                # 限制解码内存；超大画布应先按零件分图，不在云端无限展开。
                if source.width * source.height > 40_000_000 or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("图纸超过 4000 万像素或包含动画，请按零件分图后上传。")
                source.load()
                oriented = ImageOps.exif_transpose(source).convert("RGBA")
                drawing = Image.new("RGB", oriented.size, "white")
                drawing.paste(oriented, mask=oriented.getchannel("A"))
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("无法安全读取图纸，请使用有效的 PNG、JPEG 或 WebP 图片。") from error

    columns = math.ceil(drawing.width / VISION_TILE_SIDE)
    rows = math.ceil(drawing.height / VISION_TILE_SIDE)
    if columns * rows > MAX_VISION_TILES:
        raise ValueError("图纸需要超过 8 个识别局部，请按零件分图，或根据原图手工填写。")
    if columns == rows == 1:
        yield "整张图纸", _encode_vision_image(drawing)
        return

    yield "整张图纸概览", _encode_vision_image(drawing, 1600)
    for row in range(rows):
        for column in range(columns):
            # 局部重叠，减少尺寸文字恰好被切在分界线上的情况。
            left = max(0, int(column * drawing.width / columns) - 96)
            top = max(0, int(row * drawing.height / rows) - 96)
            right = min(drawing.width, math.ceil((column + 1) * drawing.width / columns) + 96)
            bottom = min(drawing.height, math.ceil((row + 1) * drawing.height / rows) + 96)
            yield (f"同一零件局部：第 {row + 1} 行、第 {column + 1} 列",
                   _encode_vision_image(drawing.crop((left, top, right, bottom))))


def _merge_drawing_fields(parts: list[dict]) -> dict:
    merged = {}
    for field in DRAWING_FIELDS:
        values = set()
        for part in parts:
            value = part.get(field)
            if value is None or isinstance(value, bool):
                continue
            if field in ("material", "shape"):
                if isinstance(value, str) and value.strip():
                    values.add(value.strip().upper() if field == "material" else value.strip().lower())
            else:
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    continue
                if not number.is_finite() or not 0 <= number <= 2**53 - 1 or (field != "inner_dim_mm" and number == 0):
                    continue
                if field == "quantity" and number != number.to_integral_value():
                    continue
                values.add(number)
        # 不用“最后一次识别覆盖”：冲突字段留空，交由原有人工确认流程处理。
        selected = next(iter(values)) if len(values) == 1 else None
        if isinstance(selected, Decimal):
            selected = int(selected) if field == "quantity" else float(selected)
        merged[field] = selected
    return merged


def _has_required_fields(fields: dict) -> bool:
    shape = fields.get("shape")
    if shape not in {"flange", "plate", "round_bar"}:
        return False
    required = ["material", "shape", "outer_dim_mm", "thickness_mm", "quantity"]
    if shape == "flange":
        required.append("inner_dim_mm")
    elif shape == "plate":
        required.append("width_mm")
    return all(fields.get(field) is not None for field in required)


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

    started = monotonic()
    views = _drawing_views(image_bytes)
    first_view = next(views)
    result = {
        "fields": {}, "model": model,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "error": None,
    }
    parts = []
    try:
        with OpenAI(api_key=api_key, base_url=base_url,
                    timeout=VISION_TIMEOUT_SECONDS, max_retries=0) as client:
            for label, image_url in chain((first_view,), views):
                remaining = VISION_TIMEOUT_SECONDS - (monotonic() - started)
                if remaining <= 0:
                    result["error"] = f"图纸处理达到 {VISION_TIMEOUT_SECONDS:g} 秒等待预算，请根据原图手工核对填写。"
                    return result
                response = client.chat.completions.create(
                    model=model,
                    timeout=remaining,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": VISION_PROMPT},
                        {"role": "user", "content": [
                            {"type": "text", "text": f"{label}。请只提取此图明确标注的核价参数。"
                             "各次图片均来自同一张图纸；局部没有标注的字段填 null，不从像素或概览推算。"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ]},
                    ],
                )
                result["model"] = getattr(response, "model", None) or model
                usage = getattr(response, "usage", None)
                for key, previous in result["usage"].items():
                    count = getattr(usage, key, None)
                    result["usage"][key] = previous + count if (
                        previous is not None and type(count) is int and count >= 0
                    ) else None
                try:
                    fields = json.loads(response.choices[0].message.content, parse_constant=lambda _: None)
                    if not isinstance(fields, dict):
                        raise ValueError
                except (AttributeError, IndexError, ValueError, TypeError):
                    result["error"] = "视觉接口未返回可用参数，请根据原图手工填写。"
                    return result
                parts.append(fields)
                # 参数齐全即停止；缺参时才在剩余等待预算内补读局部。
                if _has_required_fields(_merge_drawing_fields(parts)):
                    break
    except APITimeoutError as error:
        if parts:
            result["error"] = "部分图纸局部识别超时，已保留已返回用量，请根据原图手工核对填写。"
            return result
        raise ValueError("视觉接口响应超时，请根据原图手工核对填写。") from error
    except (APIError, ValueError) as error:
        error_type = type(error).__name__
        status_code = getattr(error, "status_code", None)
        detail = f"{error_type}, HTTP {status_code}" if status_code is not None else error_type
        message = f"视觉接口暂不可用（{detail}），请根据原图手工核对填写。"
        if status_code == 413:
            message = "图纸已自动优化，但接口仍拒绝请求（HTTP 413），请核实服务商请求体上限或裁剪零件区域。"
        if parts:
            result["error"] = message
            return result
        raise ValueError(message) from error
    finally:
        views.close()

    result["fields"] = _merge_drawing_fields(parts)
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
