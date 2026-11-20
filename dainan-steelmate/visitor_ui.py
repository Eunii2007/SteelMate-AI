"""Small Streamlit v2 bridge: real browser cookies, guarded callbacks and downloads."""

import base64
import hashlib
from functools import wraps
from html import escape

import streamlit as st

from visitor_context import (
    COOKIE_DAYS, COOKIE_KEY, COOKIE_NAME, VisitorAccessError, cookie_observation,
    current_access, make_access, new_credential, session_access, signing_key,
    storage_mode, sync_session_owner, visitor_root,
)


# V2 executes in the app document (not a separate component iframe).
# User content is passed as data, never interpolated into executable JS/HTML.
_cookie_bridge = st.components.v2.component(
    "dainan_visitor_cookie",
    js=r"""
    export default function({data, setStateValue}) {
      const read = () => {
        const prefix = data.name + '=';
        return document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith(prefix))?.slice(prefix.length) || '';
      };
      let last = data.observed || {}, attempted = false, writable = true;
      function report() {
        let credential = '', available = writable;
        try {
          credential = read();
          if (data.proposal && credential === data.expected && !attempted) {
            attempted = true;
            const secure = location.protocol === 'https:';
            // SameSite=None on HTTPS permits the Community Cloud app frame;
            // browser-level third-party blocking is handled by explicit fallback.
            document.cookie = `${data.name}=${data.proposal}; Path=/; Max-Age=${data.maxAge}; SameSite=${secure ? 'None; Secure' : 'Lax'}`;
            credential = read();
            writable = available = credential === data.proposal;
          }
        } catch (_) { available = false; }
        if (credential !== last.credential || available !== last.available) {
          last = {credential, available};
          setStateValue('observation', last);
        }
      }
      report();
      const timer = setInterval(report, 500);
      const focus = report;
      window.addEventListener('focus', focus);
      return () => { clearInterval(timer); window.removeEventListener('focus', focus); };
    }
    """,
)


def identity_changed() -> None:
    try:
        owner = session_access(st.session_state).owner
    except VisitorAccessError:
        owner = None
    sync_session_owner(st.session_state, owner)


def visitor_gate() -> None:
    """Run before any private render, history query or draft lookup."""
    try:
        if storage_mode() == "single_user":
            st.caption("本机单人模式 · 所有使用者共用本机记录，请勿用于公开部署。")
            return
        key = signing_key()
    except VisitorAccessError as error:
        sync_session_owner(st.session_state, None)
        st.error(str(error))
        st.stop()
    observed = cookie_observation(st.session_state)
    try:
        proposal = None
        try:
            make_access(observed.get("credential"), key, visitor_root())
        except VisitorAccessError:
            if observed and not st.session_state.get("_visitor_session_only"):
                pending = st.session_state.get("_visitor_pending", {})
                try:
                    make_access(pending.get("credential"), key, visitor_root())
                    same = pending.get("observed") == observed.get("credential", "")
                except VisitorAccessError:
                    same = False
                if not same:
                    pending = {"observed": observed.get("credential", ""), "credential": new_credential(key)}
                    st.session_state._visitor_pending = pending
                proposal = pending["credential"]
        _cookie_bridge(
            key=COOKIE_KEY, height=0,
            data={"name": COOKIE_NAME, "maxAge": COOKIE_DAYS * 86400, "observed": observed,
                  "proposal": proposal, "expected": observed.get("credential", "")},
            on_observation_change=identity_changed,
        )
        access = session_access(st.session_state)
    except VisitorAccessError:
        sync_session_owner(st.session_state, None)
        if not observed:
            st.info("正在建立匿名访客空间。若浏览器限制 Cookie，可选择仅本次会话。")
        else:
            st.info("正在验证浏览器 Cookie；私有记录尚未开放。Cookie 不可用时可继续使用独立的临时空间。")
        if st.button("仅本次会话使用", key="visitor-session-choice"):
            st.session_state._visitor_session_only = new_credential(key)
            st.rerun()
        st.stop()
    sync_session_owner(st.session_state, access.owner)
    st.session_state.pop("_visitor_pending", None)
    if access.session_only:
        st.warning("仅本次会话 · 记录独立保存。关闭、刷新或连接重建后可能无法找回，请及时下载报价单。")


