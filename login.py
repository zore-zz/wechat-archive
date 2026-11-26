# -*- coding: utf-8 -*-
"""扫码登录：用 Playwright 持久化 profile 打开后台，取 token / Cookie / fakeid。

设计要点：
- headless 必须为 False：登录页对无头环境有检测，且可能弹安全验证需人工处置
- 判定登录成功用 page.url 匹配 cgi-bin/home?...token=xxx，比轮询 DOM 可靠
- Cookie 只取 name->value，供 requests 使用
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

TOKEN_RE = re.compile(r"[?&]token=(\d+)")
HOME_OK = re.compile(r"cgi-bin/home\?.*token=\d+")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class AuthError(RuntimeError):
    pass


def load_auth(cfg) -> dict | None:
    p = Path(cfg["paths"]["auth"])
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_auth(cfg, data: dict) -> None:
    p = Path(cfg["paths"]["auth"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _launch(p, profile_dir: Path):
    last = None
    for channel in ("msedge", "chrome", None):
        try:
            kw = dict(
                user_data_dir=str(profile_dir),
                headless=False,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                ],
            )
            if channel:
                kw["channel"] = channel
            return p.chromium.launch_persistent_context(**kw)
        except Exception as exc:  # 依次尝试系统 Edge / Chrome / 自带 Chromium
            last = exc
    raise AuthError(
        "无法启动浏览器。请先执行 `python -m playwright install chromium`，"
        f"或确认本机装有 Edge/Chrome。原始错误：{last}"
    )


def browser_login(cfg, force: bool = False, timeout_s: int = 300) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthError("未安装 playwright，请先执行 pip install playwright") from exc

    profile_dir = Path(cfg["paths"]["profile"]).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = _launch(p, profile_dir)
        try:
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded")

            if not force:
                try:
                    page.wait_for_url(HOME_OK, timeout=8000)
                except Exception:
                    pass

            if not HOME_OK.search(page.url):
                print("\n>>> 请在刚打开的浏览器窗口里扫码登录（最长等待 %d 秒）..." % timeout_s)
                print(">>> 若出现滑块或短信验证，请在该窗口内完成，脚本会继续等待。\n")
                page.wait_for_url(HOME_OK, timeout=timeout_s * 1000)

            m = TOKEN_RE.search(page.url)
            if not m:
                raise AuthError("已进入后台但没能取到 token，请重试")
            token = m.group(1)

            page.goto(
                f"https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token={token}",
                wait_until="domcontentloaded",
            )
            time.sleep(1.5)

            cookies = {}
            for c in ctx.cookies(["https://mp.weixin.qq.com"]):
                cookies[c["name"]] = c["value"]

            nickname = ""
            try:
                nickname = page.evaluate(
                    "() => { const e=document.querySelector('.weui-desktop-account__nickname');"
                    " return e ? e.textContent.trim() : '' }"
                ) or ""
            except Exception:
                pass
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    return {
        "token": token,
        "cookies": cookies,
        "ua": DEFAULT_UA,
        "nickname": nickname,
        "fakeid": cfg.get("account", {}).get("fakeid") or "",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def search_fakeid(auth: dict, nickname: str, timeout: int = 20) -> str:
    import requests

    if not nickname:
        return ""
    url = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
    params = {
        "action": "search_biz",
        "begin": 0,
        "count": 5,
        "query": nickname,
        "token": auth["token"],
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
    }
    headers = {
        "User-Agent": auth["ua"],
        "Referer": f"https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token={auth['token']}",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie_header(auth["cookies"]),
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        j = r.json()
    except Exception:
        return ""
    items = j.get("list") or []
    for it in items:
        if (it.get("nickname") or "").strip() == nickname.strip():
            return it.get("fakeid") or ""
    return items[0].get("fakeid", "") if items else ""


def ensure_auth(cfg, force: bool = False, quiet: bool = False) -> dict:
    """返回可用登录态。优先复用 auth.json，失效才开浏览器。"""
    auth = load_auth(cfg)
    if auth and not force:
        from fetch import api_probe  # 延迟导入，避免循环依赖

        ok, ret = api_probe(auth, cfg)
        if ok:
            if not quiet:
                print(f"[auth] 复用已有登录态（token={auth['token']}）")
            return auth
        if not quiet:
            print(f"[auth] 登录态已失效（ret={ret}），需要重新登录")

    if not quiet:
        print("[auth] 正在打开浏览器获取登录态...")
    auth = browser_login(cfg, force=force)

    fakeid = cfg.get("account", {}).get("fakeid") or ""
    # 抓自己的号不需要 fakeid（带上反而会被频控），默认跳过这次查询以省一次请求
    if not fakeid and cfg.get("account", {}).get("use_fakeid"):
        fakeid = search_fakeid(auth, cfg.get("account", {}).get("nickname", ""))
        if fakeid:
            if not quiet:
                print(f"[auth] 已识别公众号 fakeid：{fakeid}")
        elif not quiet:
            print("[auth] 未能自动识别 fakeid，将按当前登录号抓取（通常可用）")
    auth["fakeid"] = fakeid
    save_auth(cfg, auth)
    return auth
