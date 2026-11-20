"""Opt-in local Edge acceptance. Uses synthetic records and an ephemeral signing key.

Run from app directory: python -B tests/browser_visitor_acceptance.py
Requires Playwright in the test environment, not in application requirements.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quote_store import get_quote, get_scrap_review, list_quotes, load_work_draft
from visitor_context import COOKIE_NAME, verify_credential, visitor_scope


def main():
    expect.set_options(timeout=15000)
    output = ROOT / ".runtime" / ("visitor-acceptance-" + time.strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True)
    signing_key = secrets.token_urlsafe(48)
    data_root = output / "visitors"
    excluded = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "VISION_MODEL", "DAINAN_VISITOR_SIGNING_KEY"}
    environment = {name: os.environ[name] for name in os.environ if name not in excluded}
    environment.update({"DAINAN_STORAGE_MODE": "visitor", "DAINAN_VISITOR_SIGNING_KEY": signing_key,
                        "DAINAN_VISITOR_DATA_ROOT": str(data_root), "OPENAI_API_KEY": "",
                        "OPENAI_BASE_URL": "", "VISION_MODEL": "", "PYTHONUTF8": "1"})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    checks = []
    completed = False

    def passed(label):
        checks.append(label)
        print("PASS: " + label, flush=True)

    def start(log):
        server = subprocess.Popen(
            [sys.executable, "-B", "-m", "streamlit", "run", "app.py", "--server.address=127.0.0.1",
             f"--server.port={port}", "--server.headless=true", "--server.fileWatcherType=none",
             "--browser.gatherUsageStats=false"], cwd=ROOT, env=environment, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(100):
            try:
                with urlopen(base + "/_stcore/health", timeout=1) as response:
                    if response.status == 200:
                        return server
            except OSError:
                time.sleep(.2)
        server.terminate()
        server.wait(timeout=10)
        raise RuntimeError("Local acceptance server did not start")

    def goto(page, suffix="?view=workbench"):
        page.goto(base + "/" + suffix)
        page.wait_for_load_state("domcontentloaded")

    def click(page, name):
        page.wait_for_timeout(200)
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
        if name in ("316L 法兰（TP-001）", "304 垫板（TP-002）"):
            if not page.get_by_role("button", name=name, exact=True).is_visible():
                page.get_by_text("使用样例", exact=True).click()
        page.get_by_role("button", name=name, exact=True).click()
        page.wait_for_timeout(200)
        expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")

    def credential(context):
        return next(cookie["value"] for cookie in context.cookies() if cookie["name"] == COOKIE_NAME)

    with (output / "server.log").open("w", encoding="utf-8") as log, sync_playwright() as playwright:
        server = start(log)
        regular = None
        browser = None
        try:
            regular = playwright.chromium.launch_persistent_context(
                str(output / "regular-profile"), channel="msedge", headless=True, viewport={"width": 1600, "height": 980},
            )
            a = regular.pages[0]
            a.set_default_timeout(20000)
            goto(a)
            expect(a.get_by_role("heading", name="报价工作台", exact=True)).to_be_visible()
            a_credential = credential(regular)
            verify_credential(a_credential, signing_key)
            assert COOKIE_NAME not in a.url and a_credential not in a.content()
            passed("regular profile received a signed Cookie without a URL credential")
            a.get_by_text("使用样例", exact=True).click()
            click(a, "316L 法兰（TP-001）")
            expect(a.locator(".quote-card").first).to_contain_text("4,729.79")
            a.get_by_role("textbox", name="客户简称", exact=True).fill("隔离验收 A")
            a.get_by_role("textbox", name="客户简称", exact=True).press("Tab")
            click(a, "保存询盘")
            expect(a.get_by_role("link", name="Q-000001", exact=True)).to_be_visible()
            a.screenshot(path=str(output / "A-workbench.png"), full_page=True)
            click(a, "加入本单")
            click(a, "304 垫板（TP-002）")
            click(a, "加入本单")
            click(a, "新询盘")
            click(a, "手动录入")
            a.get_by_role("textbox", name="材质牌号", exact=True).fill("2205")
            a.get_by_role("textbox", name="材质牌号", exact=True).press("Tab")
            a.wait_for_timeout(300)
            expect(a.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
            with visitor_scope(a_credential, signing_key, data_root):
                draft = load_work_draft()
                assert len(draft["payload"]["order"]["items"]) == 2
                assert draft["payload"]["inquiry"]["parameters"]["material"] == "2205"
            passed("A saved a quote plus an independent pending draft and two confirmed order lines")

            browser = playwright.chromium.launch(channel="msedge", headless=True)
            private = browser.new_context(viewport={"width": 1440, "height": 980}, accept_downloads=True)
            b = private.new_page()
            b.set_default_timeout(20000)
            goto(b, "?view=records")
            expect(b.get_by_role("textbox", name="查找询盘")).to_be_visible()
            expect(b.get_by_text("暂无询盘记录。", exact=False)).to_be_visible()
            b_credential = credential(private)
            assert b_credential != a_credential
            b.screenshot(path=str(output / "B-private-empty.png"), full_page=True)
            goto(b, "?view=records&quote=1")
            expect(b.get_by_text("找不到这条报价，请返回询盘记录查找。", exact=True)).to_be_visible()
            expect(b.get_by_role("button", name="下载客户报价单", exact=True)).to_have_count(0)
            assert "隔离验收 A" not in b.content()
            goto(b, "?view=records&remove=1")
            expect(b.get_by_role("button", name="确认删除", exact=True)).to_have_count(0)
            passed("incognito B cannot see, export, copy or delete A using its numeric detail link")
            goto(b)
            expect(b.get_by_role("heading", name="报价工作台", exact=True)).to_be_visible()
            expect(b.get_by_role("button", name="继续上次询盘", exact=True)).to_have_count(0)
            b.get_by_text("使用样例", exact=True).click()
            click(b, "304 垫板（TP-002）")
            click(b, "保存询盘")
            expect(b.get_by_role("link", name="Q-000001", exact=True)).to_be_visible()
            with visitor_scope(b_credential, signing_key, data_root):
                assert get_quote(1)["snapshot"]["filename"] == "TP-002.png"
                assert get_scrap_review(1, 1) is None
            passed("A and B both have ID 1 but it resolves to each visitor's own quote")

            blocked = browser.new_context(viewport={"width": 1280, "height": 900})
            blocked.add_init_script("""
                const cookie = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
                Object.defineProperty(document, 'cookie', {
                  get() { return cookie.get.call(document).split(';').filter(s => !s.trim().startsWith('dainan_visitor_v1=')).join(';'); },
                  set(value) { if (!value.startsWith('dainan_visitor_v1=')) cookie.set.call(document, value); }
                });
            """)
            temporary = blocked.new_page()
            goto(temporary)
            expect(temporary.get_by_role("button", name="仅本次会话使用", exact=True)).to_be_visible()
            click(temporary, "仅本次会话使用")
            expect(temporary.get_by_text("仅本次会话 · 记录独立保存。", exact=False)).to_be_visible()
            click(temporary, "304 垫板（TP-002）")
            click(temporary, "保存询盘")
            expect(temporary.get_by_role("link", name="Q-000001", exact=True)).to_be_visible()
            assert not any(cookie["name"] == COOKIE_NAME for cookie in blocked.cookies())
            temporary.screenshot(path=str(output / "blocked-cookie-session.png"), full_page=True)
            passed("blocked visitor Cookie requires explicit session-only use and saves in a separate space")

            # Fully restart the server and reopen the normal browser profile.
            regular.close()
            regular = None
            server.terminate()
            server.wait(timeout=15)
            server = start(log)
            regular = playwright.chromium.launch_persistent_context(
                str(output / "regular-profile"), channel="msedge", headless=True, viewport={"width": 1600, "height": 980},
            )
            a = regular.pages[0]
            a.set_default_timeout(20000)
            goto(a)
            expect(a.get_by_role("button", name="继续上次询盘", exact=True)).to_be_visible()
            assert credential(regular) == a_credential
            click(a, "继续上次询盘")
            expect(a.get_by_role("textbox", name="材质牌号", exact=True)).to_have_value("2205")
            expect(a.get_by_role("button", name="保存询盘", exact=True)).to_have_count(0)
            passed("server and browser restart recover A's draft, order and Cookie without a premature quote")
            goto(a, "?view=records&quote=1")
            expect(a.get_by_text("隔离验收 A", exact=True)).to_be_visible()
            with a.expect_download() as download:
                click(a, "下载客户报价单")
            download.value.save_as(output / "A-client-quote.html")
            customer_html = (output / "A-client-quote.html").read_text(encoding="utf-8")
            assert "4,729.79" in customer_html and "5,102.40" in customer_html and "隔离验收 A" in customer_html
            a.get_by_text("内部核价明细", exact=True).click()
            with a.expect_download() as download:
                click(a, "下载 CSV")
            download.value.save_as(output / "A-internal-quote.csv")
            a.get_by_text("确认可供", exact=True).click()
            a.get_by_role("spinbutton", name="本单确认可用件数（件）", exact=True).fill("14")
            a.get_by_role("textbox", name="复核备注（选填）", exact=True).fill("隔离验收：模拟回填")
            click(a, "登记复核结果")
            expect(a.get_by_text("复核结果已登记。", exact=True)).to_be_visible()
            with visitor_scope(a_credential, signing_key, data_root):
                assert get_scrap_review(1, 1)["confirmed_quantity"] == 14
            with visitor_scope(b_credential, signing_key, data_root):
                assert get_scrap_review(1, 1) is None
            a.screenshot(path=str(output / "A-history-review.png"), full_page=True)
            a.get_by_test_id("stMain").evaluate("element => element.scrollTo(0, 0)")
            a.screenshot(path=str(output / "A-history-top.png"), full_page=True)
            passed("A's Blob HTML/CSV downloads and simulated review remain private after restart")

            private.add_cookies([{"name": COOKIE_NAME, "value": "tampered", "url": base}])
            goto(b, "?view=records&quote=1")
            expect(b.get_by_text("找不到这条报价，请返回询盘记录查找。", exact=True)).to_be_visible()
            replacement = credential(private)
            assert replacement != b_credential
            verify_credential(replacement, signing_key)
            with visitor_scope(b_credential, signing_key, data_root):
                assert get_quote(1)["snapshot"]["filename"] == "TP-002.png"
            passed("tampered Cookie receives a fresh signed identity without inheriting or deleting B's records")

            regular.clear_cookies()
            # Bridge observes identity loss without requiring the user to reload.
            expect(a.get_by_text("找不到这条报价，请返回询盘记录查找。", exact=True)).to_be_visible()
            assert credential(regular) != a_credential
            assert "隔离验收 A" not in a.content()
            expect(a.get_by_role("button", name="下载客户报价单", exact=True)).to_have_count(0)
            with visitor_scope(a_credential, signing_key, data_root):
                assert len(list_quotes()) == 1 and load_work_draft() is not None
            a.screenshot(path=str(output / "A-after-cookie-clear.png"), full_page=True)
            passed("Cookie clearing removes the old UI/exports but preserves the old database")
            completed = True
        except Exception:
            if regular is not None:
                for index, page in enumerate(regular.pages):
                    page.screenshot(path=str(output / f"failure-regular-{index}.png"), full_page=True)
            raise
        finally:
            if regular is not None:
                regular.close()
            if browser is not None:
                browser.close()
            server.terminate()
            server.wait(timeout=15)
            (output / "results.json").write_text(json.dumps({"completed": completed, "passed": checks,
                "scope": "local headless Edge: regular persistent profile + independent incognito context",
                "community_cloud": "not tested; no deployment performed"}, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Evidence: " + str(output), flush=True)


if __name__ == "__main__":
    main()