def guarded_callback(callback):
    """Bind the render's owner: an old callback cannot act on a new visitor."""
    if storage_mode() == "single_user":
        return callback
    expected_owner = current_access().owner

    @wraps(callback)
    def guarded(*args, **kwargs):
        try:
            if current_access().owner != expected_owner:
                raise VisitorAccessError("访客身份已变化。")
        except VisitorAccessError:
            identity_changed()
            st.warning("访客身份已变化，本次旧页面操作已停止。请刷新后重试。")
            st.stop()
        return callback(*args, **kwargs)

    return guarded


_download = st.components.v2.component(
    "dainan_private_download",
    html='<button type="button"></button><small role="status"></small>',
    css="""
      button {width:100%;min-height:40px;border:1px solid #43453C;border-radius:6px;
        background:#292B27;color:#F6F5EE;font:inherit;cursor:pointer;padding:8px 12px;}
      button.primary {background:#D8FA91;color:#20211F;} small {display:block;color:#FFC388;}
    """,
    js=r"""
    export default function({data, parentElement, setTriggerValue}) {
      const button = parentElement.querySelector('button');
      button.textContent = data.label;
      button.className = data.primary ? 'primary' : '';
      button.onclick = async () => {
        const prefix = data.cookieName + '=';
        const credential = document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith(prefix))?.slice(prefix.length) || '';
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(credential));
        const proof = Array.from(new Uint8Array(digest)).map(n => n.toString(16).padStart(2,'0')).join('');
        if (proof !== data.cookieProof) {
          parentElement.querySelector('small').textContent = '访客身份已变化，请刷新后重新下载。';
          return;
        }
        const bytes = Uint8Array.from(atob(data.content), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type:data.mime}));
        const link = document.createElement('a');
        link.href = url; link.download = data.filename; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        setTriggerValue('clicked', true);
      };
    }
    """,
)


def private_download_button(container, label, data, file_name, mime, *, on_click="ignore", args=(), **kwargs):
    if storage_mode() == "single_user":
        return container.download_button(label, data, file_name, mime, on_click=on_click, args=args, **kwargs)
    current_access()
    content = data.encode("utf-8") if isinstance(data, str) else data
    credential = cookie_observation(st.session_state).get("credential", "")
    callback = guarded_callback(lambda: on_click(*args)) if callable(on_click) else guarded_callback(lambda: None)
    with container.container():
        # Blob download stays in this browser; no process-global /media URL.
        return _download(
            key=kwargs.get("key") or f"visitor-download-{label}", on_clicked_change=callback,
            data={"label": label, "content": base64.b64encode(content).decode("ascii"), "filename": file_name,
                  "mime": mime, "primary": kwargs.get("type") == "primary", "cookieName": COOKIE_NAME,
                  "cookieProof": hashlib.sha256(credential.encode("utf-8")).hexdigest()},
        )


def private_image(data: bytes, mime: str, *, caption: str, alt: str, width="stretch") -> None:
    if storage_mode() == "single_user":
        st.image(data, caption=caption, alt=alt, width=width)
        return
    current_access()
    encoded = base64.b64encode(data).decode("ascii")
    size = f"width:{width}px;" if isinstance(width, int) else "width:100%;"
    st.html(f'<figure style="margin:0;{size}max-width:100%"><img alt="{escape(alt, quote=True)}" '
            f'src="data:{escape(mime, quote=True)};base64,{encoded}" style="width:100%;border-radius:6px">'
            f'<figcaption>{escape(caption)}</figcaption></figure>')
