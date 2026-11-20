"""Signed anonymous identities and per-execution storage scope (no shared fallback)."""

import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


COOKIE_NAME = "dainan_visitor_v1"
COOKIE_DAYS = 30
COOKIE_KEY = "_visitor_cookie"
_TOKEN_PATTERN = re.compile(r"v1\.([0-9a-f]{64})\.([0-9]{10})\.([0-9]{10})\.([0-9a-f]{64})")
_AUTH_KEYS = {COOKIE_KEY, "_visitor_owner", "_visitor_pending", "_visitor_session_only"}


class VisitorAccessError(RuntimeError):
    """Private storage must remain closed until an identity is verified."""


@dataclass(frozen=True, repr=False)
class VisitorAccess:
    credential: str = field(repr=False)
    signing_key: str = field(repr=False)
    root: Path
    owner: str
    session_only: bool = False

    def database_path(self) -> Path:
        # Revalidate on every connection, including callbacks and expired sessions.
        identity = verify_credential(self.credential, self.signing_key)
        owner = hashlib.sha256(identity.encode("ascii")).hexdigest()
        if not hmac.compare_digest(owner, self.owner):
            raise VisitorAccessError("访客身份已变化，请刷新后重试。")
        root = self.root.expanduser().resolve()
        path = (root / owner / "quotes.sqlite3").resolve()
        if not path.is_relative_to(root):
            raise VisitorAccessError("访客存储目录配置无效。")
        return path


# ContextVar is execution-local, not a process-wide current visitor.
_access_scope: ContextVar[VisitorAccess | None] = ContextVar("dainan_visitor_access", default=None)


def setting(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is not None:
        import streamlit as st
        try:
            value = st.secrets.get(name, default)
        except FileNotFoundError:
            return default
        if not isinstance(value, str):
            raise VisitorAccessError(f"{name} 须配置为字符串。")
        return value
    return default


def storage_mode() -> str:
    mode = setting("DAINAN_STORAGE_MODE", "visitor")
    if mode not in ("visitor", "single_user"):
        raise VisitorAccessError("DAINAN_STORAGE_MODE 只能为 visitor 或 single_user。")
    return mode


def signing_key() -> str:
    key = setting("DAINAN_VISITOR_SIGNING_KEY")
    if len(key.encode("utf-8")) < 32:
        raise VisitorAccessError("请配置 DAINAN_VISITOR_SIGNING_KEY（至少 32 字节的随机密钥），私有记录尚未开放。")
    return key


def visitor_root() -> Path:
    return Path(setting("DAINAN_VISITOR_DATA_ROOT", str(Path(tempfile.gettempdir()) / "dainan-steelmate" / "visitors")))


def new_credential(key: str, *, now: int | None = None) -> str:
    if len(key.encode("utf-8")) < 32:
        raise VisitorAccessError("访客签名密钥尚未正确配置。")
    issued = int(time.time()) if now is None else now
    payload = f"v1.{secrets.token_hex(32)}.{issued}.{issued + COOKIE_DAYS * 86400}"
    signature = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_credential(credential: object, key: str, *, now: int | None = None) -> str:
    if len(key.encode("utf-8")) < 32 or not isinstance(credential, str):
        raise VisitorAccessError("访客凭据尚未就绪。")
    match = _TOKEN_PATTERN.fullmatch(credential)
    if match is None:
        raise VisitorAccessError("访客凭据无效或已过期。")
    identity, issued_text, expires_text, signature = match.groups()
    payload = credential.rsplit(".", 1)[0]
    expected = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    current = int(time.time()) if now is None else now
    issued, expires = int(issued_text), int(expires_text)
    if (not hmac.compare_digest(signature, expected) or issued > current + 60
            or expires - issued != COOKIE_DAYS * 86400 or current >= expires):
        raise VisitorAccessError("访客凭据无效或已过期。")
    return identity


def make_access(credential: str, key: str, root: Path, *, session_only: bool = False) -> VisitorAccess:
    identity = verify_credential(credential, key)
    return VisitorAccess(credential, key, Path(root), hashlib.sha256(identity.encode("ascii")).hexdigest(), session_only)


def cookie_observation(state) -> dict:
    value = state.get(COOKIE_KEY, {})
    observed = value.get("observation") if isinstance(value, dict) else None
    if not isinstance(observed, dict):
        return {}
    credential = observed.get("credential", "")
    if not isinstance(credential, str) or len(credential) > 512:
        return {"credential": "", "available": False}
    return {"credential": credential, "available": observed.get("available") is True}


def sync_session_owner(state, owner: str | None) -> bool:
    """Discard all business/widget/export state on a change of verified identity."""
    if state.get("_visitor_owner") == owner:
        return False
    if state.get("_visitor_owner") is not None:
        # A previous temporary identity must not return after adopting a Cookie.
        state.pop("_visitor_session_only", None)
    for name in list(state):
        if name not in _AUTH_KEYS:
            del state[name]
    state["_visitor_owner"] = owner
    return True


def session_access(state) -> VisitorAccess:
    key = signing_key()
    observed = cookie_observation(state)
    credential = observed.get("credential")
    try:
        access = make_access(credential, key, visitor_root())
        state.pop("_visitor_session_only", None)
        return access
    except VisitorAccessError:
        # Session-only use requires an explicit choice; it has its own random DB.
        temporary = state.get("_visitor_session_only")
        if temporary:
            return make_access(temporary, key, visitor_root(), session_only=True)
        raise


def current_access() -> VisitorAccess:
    if access := _access_scope.get():
        access.database_path()
        return access
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    if get_script_run_ctx(suppress_warning=True) is None:
        raise VisitorAccessError("当前没有已验证的访客上下文。")
    import streamlit as st
    access = session_access(st.session_state)
    if st.session_state.get("_visitor_owner") != access.owner:
        raise VisitorAccessError("访客身份已变化，私有操作已暂停。请刷新页面。")
    return access


@contextmanager
def visitor_scope(credential: str, key: str, root: Path):
    """Explicit backend scope for jobs/tests; never accepts a path from the UI."""
    access = make_access(credential, key, root)
    handle = _access_scope.set(access)
    try:
        yield access
    finally:
        _access_scope.reset(handle)


def private_database_path() -> Path:
    scoped = _access_scope.get()
    if scoped is not None or storage_mode() == "visitor":
        return current_access().database_path()
    # Legacy local workstation mode must be explicitly enabled.
    if override := setting("DAINAN_QUOTE_DB"):
        return Path(override).expanduser()
    if app_data := os.environ.get("LOCALAPPDATA"):
        return Path(app_data) / "DainanSteelAI" / "quotes.sqlite3"
    return Path.home() / ".dainan-steel-ai" / "quotes.sqlite3"
