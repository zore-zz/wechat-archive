# -*- coding: utf-8 -*-
"""导出：试跑报告（pilot.md）、正式台账（Excel）、大事记索引（Markdown）。"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

import classify
import db

# ---------------- 通用 ----------------

_EXCLUDE_IDS = set(classify.VERDICT_NAMES.keys())


def _brand(cfg) -> str:
    """导出文件与标题里用的单位名。config 没配就退回昵称，再退回中性词。"""
    acc = cfg.get("account") or {}
    return acc.get("brand") or acc.get("nickname") or "本单位"


def _ledger_name(cfg, year) -> str:
    return f"{_brand(cfg)}大事记台账_{year}.xlsx"


def _names(rules) -> dict:
    return {c["id"]: c["name"] for c in rules["categories"]}


def _event_cats(rules) -> list[tuple[str, str]]:
    return [(c["id"], c["name"]) for c in rules["categories"] if c.get("type") == "event"]


def _id(art) -> str:
    return f"{art['mid']}-{art['idx']}"


def _is_event(r) -> bool:
    cid = r["cat_id"] or "undetermined"
    return cid not in _EXCLUDE_IDS and cid != "undetermined"


def _is_gray(r) -> bool:
    return _is_event(r) and (r["zone"] or "") == "gray"


def _is_annual(r) -> bool:
    return "annual_review" in (r["flags"] or "")


def _load(cfg, conn):
    rules = classify.load_rules(cfg["classify"]["rules"])
    window = cfg.get("window") or {}
    rows = db.articles_for_export(conn, since=window.get("since"),
                                  until=window.get("until"))
    return rules, list(rows)


def _hits_brief(r, limit: int = 6) -> str:
    """定性只看标题和摘要，索引里不必把正文命中的一堆泛词铺出来。"""
    raw = (r["matched"] or "").replace("仅正文命中·", "")
    parts = [p for p in raw.split("、") if p and not p.endswith("(正文)")]
    if not parts:
        return "仅正文命中"
    return "、".join(parts[:limit])


def _summary(r, n: int = 110) -> str:
    """摘要优先用公众号自带的 digest，没有就从正文开头取。"""
    d = (r["digest"] or "").strip()
    if d and len(d) >= 12:
        return d[:n]
    body = (r["content_text"] or "").strip()
    if body:
        first = next((ln.strip() for ln in body.split("\n") if len(ln.strip()) > 8), "")
        if first:
            return first[:n]
    return d[:n]


# ---------------- Excel ----------------

_HEAD_FILL = PatternFill("solid", fgColor="1F3864")
_HEAD_FONT = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="微软雅黑", size=10)
_LINK_FONT = Font(name="微软雅黑", size=9, color="0563C1", underline="single")
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_GRAY_FILL = PatternFill("solid", fgColor="FFF2CC")   # 待核实：浅黄
_ANNUAL_FILL = PatternFill("solid", fgColor="E2EFDA")  # 年度参考：浅绿
_CAT_FILLS = {
    "honor": "FCE4D6",
    "case_achievement": "DDEBF7",
    "leader_visit": "E4DFEC",
    "personnel": "FFF2CC",
    "party_building": "F8CBAD",
    "public_welfare": "E2EFDA",
    "branch_coop": "DEEAF6",
    "conference": "D9E1F2",
    "anniversary_culture": "FFF2CC",
    "media_report": "EDEDED",
}


def _write_sheet(wb, title, headers, rows, widths, freezes="A2"):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci)
        c.fill, c.font = _HEAD_FILL, _HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = _BORDER
    ws.row_dimensions[1].height = 26

    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(headers)):
        for c in row:
            c.font = _BODY_FONT
            c.border = _BORDER
            c.alignment = Alignment(vertical="top", wrap_text=True)

    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = freezes
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    return ws


def _add_dropdown(ws, col: int, values: list[str]) -> None:
    """给某一列加下拉候选，值太多（超 255 字符）就不加。"""
    text = ",".join(v for v in values if v)
    if not text or len(text) > 250:
        return
    letter = get_column_letter(col)
    dv = DataValidation(type="list", formula1=f'"{text}"', allow_blank=True,
                        showDropDown=False)
    dv.error = "请从下拉里选一个类别"
    dv.errorTitle = "类别不在清单里"
    dv.prompt = "可直接改这里，改完跑 run.py import-xlsx 把改动读回库"
    dv.promptTitle = "人工改判"
    ws.add_data_validation(dv)
    dv.add(f"{letter}2:{letter}{max(ws.max_row, 2)}")


def _pick_dates(rows) -> str:
    ds = [r["publish_date"] for r in rows if r["publish_date"]]
    return f"{min(ds)} ~ {max(ds)}" if ds else "—"


def write_ledger(cfg, conn) -> dict:
    rules, rows = _load(cfg, conn)
    names = _names(rules)
    out_dir = Path(cfg["paths"]["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    year = (cfg.get("window") or {}).get("since", "")[:4] or "全部"
    xlsx = out_dir / _ledger_name(cfg, year)

    events = [r for r in rows if _is_event(r)]
    events.sort(key=lambda r: (r["publish_date"] or "", r["publish_ts"] or 0))
    gray = [r for r in events if _is_gray(r)]
    annual = [r for r in rows if _is_annual(r)]
    excluded = [r for r in rows if not _is_event(r)]

    wb = Workbook()
    wb.remove(wb.active)

    # --- Sheet 1：大事记候选 ---
    headers = ["序号", "日期", "月份", "类别", "事件标题", "置信", "得分",
               "命中依据", "内容摘要", "原文链接", "复核状态", "备注"]
    widths = [6, 11, 9, 16, 42, 6, 7, 30, 46, 26, 10, 18]
    data = []
    for i, r in enumerate(events, 1):
        cid = r["cat_id"]
        data.append([
            i, r["publish_date"], (r["publish_date"] or "")[:7],
            names.get(cid, cid), r["title"], r["confidence"] or "",
            round(float(r["score"] or 0), 1), r["matched"] or "",
            _summary(r), r["link"], r["decision"] or "未复核",
            r["review_note"] or "",
        ])
    ws = _write_sheet(wb, "大事记候选", headers, data, widths)
    for i, r in enumerate(events, 2):
        cid = r["cat_id"]
        fill = _CAT_FILLS.get(cid)
        if fill:
            ws.cell(row=i, column=4).fill = PatternFill("solid", fgColor=fill)
        if _is_gray(r):
            for ci in range(1, len(headers) + 1):
                ws.cell(row=i, column=ci).fill = _GRAY_FILL
        lk = ws.cell(row=i, column=10)
        lk.hyperlink, lk.font = r["link"], _LINK_FONT

    # --- Sheet 2：全年稿件汇总（全部文章按时间正序，含已排除的，作为全年底稿） ---
    headers_all = ["序号", "日期", "月份", "栏目", "标题", "判定", "置信", "得分",
                   "字数", "内容摘要", "原文链接", "正文全文"]
    widths_all = [6, 11, 9, 12, 46, 20, 6, 7, 7, 44, 26, 60]
    all_sorted = sorted(rows, key=lambda r: (r["publish_date"] or "", r["publish_ts"] or 0,
                                             r["idx"] or 0))
    data_all = []
    for i, r in enumerate(all_sorted, 1):
        cid = r["cat_id"] or "undetermined"
        verdict = names.get(cid) or classify.VERDICT_NAMES.get(cid, "未达阈值")
        fetched = (r["body_status"] or 0) == 1
        data_all.append([
            i, r["publish_date"], (r["publish_date"] or "")[:7], r["channel"] or "",
            r["title"], verdict, r["confidence"] or "",
            round(float(r["score"] or 0), 1) if r["score"] else "",
            (r["content_len"] or 0) if fetched else "—",
            _summary(r, 120), r["link"], (r["content_text"] or "") if fetched else "",
        ])
    ws_all = _write_sheet(wb, "全年稿件汇总", headers_all, data_all, widths_all)
    for i, r in enumerate(all_sorted, 2):
        cid = r["cat_id"] or "undetermined"
        fill = _CAT_FILLS.get(cid)
        cell = ws_all.cell(row=i, column=6)
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
        elif not _is_event(r):
            cell.fill = PatternFill("solid", fgColor="F2F2F2")
        lk = ws_all.cell(row=i, column=11)
        lk.hyperlink, lk.font = r["link"], _LINK_FONT
        # 正文全文不换行：否则一条长文会把行高撑到离谱，点单元格可在编辑栏看全文
        txt = ws_all.cell(row=i, column=12)
        txt.alignment = Alignment(vertical="top", wrap_text=False)

    # 判定列给下拉，避免手改时写出脚本认不出的类别名（导入时会对不上）
    allow = [n for n in names.values()] + ["未达阈值"]
    _add_dropdown(ws_all, col=6, values=allow)
    _add_dropdown(ws, col=4, values=allow)

    # --- Sheet 3：待核实 ---
    headers2 = ["序号", "日期", "类别", "事件标题", "得分", "为何待核实", "内容摘要", "原文链接"]
    widths2 = [6, 11, 16, 42, 7, 34, 46, 26]
    data2 = []
    for i, r in enumerate(gray, 1):
        why = "只靠正文命中，标题/摘要无事件词" if "仅正文命中" in (r["matched"] or "") else "分数落在灰区"
        data2.append([i, r["publish_date"], names.get(r["cat_id"], r["cat_id"]), r["title"],
                      round(float(r["score"] or 0), 1), why, _summary(r), r["link"]])
    ws2 = _write_sheet(wb, "待核实", headers2, data2, widths2)
    for i in range(2, ws2.max_row + 1):
        lk = ws2.cell(row=i, column=8)
        lk.hyperlink, lk.font = lk.value, _LINK_FONT

    # --- Sheet 3：年度参考（年度案例/年度回顾） ---
    headers3 = ["序号", "日期", "栏目", "标题", "内容摘要", "原文链接", "用途"]
    widths3 = [6, 11, 12, 46, 52, 26, 20]
    data3 = []
    for i, r in enumerate(sorted(annual, key=lambda x: x["publish_date"] or ""), 1):
        use = "年度案例清单，可反查业绩" if (r["channel"] == "年度案例") else "年度总结，可交叉校验"
        data3.append([i, r["publish_date"], r["channel"] or "", r["title"],
                      _summary(r, 140), r["link"], use])
    ws3 = _write_sheet(wb, "年度参考", headers3, data3, widths3)
    for i in range(2, ws3.max_row + 1):
        ws3.cell(row=i, column=1).fill = _ANNUAL_FILL
        lk = ws3.cell(row=i, column=6)
        lk.hyperlink, lk.font = lk.value, _LINK_FONT

    # --- Sheet 4：已排除 ---
    headers4 = ["序号", "日期", "判定", "标题", "排除原因", "原文链接"]
    widths4 = [6, 11, 14, 46, 40, 26]
    data4 = []
    ex_sorted = sorted(excluded, key=lambda r: (r["cat_id"] or "", r["publish_date"] or ""))
    for i, r in enumerate(ex_sorted, 1):
        cid = r["cat_id"] or "undetermined"
        verdict = classify.VERDICT_NAMES.get(cid, "未达阈值")
        data4.append([i, r["publish_date"], verdict, r["title"],
                      r["matched"] or "无命中词", r["link"]])
    ws4 = _write_sheet(wb, "已排除", headers4, data4, widths4)
    for i in range(2, ws4.max_row + 1):
        lk = ws4.cell(row=i, column=6)
        lk.hyperlink, lk.font = lk.value, _LINK_FONT

    # --- Sheet 5：统计 ---
    ws5 = wb.create_sheet("统计")
    months = sorted({(r["publish_date"] or "")[:7] for r in rows if r["publish_date"]})
    cat_ids = [cid for cid, _ in _event_cats(rules)]
    grid = defaultdict(Counter)
    for r in events:
        grid[r["cat_id"]][(r["publish_date"] or "")[:7]] += 1

    ws5.append([f"统计区间：{_pick_dates(rows)}　共 {len(rows)} 篇"])
    ws5["A1"].font = Font(name="微软雅黑", size=11, bold=True)
    ws5.append([])
    head_row = ["类别"] + months + ["合计"]
    ws5.append(head_row)
    hr = ws5.max_row
    for ci in range(1, len(head_row) + 1):
        c = ws5.cell(row=hr, column=ci)
        c.fill, c.font = _HEAD_FILL, _HEAD_FONT
        c.alignment = Alignment(horizontal="center")
        c.border = _BORDER
    for cid in cat_ids:
        line = [names.get(cid, cid)] + [grid[cid].get(m, 0) or "" for m in months]
        line.append(sum(grid[cid].values()))
        ws5.append(line)
    total = ["合计"] + [sum(grid[c].get(m, 0) for c in cat_ids) or "" for m in months]
    total.append(len(events))
    ws5.append(total)
    for row in ws5.iter_rows(min_row=hr + 1, max_row=ws5.max_row, max_col=len(head_row)):
        for c in row:
            c.font = _BODY_FONT
            c.border = _BORDER
            c.alignment = Alignment(horizontal="center")
    for c in ws5[ws5.max_row]:
        c.font = Font(name="微软雅黑", size=10, bold=True)
    ws5.column_dimensions["A"].width = 18
    for i in range(2, len(head_row) + 1):
        ws5.column_dimensions[get_column_letter(i)].width = 9
    ws5.freeze_panes = f"B{hr + 1}"

    # Excel 被占用时不阻断流程：md 照常更新，只提示 Excel 没写进去
    locked = False
    try:
        wb.save(xlsx)
    except PermissionError:
        locked = True
    return {"xlsx": xlsx, "events": len(events), "gray": len(gray),
            "annual": len(annual), "excluded": len(excluded), "total": len(rows),
            "locked": locked}


# ---------------- Markdown 索引 ----------------

def write_index(cfg, conn) -> Path:
    """给 AI 写大事记用的索引：按月份分组，保留标题、摘要、链接。"""
    rules, rows = _load(cfg, conn)
    names = _names(rules)
    out_dir = Path(cfg["paths"]["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    year = (cfg.get("window") or {}).get("since", "")[:4] or "全部"
    path = out_dir / f"大事记索引_{year}.md"

    events = [r for r in rows if _is_event(r)]
    events.sort(key=lambda r: (r["publish_date"] or "", r["publish_ts"] or 0))
    gray = {r["id"] for r in events if _is_gray(r)}
    annual = sorted([r for r in rows if _is_annual(r) and not _is_event(r)],
                    key=lambda r: r["publish_date"] or "")
    excluded = [r for r in rows if not _is_event(r)]

    by_month = defaultdict(list)
    for r in events:
        by_month[(r["publish_date"] or "未知")[:7]].append(r)

    cnt = Counter(r["cat_id"] for r in events)
    lines: list[str] = []
    lines.append(f"# {_brand(cfg)} {year} 年大事记 · 事实底稿\n")
    lines.append(f"- 统计区间：{_pick_dates(rows)}，共抓取 {len(rows)} 篇")
    lines.append(f"- 事件候选：{len(events)} 篇（其中待核实 {len(gray)} 篇）"
                 f"　|　排除 {len(excluded)} 篇　|　年度参考 {len(annual)} 篇")
    lines.append("- 类别分布：" + "、".join(
        f"{names.get(k, k)} {v}" for k, v in cnt.most_common()))
    lines.append("")
    lines.append("> 每条都带原文链接，写大事记时事实以原文为准。标「待核实」的请先扫一眼。")
    lines.append("> 文末附「全年稿件汇总」，是当年全部稿件的流水（含被排除的），可用来核对有没有漏稿。\n")
    lines.append("---\n")

    for m in sorted(by_month, reverse=True):
        items = by_month[m]
        lines.append(f"## {m}（{len(items)} 项）\n")
        for r in items:
            cid = r["cat_id"]
            mark = " ⚠️待核实" if r["id"] in gray else ""
            lines.append(f"### {r['publish_date']}　[{names.get(cid, cid)}]{mark}")
            lines.append(f"**{r['title']}**")
            s = _summary(r, 200)
            if s:
                lines.append(f"- 摘要：{s}")
            if r["matched"]:
                lines.append(f"- 判定依据：{_hits_brief(r)}")
            lines.append(f"- 原文：{r['link']}")
            lines.append("")
    lines.append("---\n")

    if annual:
        lines.append("## 年度参考（不计入事件，供交叉校验）\n")
        for r in annual:
            lines.append(f"- {r['publish_date']}　{r['title']}　{r['link']}")
        lines.append("")

    # 全年流水：把当年所有稿件按时间正序列一遍（含被排除的），用来核「有没有漏稿」
    all_sorted = sorted(rows, key=lambda r: (r["publish_date"] or "", r["publish_ts"] or 0,
                                             r["idx"] or 0))
    lines.append("---\n")
    lines.append(f"## 全年稿件汇总（{len(all_sorted)} 篇，按时间正序）\n")
    lines.append("含被排除的稿件，用于核对全年有没有漏稿。标题即原文链接。\n")
    lines.append("| # | 日期 | 栏目 | 标题 | 判定 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for i, r in enumerate(all_sorted, 1):
        cid = r["cat_id"] or "undetermined"
        verdict = names.get(cid) or classify.VERDICT_NAMES.get(cid, "未达阈值")
        if _is_gray(r):
            verdict += "（待核实）"
        title = (r["title"] or "").replace("|", "/")
        link = r["link"] or ""
        cell = f"[{title}]({link})" if link else title
        lines.append(f"| {i} | {r['publish_date'] or ''} | {r['channel'] or '—'} "
                     f"| {cell} | {verdict} |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_excluded(cfg, conn) -> Path:
    """被排除的条目全部留档，附原因，便于判断是否误杀。"""
    rules, rows = _load(cfg, conn)
    out_dir = Path(cfg["paths"]["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    year = (cfg.get("window") or {}).get("since", "")[:4] or "全部"
    path = out_dir / f"已排除_{year}.md"

    groups = defaultdict(list)
    for r in rows:
        if _is_event(r):
            continue
        cid = r["cat_id"] or "undetermined"
        groups[cid].append(r)

    lines = [f"# {year} 年被排除的条目\n",
             "判定为以下类别的文章不会进入大事记候选。若发现误杀，改 `rules.yaml` 后重跑分类即可，不用重新抓取。\n"]
    order = list(classify.VERDICT_NAMES.items())
    for cid, name in order:
        items = sorted(groups.get(cid, []), key=lambda r: r["publish_date"] or "")
        if not items:
            continue
        lines.append(f"## {name}（{len(items)} 篇）\n")
        for r in items:
            lines.append(f"- {r['publish_date']}　{r['title']}　`{r['matched'] or '—'}`")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------- 试跑报告 ----------------

def write_pilot(cfg, conn, n: int) -> Path:
    rules = classify.load_rules(cfg["classify"]["rules"])
    names = _names(rules)
    rows = db.articles_for_export(conn, limit=n)

    out_dir = Path(cfg["paths"]["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "pilot.md"

    dates = [r["publish_date"] for r in rows if r["publish_date"]]
    span = f"{min(dates)} ~ {max(dates)}" if dates else "无数据"

    counts: dict[str, int] = {}
    covered: dict[str, int] = {}
    for r in rows:
        cid = r["cat_id"] or "undetermined"
        counts[cid] = counts.get(cid, 0) + 1
        if cid not in classify.VERDICT_NAMES and cid != "undetermined":
            covered[cid] = covered.get(cid, 0) + 1

    event_cnt = sum(v for k, v in counts.items() if k not in classify.VERDICT_NAMES)
    other = "　".join(
        f"{classify.VERDICT_NAMES[k]} {counts[k]}"
        for k in classify.VERDICT_NAMES
        if counts.get(k)
    )
    if counts.get("undetermined"):
        other = (other + f"　未达阈值 {counts['undetermined']}").strip()
    cats_line = "、".join(f"{names.get(k)} {v}" for k, v in
                          sorted(covered.items(), key=lambda x: -x[1])) or "无"

    missing = [name for cid, name in _event_cats(rules) if cid not in covered]

    lines = []
    lines.append(f"# 试跑：最近 {len(rows)} 篇\n")
    lines.append(f"- 样本区间：{span}")
    lines.append(f"- 判定汇总：事件 {event_cnt} 篇　{other}".rstrip())
    lines.append(f"- 事件类别：{cats_line}")
    if missing:
        lines.append(f"- **未覆盖（样本不足，规则未校验）：{'、'.join(missing)}**")
    lines.append("")

    lines.append("## 逐条判定\n")
    lines.append("| # | 日期 | 栏目 | 标题 | 判定 | 置信 | 得分 | 命中词 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(rows, 1):
        cid = r["cat_id"] or "undetermined"
        verdict = names.get(cid, "未达阈值")
        conf = r["confidence"] or "—"
        score = f"{r['score']:.1f}" if r["score"] else "—"
        hits = (r["matched"] or "—").replace("|", "/")
        title = (r["title"] or "").replace("|", "/")
        ch = (r["channel"] or "—") if "channel" in r.keys() else "—"
        lines.append(f"| {i} | {r['publish_date'] or ''} | {ch} | {title} | {verdict} "
                     f"| {conf} | {score} | {hits} |")
    lines.append("")

    lines.append("## 逐条详情\n")
    for i, r in enumerate(rows, 1):
        cid = r["cat_id"] or "undetermined"
        verdict = names.get(cid, "未达阈值")
        flag = "　**[年度总结·重点]**" if _is_annual(r) else ""
        lines.append(f"**{i}. {r['title']}**{flag}")
        score_txt = f"{r['score']:.1f}" if r["score"] else "—"
        ch = (r["channel"] or "无栏目") if "channel" in r.keys() else "无栏目"
        lines.append(f"- 日期：{r['publish_date']}　｜　栏目：{ch}　｜　判定：{verdict}"
                     f"　｜　置信：{r['confidence'] or '—'}　｜　得分：{score_txt}")
        lines.append(f"- 命中：{r['matched'] or '无'}")
        summary = (r["digest"] or "").strip() or "（无摘要）"
        lines.append(f"- 摘要：{summary[:120]}")
        lines.append(f"- 链接：{r['link']}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------- 从台账读回人工改判 ----------------

def import_xlsx_edits(cfg, conn, path: str | None = None) -> dict:
    """把台账里人工改过的「判定」读回来，写进 data/overrides.yaml。

    场景：在 Excel 里直接把某篇的类别改掉。这种改动只在那个文件里，
    下次导出会被脚本覆盖。跑一次导入，改判就固化成 overrides，
    重算分类、重导出都不会丢，台账里还会标「人工改判」。
    """
    rules = classify.load_rules(cfg["classify"]["rules"])
    names = {c["id"]: c["name"] for c in rules["categories"]}
    names["undetermined"] = "未达阈值"
    rev = {v: k for k, v in names.items()}
    rev.update({v: k for k, v in classify.VERDICT_NAMES.items()})  # 兼容短名

    year = (cfg.get("window") or {}).get("since", "")[:4] or "全部"
    xlsx = Path(path) if path else Path(cfg["paths"]["out"]) / _ledger_name(cfg, year)
    if not xlsx.exists():
        raise FileNotFoundError(f"找不到台账：{xlsx}")

    cur = {}
    for r in db.articles_for_export(conn):
        cur[((r["title"] or "").strip(), (r["publish_date"] or "")[:10])] = r

    wb = load_workbook(xlsx, data_only=True)
    changes, unknown = [], []
    for sheet in ("全年稿件汇总", "大事记候选", "已排除"):
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        hdr = [str(c.value or "") for c in ws[1]]
        try:
            i_date = hdr.index("日期")
            i_title = hdr.index("标题") if "标题" in hdr else hdr.index("事件标题")
            i_judge = hdr.index("判定") if "判定" in hdr else hdr.index("类别")
        except ValueError:
            continue
        for row in ws.iter_rows(min_row=2, values_only=True):
            title = str(row[i_title] or "").strip()
            date = str(row[i_date] or "").strip()[:10]
            raw = str(row[i_judge] or "").replace("（待核实）", "").strip()
            if not title or not raw:
                continue
            cid = rev.get(raw)
            if not cid:
                unknown.append(raw)
                continue
            r = cur.get((title, date))
            if not r:
                continue
            old = r["cat_id"] or "undetermined"
            if old == cid:
                continue
            changes.append({
                "key": f"{r['mid']}-{r['idx']}",
                "category": cid,
                "note": f"台账人工调整：{names.get(old, old)} → {names.get(cid, cid)}",
                "date": date, "title": title,
            })

    if changes:
        ov_path = (cfg.get("paths") or {}).get("overrides") or "data/overrides.yaml"
        mapping = {k: {"category": v["category"], "note": v["note"]}
                   for k, v in classify.load_overrides(cfg).items()}
        for c in changes:
            mapping[c["key"]] = {"category": c["category"], "note": c["note"]}
        classify.save_overrides(ov_path, mapping)

    return {"file": str(xlsx), "changes": changes, "unknown": sorted(set(unknown))}
