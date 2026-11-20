import json
import hashlib
import io
import os
import sqlite3
import warnings
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image


SCRAP_REVIEW_STATUSES = ("待复核", "确认可供", "无法供料")
CUSTOMER_INFO_FIELDS = {
    "short_name": ("客户简称", 40),
    "project_name": ("项目名称", 80),
    "delivery_note": ("交期备注", 200),
}
BEIJING_TIME = timezone(timedelta(hours=8))


def parse_quote_deadline(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return None
    text = value.strip().removesuffix("（北京时间）").removesuffix("(北京时间)").strip()
    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            # 旧记录只有日期时，“有效期至”包含当天，以次日零时为到期边界。
            return (datetime.strptime(text, date_format) + timedelta(days=1)).replace(tzinfo=BEIJING_TIME)
        except (ValueError, OverflowError):
            pass
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        for date_format in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y年%m月%d日 %H:%M:%S"):
            try:
                deadline = datetime.strptime(text, date_format)
                break
            except ValueError:
                pass
        else:
            return None
    try:
        return deadline.replace(tzinfo=BEIJING_TIME) if deadline.tzinfo is None else deadline.astimezone(BEIJING_TIME)
    except (ValueError, OverflowError):
        return None


def quote_validity(valid_until: object, now: datetime | None = None) -> str:
    deadline = parse_quote_deadline(valid_until)
    if deadline is None:
        return "有效期待核对"
    current = now if now is not None else datetime.now(BEIJING_TIME)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TIME)
    return "已过期" if current >= deadline else "有效期内"


def quote_deadline_text(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            pass
        else:
            deadline = parse_quote_deadline(value)
            if parsed.tzinfo is not None and deadline is not None:
                return deadline.strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value is not None and str(value).strip() else "未记录"


def clean_customer_info(customer: dict | None = None) -> dict[str, str]:
    if customer is not None and not isinstance(customer, dict):
        raise ValueError("客户信息格式无效。")
    result = {}
    for field, (label, limit) in CUSTOMER_INFO_FIELDS.items():
        value = (customer or {}).get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"{label}须为文本。")
        value = value.strip()
        if len(value) > limit or any(ord(character) < 32 for character in value):
            raise ValueError(f"{label}最多 {limit} 字，请使用单行文本。")
        result[field] = value
    return result


def database_path() -> Path:
    if override := os.environ.get("DAINAN_QUOTE_DB"):
        return Path(override).expanduser()
    if app_data := os.environ.get("LOCALAPPDATA"):
        return Path(app_data) / "DainanSteelAI" / "quotes.sqlite3"
    return Path.home() / ".dainan-steel-ai" / "quotes.sqlite3"


def _initialize_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA user_version").fetchone()[0]:
        return
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA user_version").fetchone()[0]:
            return
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quotes'"
        ).fetchone()
        if legacy:
            connection.execute("ALTER TABLE quotes RENAME TO legacy_quotes")
        connection.execute("""
            CREATE TABLE quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                save_key TEXT NOT NULL UNIQUE,
                source_quote_id INTEGER,
                filename TEXT NOT NULL,
                issued TEXT NOT NULL,
                snapshot TEXT NOT NULL
            )
        """)
        if legacy:
            # 保留历史编号和来源。来源单删除后，改价单仍保留原编号。
            connection.execute("""
                INSERT INTO quotes (id, save_key, source_quote_id, filename, issued, snapshot)
                SELECT id, save_key, source_quote_id, filename, issued, snapshot FROM legacy_quotes
            """)
            connection.execute("DROP TABLE legacy_quotes")
        connection.execute("CREATE TABLE deleted_quote_keys (save_key TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")


@contextmanager
def connect_database():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _initialize_schema(connection)
        with connection:
            yield connection


def quote_number(quote_id: int) -> str:
    return f"Q-{quote_id:06d}"


