# -*- coding: utf-8 -*-
"""列表抓取与正文抓取。只负责"取回并落库"，不做分类。

正文不走 mp.weixin.qq.com/s 文章页：该页面会对本机 IP 弹环境验证（wappoc_appmsgcaptcha），
连真实浏览器都拦。改用后台自己的图文编辑接口 cgi-bin/appmsg?action=edit，
它返回的 app_msg_info 里带完整 content HTML，同域同会话，不会被验证码拦。
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from datetime import datetime

import requests

import db
import login as login_mod

LIST_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"
AUTH_RETS = {200002, 200003, -6}
# 图文已删除 / 不存在
GONE_RETS = {64501, 200009, 200008}


class RateLimited(RuntimeError):
    pass


class FetchError(RuntimeError):
    pass


class ArticleGone(FetchError):
    """接口明确表示该图文已删除/不存在。和网络异常要严格区分：
    网络异常只算「这次没抓到」，下次还会重试；已删才落终态。"""
    pass


# ---------------- 通用 ----------------

def to_ts(date_str: str | None) -> int:
    if not date_str:
        return 0
    try:
        return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    except ValueError:
        return 0


def build_headers(auth: dict) -> dict:
    return {
        "User-Agent": auth["ua"],
        "Referer": (
            "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit"
            f"&action=edit&type=10&isNew=1&isMul=1&token={auth['token']}&lang=zh_CN"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Cookie": login_mod.cookie_header(auth["cookies"]),
    }


def new_session(auth: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update(build_headers(auth))
    return s


def _sleep(rng, prefix: str = "") -> None:
    sec = random.uniform(float(rng[0]), float(rng[1]))
    if prefix:
        print(f"       {prefix} {sec:.0f}s ...")
    time.sleep(sec)


def _json(resp: requests.Response) -> dict:
    resp.encoding = "utf-8"
    try:
        return resp.json()
    except Exception as exc:
        raise FetchError(f"接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:160]}") from exc


def _check_ret(ret: int, err: str = "") -> None:
    if ret == 200013:
        raise RateLimited("接口频控（200013 freq control）")
    if ret in AUTH_RETS:
        raise login_mod.AuthError(f"登录态失效（ret={ret}），请重新扫码")
    if ret != 0:
        raise FetchError(f"接口返回 ret={ret} {err}")


def request_page(s, auth, cfg, begin: int, count: int, fakeid: str = "") -> dict:
    params = {
        "action": "list_ex",
        "begin": begin,
        "count": count,
        "type": 9,
        "query": "",
        "token": auth["token"],
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
    }
    # 抓自己号的历史文章不能带 fakeid：带上会直接返回 200013 freq control。
    # 只有以后要抓「别的号」时，才把 use_fakeid 设为 true。
    if fakeid and cfg.get("account", {}).get("use_fakeid"):
        params["fakeid"] = fakeid
    r = s.get(LIST_URL, params=params, timeout=cfg["fetch"]["timeout"])
    return _json(r)


def api_probe(auth: dict, cfg) -> tuple[bool, int]:
    s = new_session(auth)
    try:
        j = request_page(s, auth, cfg, 0, 1, auth.get("fakeid") or "")
    except Exception:
        return False, -99
    ret = (j.get("base_resp") or {}).get("ret", -99)
    return ret == 0, int(ret)


def _ext(link: str, key: str) -> str:
    m = re.search(rf"[?&]{key}=([^&#]+)", link or "")
    return m.group(1) if m else ""


CHANNEL_RE = re.compile(r"^(?P<title>.{6,})\s*[|｜]\s*(?P<ch>[\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*$")


def split_channel(raw: str) -> tuple[str, str]:
    """很多号的标题写成「正文标题 | 栏目名」，把栏目拆出来单独存。

    栏目是比关键词可靠得多的分类信号，拆出来也能避免「荣誉」二字在
    标题里被重复计分。拆不出就返回 (原标题, "")。
    """
    m = CHANNEL_RE.match(raw or "")
    if not m:
        return (raw or "").strip(), ""
    return m.group("title").strip(), m.group("ch").strip()


def normalize_item(it: dict, account: str = "") -> dict:
    link = it.get("link") or ""
    mid = it.get("appmsgid") or it.get("app_id") or it.get("aid") or ""
    idx = it.get("itemidx") or it.get("item_idx") or it.get("idx") or 1
    ts = int(it.get("create_time") or 0)
    title, channel = split_channel(it.get("title") or "")
    return {
        "biz": _ext(link, "__biz") or it.get("biz") or "",
        "mid": str(mid),
        "idx": int(idx),
        "sn": _ext(link, "sn"),
        "title": title,
        "channel": channel,
        "digest": (it.get("digest") or "").strip(),
        "author": it.get("author") or "",
        "account": account,
        "publish_ts": ts,
        "publish_date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
        "link": link,
        "cover": it.get("cover") or "",
        "group_key": f"{_ext(link, '__biz')}:{mid}",
    }


# ---------------- 列表抓取 ----------------

def run_list(cfg, conn, mode: str = "new", n: int = 10,
             before_date: str | None = None) -> dict:
    """mode: pilot | full | new | backfill"""
    auth = login_mod.ensure_auth(cfg)
    s = new_session(auth)
    f = cfg["fetch"]
    count = int(f["page_size"])
    account = cfg.get("account", {}).get("nickname", "")

    since_ts = to_ts(cfg.get("window", {}).get("since"))
    before_ts = to_ts(before_date)

    if mode == "pilot":
        # 多留几页余量：库中已有的文章不计入 new，只按 new 判断会提前退出
        max_pages = math.ceil(n / count) + 3
        stop_pages = 0
    else:
        max_pages = int(f["max_pages_per_run"])
        stop_pages = int(f.get("new_stop_pages", 2)) if mode == "new" else 0

    # backfill 用独立断点行（end_ts 作为任务键），避免和日常增量互相覆盖 offset
    task = db.ensure_task(conn, auth.get("fakeid") or "", 0,
                          before_ts if mode == "backfill" else 0)
    begin = int(task["next_offset"] or 0)
    if mode in ("full", "pilot", "new"):
        begin = 0  # 这几种模式都从最新开始

    pages = 0
    new_total = 0
    zero_pages = 0
    reached_edge = False
    run_id = db.start_run(conn, f"list:{mode}", f"n={n} before={before_date}")

    print(f"[list] 模式={mode} 起始 offset={begin} 单次上限={max_pages} 页")
    try:
        while pages < max_pages:
            j = request_page(s, auth, cfg, begin, count, auth.get("fakeid") or "")
            ret = int((j.get("base_resp") or {}).get("ret", -99))
            if ret == 200013:
                print("[list] 触发频控（200013），长休后重试该页...")
                _sleep(f.get("freq_backoff_seconds", [120, 200]), "频控退避")
                j = request_page(s, auth, cfg, begin, count, auth.get("fakeid") or "")
                ret = int((j.get("base_resp") or {}).get("ret", -99))
            if ret == 200013:
                raise RateLimited(
                    "列表接口频控（200013 freq control）。断点已保存，"
                    f"建议 1 小时后再跑，脚本会从第 {begin} 篇继续"
                )
            _check_ret(ret, (j.get("base_resp") or {}).get("err_msg", ""))

            items = j.get("app_msg_list") or []
            if not items:
                print("[list] 已到末页（返回空列表）")
                reached_edge = True
                break

            page_new = 0
            for it in items:
                rec = normalize_item(it, account)
                if not rec["mid"] or not rec["link"]:
                    continue
                ts = rec["publish_ts"]

                if mode == "full" and since_ts and ts and ts < since_ts:
                    reached_edge = True
                    continue
                if mode == "backfill" and before_ts and ts and ts < before_ts:
                    # 翻到目标日期之前，本轮够了；该篇仍会入库
                    reached_edge = True

                if db.upsert_article(conn, rec):
                    page_new += 1

            conn.commit()
            db.refresh_item_cnt(conn)
            conn.commit()

            pages += 1
            new_total += page_new
            # begin 是「第几次群发」的序号，不是条目数：一次群发含 2 篇时会多返回 1 条。
            # 若按 len(items) 推进，每页会多跳过 (条目数-群发数) 个群发 → 静默漏稿。
            begin += count
            db.update_task(conn, task["id"], next_offset=begin,
                           pages_done=int(task["pages_done"] or 0) + pages,
                           new_items=int(task["new_items"] or 0) + page_new,
                           status="running", last_ret=0, last_err="")
            conn.commit()

            print(f"[list] 第 {pages} 页：{len(items)} 条，新增 {page_new} 条"
                  f"（累计新增 {new_total}）")

            if mode == "pilot" and (db.stats(conn).get("total") or 0) >= n:
                break
            if reached_edge:
                break
            if mode == "new":
                zero_pages = zero_pages + 1 if page_new == 0 else 0
                if zero_pages >= stop_pages:
                    print(f"[list] 连续 {stop_pages} 页无新增，判定已追上最新")
                    break
            if pages < max_pages:
                if pages % int(f["long_rest_every"]) == 0:
                    _sleep(f["long_rest_seconds"], "长休")
                else:
                    _sleep(f["page_interval"], "翻页间隔")

        db.update_task(conn, task["id"], status="done")
        conn.commit()
        db.finish_run(conn, run_id, f"pages={pages} new={new_total}")
        return {"pages": pages, "new": new_total}
    except RateLimited as exc:
        db.update_task(conn, task["id"], status="aborted", last_ret=200013, last_err=str(exc))
        db.finish_run(conn, run_id, f"pages={pages} new={new_total}", str(exc))
        conn.commit()
        raise
    except Exception as exc:
        db.update_task(conn, task["id"], status="aborted", last_err=str(exc))
        db.finish_run(conn, run_id, f"pages={pages} new={new_total}", str(exc))
        conn.commit()
        raise


# ---------------- 正文抓取（走后台编辑接口） ----------------

def html_to_text(fragment: str) -> str:
    """把 content HTML 转成纯文本，图片位置留【图片】占位。"""
    from lxml import html as LH

    if not (fragment or "").strip():
        return ""
    try:
        doc = LH.fromstring(fragment)
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()

    for bad in doc.xpath(".//script|.//style|.//svg|.//iframe|.//noscript"):
        bad.drop_tree()
    imgs = doc.xpath(".//img")
    for img in imgs:
        img.tail = "【图片】" + (img.tail or "")
        img.drop_tag()

    blocks = doc.xpath(
        ".//p|.//li|.//blockquote|.//h1|.//h2|.//h3"
        "|.//section[not(.//section) and not(.//p)]"
    )
    texts: list[str] = []
    for el in blocks:
        t = re.sub(r"[\s\u00a0\u3000]+", " ", el.text_content()).strip()
        if not t or t == "【图片】":
            continue
        if texts and texts[-1] == t:
            continue
        texts.append(t)
    if not texts:  # div 手写排版的正文
        t = re.sub(r"[\s\u00a0\u3000]+", " ", doc.text_content()).strip()
        if t:
            texts.append(t)
    return "\n".join(texts)


def fetch_group(s, auth, cfg, mid: str) -> tuple[list[dict], str]:
    """取一次群发的全部图文。返回 (items, err_msg)。"""
    params = {
        "action": "edit",
        "type": 10,
        "appmsgid": mid,
        "token": auth["token"],
        "lang": "zh_CN",
        "f": "json",
        "ajax": 1,
    }
    r = s.get(LIST_URL, params=params, timeout=cfg["fetch"]["timeout"])
    j = _json(r)
    br = j.get("base_resp") or {}
    ret = int(br.get("ret", -99))
    if ret in GONE_RETS:
        raise ArticleGone(br.get("err_msg", "") or f"ret={ret} 图文已删除")
    if ret == 200013:
        raise RateLimited("正文接口频控（200013）")
    if ret in AUTH_RETS:
        raise login_mod.AuthError(f"登录态失效（ret={ret}），请重新扫码")
    if ret != 0:
        raise FetchError(f"正文接口 ret={ret} {br.get('err_msg','')}")

    info = j.get("app_msg_info")
    if isinstance(info, str):
        try:
            info = json.loads(info or "{}")
        except Exception:
            info = {}
    return (info or {}).get("item") or [], ""


def run_body(cfg, conn, limit: int | None = None) -> dict:
    auth = login_mod.ensure_auth(cfg)
    s = new_session(auth)
    f = cfg["fetch"]
    limit = int(limit or f["max_body_per_run"])

    today = db.count_body_today(conn)
    quota = max(0, int(f["max_body_per_day"]) - today)
    if quota <= 0:
        print(f"[body] 今日已抓 {today} 篇，达到单日软上限 {f['max_body_per_day']}，明天再跑")
        return {"ok": 0, "skipped_quota": True}
    limit = min(limit, quota)

    rows = db.pending_body(conn, limit)
    if not rows:
        print("[body] 没有待抓正文的文章")
        return {"ok": 0, "total": 0}

    # 同一次群发（同 mid）只发一次请求，能拿回该次群发的所有条目
    groups: dict[str, list] = {}
    for art in rows:
        groups.setdefault(str(art["mid"]), []).append(art)

    print(f"[body] 待抓 {len(rows)} 篇，合并为 {len(groups)} 次请求（今日剩余额度 {quota}）")
    run_id = db.start_run(conn, "body", f"limit={limit} groups={len(groups)}")
    ok = deleted = failed = skipped = 0
    env_hits = 0
    done = 0

    try:
        for gi, (mid, arts) in enumerate(groups.items(), 1):
            # 节日祝福已在初判阶段确定，不抓正文
            todo = []
            for art in arts:
                if db.get_primary_category(conn, art["id"]) == "exclude_festival":
                    db.mark_skip(conn, art["id"], "节日祝福，无需正文")
                    skipped += 1
                else:
                    todo.append(art)
            if not todo:
                conn.commit()
                continue

            gone = False
            try:
                items, err = fetch_group(s, auth, cfg, mid)
            except ArticleGone as exc:
                items, err, gone = [], str(exc), True
            except RateLimited:
                db.finish_run(conn, run_id, f"ok={ok} deleted={deleted} failed={failed}", "200013")
                raise
            except login_mod.AuthError:
                db.finish_run(conn, run_id, f"ok={ok} deleted={deleted} failed={failed}", "auth")
                raise
            except Exception as exc:
                # 网络/解析异常绝不当成「已删」，否则会永久漏抓
                items, err = [], str(exc)

            # 一次群发多篇时，接口只返回首篇 item，其余篇在它的 multi_item 数组里；
            # 单篇群发的 multi_item 也只有一条，所以统一按 multi_item 展开。
            # 注意：循环变量别用 s，会遮蔽外层的 session s，导致后续请求全炸。
            flat: dict[int, dict] = {}
            for it in items:
                subs = it.get("multi_item") or []
                if subs:
                    for sub in subs:
                        flat[int(sub.get("seq") or 0) + 1] = sub
                else:
                    flat.setdefault(int(it.get("seq") or 0) + 1, it)

            for art in todo:
                done += 1
                it = flat.get(int(art["idx"]))
                if it is None and len(flat) == 1:
                    it = next(iter(flat.values()))
                if it is None:
                    msg = err or "接口未返回该条目"
                    db.save_body(conn, art["id"], 2 if gone else 4, None, msg)
                    if gone:
                        deleted += 1
                    else:
                        failed += 1
                    print(f"[body] {done}/{len(rows)} 取不到 {art['title'][:24]} {msg[:30]}")
                    continue

                text = html_to_text(it.get("content") or "")
                if text:
                    ts = int(it.get("create_time") or 0) or None
                    db.save_body(conn, art["id"], 1, text, "ok",
                                 (it.get("content") or "").count("<img"), ts)
                    ok += 1
                    print(f"[body] {done}/{len(rows)} ok  {art['title'][:26]}  {len(text)} 字")
                else:
                    db.save_body(conn, art["id"], 4, None, "内容为空")
                    failed += 1
                    print(f"[body] {done}/{len(rows)} 空  {art['title'][:26]}")
            conn.commit()

            if gi < len(groups):
                if gi % int(f["body_batch"]) == 0:
                    _sleep(f["body_batch_rest"], "正文长休")
                else:
                    _sleep(f["body_interval"], "正文间隔")

        db.finish_run(conn, run_id,
                      f"ok={ok} deleted={deleted} failed={failed} skipped={skipped}")
        return {"ok": ok, "deleted": deleted, "failed": failed,
                "skipped": skipped, "total": len(rows)}
    except Exception as exc:
        db.finish_run(conn, run_id, f"ok={ok} deleted={deleted} failed={failed}", str(exc))
        raise


# ---------------- 对账补漏 ----------------

def run_verify(cfg, conn, since: str | None = None, max_pages: int | None = None) -> dict:
    """把后台列表从头扫到底，逐条和本地库比对，补进缺的条目。

    为什么需要：早期翻页用 `begin += len(items)`，而 begin 是群发序号、
    一页可能返回比 count 多的条目（群发含多篇），于是每页多跳 1 个群发，
    个别文章静默丢失。这里做一次全量对账，只补不删。
    """
    auth = login_mod.ensure_auth(cfg)
    s = new_session(auth)
    f = cfg["fetch"]
    count = int(f["page_size"])
    account = cfg.get("account", {}).get("nickname", "")
    since_ts = to_ts(since or cfg.get("window", {}).get("since"))
    limit_pages = int(max_pages or f.get("verify_max_pages", 40))

    begin = 0
    pages = 0
    seen = 0
    added: list[dict] = []
    run_id = db.start_run(conn, "verify", f"since={since or '-'}")

    print(f"[verify] 从 offset=0 全量对账，起始日期下限="
          f"{since or (cfg.get('window') or {}).get('since') or '不限'}，单次上限 {limit_pages} 页")
    try:
        while pages < limit_pages:
            j = request_page(s, auth, cfg, begin, count, auth.get("fakeid") or "")
            ret = int((j.get("base_resp") or {}).get("ret", -99))
            if ret == 200013:
                print("[verify] 触发频控，长休后重试该页...")
                _sleep(f.get("freq_backoff_seconds", [120, 200]), "频控退避")
                j = request_page(s, auth, cfg, begin, count, auth.get("fakeid") or "")
                ret = int((j.get("base_resp") or {}).get("ret", -99))
                if ret == 200013:
                    raise RateLimited("列表接口频控（200013），补漏进度未存断点，请 1 小时后再跑")
            _check_ret(ret, (j.get("base_resp") or {}).get("err_msg", ""))

            items = j.get("app_msg_list") or []
            if not items:
                print("[verify] 已到末页")
                break

            reached = False
            for it in items:
                rec = normalize_item(it, account)
                if not rec["mid"] or not rec["link"]:
                    continue
                seen += 1
                if since_ts and rec["publish_ts"] and rec["publish_ts"] < since_ts:
                    reached = True
                    continue
                if not db.article_exists(conn, rec["biz"], rec["mid"], rec["idx"]):
                    db.upsert_article(conn, rec)
                    added.append({k: rec[k] for k in
                                  ("publish_date", "title", "mid", "idx", "channel")})
            conn.commit()

            pages += 1
            begin += count  # begin 是群发序号，按请求的群发数推进（不按返回条目数）
            print(f"[verify] 第 {pages} 页：{len(items)} 条，累计比对 {seen}，补进 {len(added)}")

            if reached and since_ts:
                print("[verify] 已翻到起始日期之前，对账结束")
                break

            if pages < limit_pages:
                _sleep(f["page_interval"], "翻页间隔")

        db.finish_run(conn, run_id, f"pages={pages} seen={seen} added={len(added)}")
        conn.commit()
        return {"pages": pages, "seen": seen, "added": added}
    except Exception as exc:
        db.finish_run(conn, run_id, f"pages={pages} seen={seen} added={len(added)}", str(exc))
        conn.commit()
        raise
