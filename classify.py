# -*- coding: utf-8 -*-
"""规则打分与判定。只读 articles、只写 classifications，可反复重算。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

import db

VERDICT_NAMES = {
    "exclude_pro": "专业文章",
    "exclude_festival": "节日祝福",
    "exclude_routine": "常规讲座",
    "exclude_personal_welfare": "个人公益",
    "exclude_duty": "个人履职（人大/政协）",
    "exclude_recruit": "招聘",
    "exclude_promo": "业务宣传",
    "exclude_notice": "预告通知",
    "exclude_internal": "内部工作会",
    "exclude_annual_case": "年度案例",
    "undetermined": "未达阈值",
}


def load_rules(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_overrides(cfg) -> dict:
    """人工改判表：键 `mid-idx`，值 {category, note}。

    规则算错的那几条不必硬调关键词，人工改判优先于规则，且重算分类不会丢。
    """
    raw_path = (cfg.get("paths") or {}).get("overrides") or "data/overrides.yaml"
    p = Path(raw_path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {str(k): v for k, v in (raw.get("overrides") or {}).items()}


def save_overrides(path: str, mapping: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    head = (
        "# 人工改判表。键为 `mid-idx`（文章身份证），值含 category 与 note。\n"
        "# 这里的选择优先于 rules.yaml 的规则判定，重算分类、重导出都不会丢。\n"
        "# 由 `python run.py import-xlsx` 从台账里读回来，也可以手工编辑。\n"
        "overrides:\n"
    )
    body = yaml.safe_dump({"overrides": mapping}, allow_unicode=True, sort_keys=True,
                          default_flow_style=False)
    p.write_text(head + body.split("\n", 1)[1], encoding="utf-8")


def _score(cat: dict, title: str, digest: str, body: str, d: dict):
    """按字段累加，返回 (总分, 标题摘要分, 命中词, 标题摘要命中数)。

    正文里几乎每篇文章都会提到「活动、会议、交流」这类泛词，
    如果让正文参与定性，几乎所有文章都会被拉进成果业绩或公益类。
    所以另算一个「标题摘要分」，归类门槛只认它；正文分只用于判断
    是否值得作为「待核实」提出来。
    """
    items = [(kw, lv) for lv in ("strong", "normal") for kw in (cat.get(lv) or [])]
    s = 0.0
    head = 0.0
    hits: list[str] = []
    strong_in_title = 0
    headline_hits = 0

    for field, text, fw in (
        ("标题", title, d["title_weight"]),
        ("摘要", digest, d["digest_weight"]),
        ("正文", body, d["body_weight"]),
    ):
        if not text:
            continue
        matched = [kw for kw, _ in items if kw in text]
        kept: list[str] = []
        for kw in sorted(matched, key=len, reverse=True):
            if any(kw != k and kw in k for k in kept):
                continue
            kept.append(kw)
        for kw in kept:
            level = next(lv for k, lv in items if k == kw)
            weight = d["strong_weight"] if level == "strong" else d["normal_weight"]
            s += weight * fw
            hits.append(f"{kw}({field})")
            if field != "正文":
                head += weight * fw
                headline_hits += 1
            if field == "标题" and level == "strong":
                strong_in_title += 1

    if strong_in_title >= int(d.get("title_bonus_min_strong", 2)):
        bonus = float(d.get("title_bonus", 0))
        s += bonus
        head += bonus
    return s, head, hits, headline_hits


def _veto(cat: dict, title: str):
    if not cat.get("veto_on_negative"):
        return None
    for nk in cat.get("negative", []) or []:
        if nk in title:
            return nk
    return None


def _festival_hits(cat: dict, title: str):
    groups = cat.get("groups") or {}
    if not groups:
        return None
    matched = {}
    for name, words in groups.items():
        hit = [w for w in words if w in title]
        if not hit:
            return None
        matched[name] = hit
    for w in cat.get("must_not_have", []) or []:
        if w in title:
            return None
    return matched


def _field(art, name: str, default: str = "") -> str:
    try:
        v = art[name]
    except (KeyError, IndexError):
        return default
    return v if v is not None else default


def _try_exclude(rules: dict, title: str, d: dict):
    """在标题上判「是否该排除」。有命中就返回 (cat, score, hits)，否则 None。

    顺序：先判「多个词同时出现才算」的（节日祝福、常规讲座），
    再按分数取最高的单关键词排除类。
    """
    for cat in rules["categories"]:
        if cat.get("type") != "exclude" or cat.get("logic") != "all_of_groups":
            continue
        matched = _festival_hits(cat, title)
        if matched:
            detail = " / ".join(f"{k}：{'、'.join(v)}" for k, v in matched.items())
            return (cat, 0.0, [detail])

    best = None
    floor = float(d.get("exclude_min_score", 1.0))
    for cat in rules["categories"]:
        if cat.get("type") != "exclude" or cat.get("logic") == "all_of_groups":
            continue
        # 标题里出现「举办/主办/莅临」这类所级活动信号时，不按排除类处理：
        # 「…研讨会在我单位举办」是会议，不是专业文章。
        if any(w in title for w in (cat.get("must_not_have") or [])):
            continue
        s, _head, hits, _n = _score(cat, title, "", "", d)
        if s >= floor and (best is None or s > best[1]):
            best = (cat, s, hits)
    return best


def _excluded_row(best) -> dict:
    cat, score, hits = best
    return {
        "category": cat["id"], "is_primary": 1, "score": round(float(score), 2),
        "confidence": "", "zone": "reject", "reason": cat["name"],
        "matched": "、".join(hits[:8]),
    }


def classify_one(art, rules: dict, scan_chars: int = 1200) -> list[dict]:
    d = dict(rules["defaults"])
    d["body_scan_chars"] = scan_chars
    title = _field(art, "title")
    digest = _field(art, "digest")
    body = _field(art, "content_text")[:scan_chars]
    channel = _field(art, "channel").strip()
    ch_cat = (rules.get("channels") or {}).get(channel)
    ch_weight = float(rules.get("channel_weight", 6.0))
    gray_low = float(d["gray_low"])
    gray_high = float(d["gray_high"])

    # 1) 栏目被编辑定性为排除类（专业视点 / 讲座预告 / 年度案例）：直接排除
    for cat in rules["categories"]:
        if cat.get("id") == ch_cat and cat.get("type") == "exclude":
            return [{
                "category": cat["id"], "is_primary": 1, "score": ch_weight,
                "confidence": "", "zone": "reject", "reason": cat["name"],
                "matched": f"栏目:{channel}",
            }]

    # 2) 栏目没给事件定性（空栏目或「新闻简讯」）时，先用标题判排除。
    #    放在事件打分之前，否则正文里的泛词会把「应邀讲课」这类拉进事件类。
    #    栏目已定性为荣誉/捷报/党建的文章不参与排除——编辑的判断优先于关键词。
    if not ch_cat:
        best = _try_exclude(rules, title, d)
        if best:
            return [_excluded_row(best)]

    # 3) 事件类别打分。归类门槛只认「标题摘要分」，正文分不参与定性。
    scored = []
    for cat in rules["categories"]:
        if cat.get("type") != "event":
            continue
        t, dg, bd = title, digest, body
        if cat.get("scope") == "title_only":
            dg = bd = ""
        veto = _veto(cat, title)
        s, head, hits, n_head = _score(cat, t, dg, bd, d)
        if veto:
            s, head, hits, n_head = 0.0, 0.0, [f"负向词「{veto}」否决"], 0
        elif ch_cat == cat["id"]:
            s += ch_weight
            head += ch_weight
            n_head += 1
            hits.insert(0, f"栏目:{channel}")
        scored.append((cat, s, head, hits, n_head))

    # 标题摘要够线才算数；只有正文命中时，必须总分足够高才作为「待核实」提出
    events = [(c, s, h, hits, n) for c, s, h, hits, n in scored
              if h >= gray_low or (s >= gray_high and h == 0)]
    if events:
        events.sort(key=lambda x: (-x[2], -x[1], -int(x[0].get("priority", 0))))
        primary = events[0][0]
        rows = []
        for cat, s, head, hits, n_head in events:
            zone = "normal" if head >= gray_high else "gray"
            rows.append({
                "category": cat["id"],
                "is_primary": 1 if cat["id"] == primary["id"] else 0,
                "score": round(s, 2),
                "confidence": "高" if zone == "normal" else "中",
                "zone": zone,
                "reason": cat["name"],
                "matched": ("仅正文命中·" if n_head == 0 else "") + "、".join(hits[:8]),
            })
        return rows

    # 4) 事件分不够，再看看是不是某个排除类（栏目已定性的文章不会走到这里）
    best = _try_exclude(rules, title, d)
    if best:
        return [_excluded_row(best)]

    return [{
        "category": "undetermined", "is_primary": 1, "score": 0.0,
        "confidence": "", "zone": "reject", "reason": "未达阈值", "matched": "",
    }]


def run_classify(cfg, conn) -> dict:
    rules = load_rules(cfg["classify"]["rules"])
    scan = int(cfg["classify"].get("body_scan_chars", 1200))
    annual_words = rules.get("annual_review_words", [])
    names = {c["id"]: c["name"] for c in rules["categories"]}
    overrides = load_overrides(cfg)
    articles = db.get_articles(conn, order="DESC")

    run_id = db.start_run(conn, "classify", f"articles={len(articles)}")
    counter = {}
    for art in articles:
        key = f"{art['mid']}-{art['idx']}"
        ov = overrides.get(key)
        if ov and ov.get("category"):
            cid = str(ov["category"])
            note = str(ov.get("note") or "").strip()
            rows = [{
                "category": cid, "is_primary": 1,
                "score": 0.0,
                "confidence": "人工",
                "zone": "reject" if (cid in VERDICT_NAMES or cid == "undetermined") else "normal",
                "reason": names.get(cid, cid),
                "matched": "人工改判" + (f"：{note}" if note else ""),
            }]
        else:
            rows = classify_one(art, rules, scan)
        db.replace_classifications(conn, art["id"], rows, run_id)
        if any(w in (art["title"] or "") for w in annual_words):
            db.add_flag(conn, art["id"], "annual_review")
        counter[rows[0]["category"]] = counter.get(rows[0]["category"], 0) + 1
    conn.commit()
    db.finish_run(conn, run_id, json.dumps(counter, ensure_ascii=False))
    return counter


def verdict_cn(cat_id: str) -> str:
    return VERDICT_NAMES.get(cat_id, cat_id)