def validate_drawing_image(data: bytes, filename: str) -> dict:
    if not isinstance(data, bytes) or not data or len(data) > 200 * 1024 * 1024:
        raise ValueError("图纸须为不超过 200 MB 的有效图片。")
    if not isinstance(filename, str):
        raise ValueError("图纸文件名无效。")
    filename = Path(filename.replace("\\", "/")).name.strip()
    if not filename or len(filename) > 255 or any(ord(character) < 32 for character in filename):
        raise ValueError("图纸文件名无效或过长。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as drawing:
                drawing.load()
                mime_type = Image.MIME[drawing.format]
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("无法打开图片，请使用有效的 PNG、JPEG 或 WebP 图纸。") from error
    return {"filename": filename, "mime_type": mime_type, "sha256": hashlib.sha256(data).hexdigest(), "data": data}


def _ensure_drawings(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS drawing_assets (
            sha256 TEXT PRIMARY KEY,
            mime_type TEXT NOT NULL,
            data BLOB NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS quote_drawings (
            quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            line_position INTEGER NOT NULL CHECK (line_position >= 1),
            sha256 TEXT NOT NULL REFERENCES drawing_assets(sha256),
            filename TEXT NOT NULL,
            PRIMARY KEY (quote_id, line_position)
        )
    """)


def get_quote_drawing(quote_id: int, line_position: int = 1) -> dict | None:
    if type(line_position) is not int or line_position < 1:
        raise ValueError("找不到这条图纸明细。")
    with connect_database() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quote_drawings'").fetchone():
            return None
        row = connection.execute("""
            SELECT links.filename, assets.mime_type, assets.sha256, assets.data
            FROM quote_drawings AS links JOIN drawing_assets AS assets USING (sha256)
            WHERE links.quote_id = ? AND links.line_position = ?
        """, (quote_id, line_position)).fetchone()
    return dict(zip(("filename", "mime_type", "sha256", "data"), row)) if row else None


def _ensure_work_draft(connection: sqlite3.Connection) -> None:
    _ensure_drawings(connection)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS work_draft (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS work_draft_drawings (
            draft_id INTEGER NOT NULL REFERENCES work_draft(id) ON DELETE CASCADE CHECK (draft_id = 1),
            slot TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL REFERENCES drawing_assets(sha256),
            filename TEXT NOT NULL
        )
    """)


def load_work_draft() -> dict | None:
    with connect_database() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'work_draft'").fetchone():
            return None
        row = connection.execute("SELECT payload, updated_at FROM work_draft WHERE id = 1").fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("草稿版本无法读取，请保留本机数据库。")
        drawings = connection.execute("""
            SELECT links.slot, links.filename, assets.mime_type, assets.sha256, assets.data
            FROM work_draft_drawings AS links JOIN drawing_assets AS assets USING (sha256)
            WHERE links.draft_id = 1
        """).fetchall()
    return {"payload": payload, "updated_at": row[1], "drawings": {
        image[0]: dict(zip(("filename", "mime_type", "sha256", "data"), image[1:])) for image in drawings
    }}


def save_work_draft(payload: dict, drawings: dict[str, dict] | None = None) -> str:
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or set(payload) - {"version", "inquiry", "order"}
            or not (payload.get("inquiry") or payload.get("order"))):
        raise ValueError("未完成询盘格式无效。")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    slots = {"inquiry"} if payload.get("inquiry") else set()
    slots.update(f'line:{item["line_id"]}' for item in (payload.get("order") or {}).get("items", []))
    prepared = {}
    for slot, drawing in (drawings or {}).items():
        if slot not in slots:
            raise ValueError("草稿图纸关联的明细无效。")
        prepared[slot] = validate_drawing_image(drawing["data"], drawing["filename"])
    with connect_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_work_draft(connection)
        current = connection.execute("SELECT payload, updated_at FROM work_draft WHERE id = 1").fetchone()
        old_links = connection.execute("SELECT slot, sha256, filename FROM work_draft_drawings").fetchall()
        links = [(slot, drawing["sha256"], drawing["filename"]) for slot, drawing in prepared.items()]
        if current and current[0] == encoded and sorted(old_links) == sorted(links):
            return current[1]
        updated_at = datetime.now(BEIJING_TIME).isoformat(timespec="seconds")
        connection.execute("""
            INSERT INTO work_draft VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
        """, (encoded, updated_at))
        connection.execute("DELETE FROM work_draft_drawings WHERE draft_id = 1")
        for slot, drawing in prepared.items():
            connection.execute("INSERT OR IGNORE INTO drawing_assets VALUES (?, ?, ?)",
                               (drawing["sha256"], drawing["mime_type"], drawing["data"]))
            connection.execute("INSERT INTO work_draft_drawings VALUES (1, ?, ?, ?)",
                               (slot, drawing["sha256"], drawing["filename"]))
    return updated_at


def clear_work_draft() -> None:
    with connect_database() as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'work_draft'").fetchone():
            # 只清理最近工作及其图纸关联，共用原图和正式报价保持不动。
            connection.execute("DELETE FROM work_draft WHERE id = 1")


def save_quote(save_key: str, snapshot: dict, source_quote_id: int | None = None,
               *, drawings: dict[int, dict] | None = None) -> int | None:
    if "customer" in snapshot:
        snapshot = {**snapshot, "customer": clean_customer_info(snapshot["customer"])}
    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    line_count = len(snapshot["items"]) if snapshot.get("kind") == "order" else 1
    prepared = {}
    for position, drawing in (drawings or {}).items():
        if type(position) is not int or not 1 <= position <= line_count:
            raise ValueError("图纸关联的报价明细无效。")
        prepared[position] = validate_drawing_image(drawing["data"], drawing["filename"])
    with connect_database() as connection:
        # 同一保存操作复用现有编号，晚到的重复请求也不会写回已删除报价。
        inserted = connection.execute("""
            INSERT INTO quotes (save_key, source_quote_id, filename, issued, snapshot)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM quotes WHERE save_key = ?
                UNION ALL SELECT 1 FROM deleted_quote_keys WHERE save_key = ?
            )
        """, (save_key, source_quote_id, snapshot["filename"], snapshot["issued"], payload, save_key, save_key))
        row = connection.execute("SELECT id FROM quotes WHERE save_key = ?", (save_key,)).fetchone()
        if inserted.rowcount and row and prepared:
            # 报价与原图关联共用事务，任何一张原图写入失败时一起回滚。
            _ensure_drawings(connection)
            for position, drawing in prepared.items():
                connection.execute("INSERT OR IGNORE INTO drawing_assets VALUES (?, ?, ?)",
                                   (drawing["sha256"], drawing["mime_type"], drawing["data"]))
                connection.execute("INSERT INTO quote_drawings VALUES (?, ?, ?, ?)",
                                   (row[0], position, drawing["sha256"], drawing["filename"]))
        return row[0] if row is not None else None


def delete_quote(quote_id: int) -> bool:
    with connect_database() as connection:
        row = connection.execute("DELETE FROM quotes WHERE id = ? RETURNING save_key", (quote_id,)).fetchone()
        if row is None:
            return False
        connection.execute("INSERT INTO deleted_quote_keys (save_key) VALUES (?)", row)
        return True


def _decode_quote(row: tuple) -> dict:
    quote_id, source_quote_id, snapshot = row
    return {"id": quote_id, "source_quote_id": source_quote_id, "snapshot": json.loads(snapshot)}


def list_quotes(search: str = "") -> list[dict]:
    search = search.strip()
    with connect_database() as connection:
        rows = connection.execute("""
            SELECT id, source_quote_id, snapshot FROM quotes
            WHERE instr(lower(printf('Q-%06d', id)), lower(?)) > 0
                OR instr(lower(filename), lower(?)) > 0
                OR instr(lower(coalesce(json_extract(snapshot, '$.customer.short_name'), '')), lower(?)) > 0
                OR instr(lower(coalesce(json_extract(snapshot, '$.customer.project_name'), '')), lower(?)) > 0
            ORDER BY issued DESC, id DESC
        """, (search, search, search, search)).fetchall()
    return [_decode_quote(row) for row in rows]


def get_quote(quote_id: int) -> dict | None:
    with connect_database() as connection:
        row = connection.execute(
            "SELECT id, source_quote_id, snapshot FROM quotes WHERE id = ?", (quote_id,)
        ).fetchone()
    return _decode_quote(row) if row is not None else None


def _ensure_scrap_reviews(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS scrap_reviews (
            quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
            line_position INTEGER NOT NULL CHECK (line_position >= 1),
            status TEXT NOT NULL CHECK (status IN ('待复核', '确认可供', '无法供料')),
            confirmed_quantity INTEGER CHECK (confirmed_quantity >= 0),
            reviewed_at TEXT NOT NULL,
            measured_dimensions TEXT NOT NULL,
            note TEXT NOT NULL,
            PRIMARY KEY (quote_id, line_position)
        )
    """)


def get_scrap_review(quote_id: int, line_position: int) -> dict | None:
    with connect_database() as connection:
        _ensure_scrap_reviews(connection)
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
            SELECT status, confirmed_quantity, reviewed_at, measured_dimensions, note
            FROM scrap_reviews WHERE quote_id = ? AND line_position = ?
        """, (quote_id, line_position)).fetchone()
    return dict(row) if row is not None else None


def save_scrap_review(quote_id: int, line_position: int, status: str,
                      confirmed_quantity: int | None = None, measured_dimensions: str = "",
                      note: str = "") -> None:
    if status not in SCRAP_REVIEW_STATUSES:
        raise ValueError("请选择有效的复核状态。")
    if type(line_position) is not int or line_position < 1:
        raise ValueError("找不到这条报价明细。")
    if len(measured_dimensions) > 120 or len(note) > 500:
        raise ValueError("实测尺寸最多 120 字，复核备注最多 500 字。")

    with connect_database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT snapshot FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if row is None:
            raise ValueError("该报价已删除或不存在。")
        snapshot = json.loads(row[0])
        lines = snapshot["items"] if snapshot.get("kind") == "order" else [snapshot]
        if line_position > len(lines):
            raise ValueError("找不到这条报价明细。")
        line = lines[line_position - 1]
        if not line.get("scrap", {}).get("最优拼单推荐"):
            raise ValueError("该明细暂无明确余料候选。")
        quantity = line["inquiry"]["quantity"]
        if status == "确认可供":
            if type(confirmed_quantity) is not int or not 1 <= confirmed_quantity <= quantity:
                raise ValueError(f"确认可用件数须为 1 至 {quantity} 的整数。")
        else:
            confirmed_quantity = 0 if status == "无法供料" else None

        _ensure_scrap_reviews(connection)
        reviewed_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("""
            INSERT INTO scrap_reviews
                (quote_id, line_position, status, confirmed_quantity, reviewed_at, measured_dimensions, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(quote_id, line_position) DO UPDATE SET
                status = excluded.status, confirmed_quantity = excluded.confirmed_quantity,
                reviewed_at = excluded.reviewed_at, measured_dimensions = excluded.measured_dimensions,
                note = excluded.note
        """, (quote_id, line_position, status, confirmed_quantity, reviewed_at,
              measured_dimensions.strip(), note.strip()))
