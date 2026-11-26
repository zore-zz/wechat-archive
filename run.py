# -*- coding: utf-8 -*-
"""命令行入口。双击 启动.bat 走交互菜单。"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

import yaml  # noqa: E402

import classify  # noqa: E402
import db  # noqa: E402
import export  # noqa: E402
import fetch  # noqa: E402
import login as login_mod  # noqa: E402

EXIT_RATE_LIMITED = 2
EXIT_ENV_BLOCKED = 3
EXIT_AUTH = 4


def load_cfg() -> dict:
    """读 config.yaml。首次运行自动从模板生成，仓库里只留模板，不留真实配置。"""
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        shutil.copyfile(ROOT / "config.example.yaml", cfg_path)
        print("[config] 未找到 config.yaml，已从 config.example.yaml 生成，"
              "请先填写 account.nickname / account.brand")
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def open_db(cfg):
    return db.connect(cfg["paths"]["db"])


# ---------------- 各命令 ----------------

def cmd_login(cfg, args) -> int:
    auth = login_mod.ensure_auth(cfg, force=getattr(args, "force", False))
    ok, ret = fetch.api_probe(auth, cfg)
    who = auth.get("nickname") or cfg.get("account", {}).get("nickname", "")
    print(f"[login] 公众号：{who}")
    print(f"[login] fakeid：{auth.get('fakeid') or '(未识别，按当前登录号抓取)'}")
    print(f"[login] 接口探针：ret={ret} {'可用' if ok else '不可用'}")
    return 0 if ok else EXIT_AUTH


def cmd_list(cfg, args) -> int:
    conn = open_db(cfg)
    if args.pilot is not None:
        mode, n = "pilot", args.pilot
    elif args.backfill:
        mode, n = "backfill", 0
    elif args.full:
        mode, n = "full", 0
    else:
        mode, n = "new", 0
    stats = fetch.run_list(cfg, conn, mode=mode, n=n, before_date=args.before)
    print(f"[list] 完成：翻 {stats['pages']} 页，新增 {stats['new']} 篇")
    return 0


def cmd_import(cfg, args) -> int:
    """把台账里手工改过的类别读回来，固化成 data/overrides.yaml。"""
    conn = open_db(cfg)
    r = export.import_xlsx_edits(cfg, conn, getattr(args, "path", None))
    print(f"[import] 读 {r['file']}")
    if not r["changes"]:
        print("[import] 台账里的判定和库里一致，没有改动")
        return 0
    print(f"[import] 识别到 {len(r['changes'])} 处人工改判，已写入 overrides：")
    for c in r["changes"]:
        print(f"          {c['date']}　{c['note']}　| {c['title'][:34]}")
    counter = classify.run_classify(cfg, conn)
    print("[import] 已按人工改判重算分类")
    print("[import] 接着跑 `python run.py export` 出新台账（关掉 Excel 再跑）")
    return 0


def cmd_verify(cfg, args) -> int:
    """后台列表和本地库对账，补进漏掉的条目（只补不删）。"""
    conn = open_db(cfg)
    stats = fetch.run_verify(cfg, conn, since=getattr(args, "since", None),
                             max_pages=getattr(args, "max_pages", None))
    added = stats["added"]
    print(f"\n[verify] 比对 {stats['seen']} 条，翻 {stats['pages']} 页，补进 {len(added)} 篇")
    for a in added:
        print(f"          + {a['publish_date']}　{a['title'][:40]}")
    if added:
        print("\n[verify] 这些新补的文章还没抓正文，接着跑 `python run.py body` 即可")
    return 0


def cmd_body(cfg, args) -> int:
    conn = open_db(cfg)
    stats = fetch.run_body(cfg, conn, limit=args.limit)
    if stats.get("skipped_quota"):
        return 0
    print(f"[body] 完成：成功 {stats.get('ok', 0)}，已删 {stats.get('deleted', 0)}，"
          f"异常 {stats.get('blocked', 0)}，失败 {stats.get('failed', 0)}，"
          f"跳过 {stats.get('skipped', 0)}")
    return EXIT_ENV_BLOCKED if stats.get("blocked", 0) >= 2 else 0


def cmd_classify(cfg, args) -> int:
    conn = open_db(cfg)
    counter = classify.run_classify(cfg, conn)
    if not counter:
        print("[classify] 库里还没有文章")
        return 0
    names = {c["id"]: c["name"] for c in
             classify.load_rules(cfg["classify"]["rules"])["categories"]}
    print("[classify] 判定分布：")
    for cid, n in sorted(counter.items(), key=lambda x: -x[1]):
        print(f"          {names.get(cid, '未达阈值'):<12} {n}")
    return 0


def cmd_export(cfg, args) -> int:
    conn = open_db(cfg)
    if getattr(args, "pilot", 0):
        n = args.pilot if args.pilot else 10
        path = export.write_pilot(cfg, conn, n)
        print(f"[export] 试跑报告：{path}")
        return 0

    r = export.write_ledger(cfg, conn)
    idx = export.write_index(cfg, conn)
    exc = export.write_excluded(cfg, conn)
    print("[export] 台账已生成：")
    if r.get("locked"):
        print(f"          Excel　　{r['xlsx']}　【被 Excel 占用，未写入，关掉后重跑本命令即可】")
    else:
        print(f"          Excel　　{r['xlsx']}")
    print(f"          索引　　 {idx}")
    print(f"          排除清单 {exc}")
    print(f"          共 {r['total']} 篇：事件候选 {r['events']}（待核实 {r['gray']}）"
          f"　年度参考 {r['annual']}　已排除 {r['excluded']}")
    return 0


def cmd_pilot(cfg, args) -> int:
    conn = open_db(cfg)
    n = args.n or 10
    print(f"===== 试跑：最近 {n} 篇 =====")
    s = fetch.run_list(cfg, conn, mode="pilot", n=n)
    if s["new"] == 0:
        print("[pilot] 最近这批文章已在库中，直接复用")
    print("\n===== 先按标题+摘要做初判 =====")
    classify.run_classify(cfg, conn)
    print("\n===== 抓正文（节日祝福自动跳过）=====")
    fetch.run_body(cfg, conn, limit=n)
    print("\n===== 带正文重新判定 =====")
    counter = classify.run_classify(cfg, conn)
    print("\n===== 生成试跑报告 =====")
    path = export.write_pilot(cfg, conn, n)
    names = {c["id"]: c["name"] for c in
             classify.load_rules(cfg["classify"]["rules"])["categories"]}
    for cid, cnt in sorted(counter.items(), key=lambda x: -x[1]):
        print(f"          {names.get(cid, '未达阈值'):<12} {cnt}")
    print(f"\n[pilot] 报告已生成：{path}")
    return 0


def cmd_status(cfg, args) -> int:
    conn = open_db(cfg)
    st = db.stats(conn)
    cats = db.category_counts(conn)
    names = {c["id"]: c["name"] for c in
             classify.load_rules(cfg["classify"]["rules"])["categories"]}
    print("===== 库内状态 =====")
    print(f"文章总数：{st.get('total') or 0}　区间：{st.get('first_date') or '—'} ~ {st.get('last_date') or '—'}")
    print(f"正文成功：{st.get('body_ok') or 0}　待抓：{st.get('body_todo') or 0}　"
          f"已删：{st.get('deleted') or 0}　异常：{st.get('blocked') or 0}　"
          f"失败：{st.get('failed') or 0}　跳过：{st.get('skipped') or 0}")
    if cats:
        print("判定分布：")
        for cid, n in cats.items():
            print(f"  {names.get(cid, '未达阈值'):<12} {n}")
    return 0


def _one_click(cfg, conn, list_mode: str = "new", n: int = 0,
               before: str | None = None) -> None:
    """一条龙：抓列表 -> 抓正文 -> 分类 -> 出台账。日常用这个就够。"""
    print("\n--- 1/4 抓列表 ---")
    s = fetch.run_list(cfg, conn, mode=list_mode, n=n, before_date=before)
    print(f"[list] 翻了 {s['pages']} 页，新增 {s['new']} 篇")

    print("\n--- 2/4 抓正文（节日祝福等排除项自动跳过）---")
    bs = fetch.run_body(cfg, conn)
    if bs.get("skipped_quota"):
        print("[body] 今日额度已用完，明天再跑")
    else:
        print(f"[body] 成功 {bs.get('ok', 0)}，已删 {bs.get('deleted', 0)}，"
              f"失败 {bs.get('failed', 0)}，跳过 {bs.get('skipped', 0)}")

    print("\n--- 3/4 重新分类 ---")
    counter = classify.run_classify(cfg, conn)
    names = {c["id"]: c["name"] for c in
             classify.load_rules(cfg["classify"]["rules"])["categories"]}
    for cid, cnt in sorted(counter.items(), key=lambda x: -x[1]):
        print(f"          {names.get(cid, '未达阈值'):<12} {cnt}")

    print("\n--- 4/4 生成台账 ---")
    r = export.write_ledger(cfg, conn)
    idx = export.write_index(cfg, conn)
    export.write_excluded(cfg, conn)
    print(f"[导出] {r['xlsx']}")
    print(f"[导出] {idx}")
    print(f"       事件候选 {r['events']}（待核实 {r['gray']}）　排除 {r['excluded']}")


MENU = """
==================================================
  公众号大事记助手
--------------------------------------------------
  1  一键更新（抓新增 + 抓正文 + 分类 + 出台账）
  2  补历史（往前翻，同样走完整流程）
  3  只抓正文（补上次没抓完的）
  4  只重新分类（改完 rules.yaml 用）
  5  只重新导出台账（不出网，秒级）
  6  查看库内状态
  7  试跑最近 10 篇（规则调参用）
  8  对账补漏（核对后台有没有漏抓）
  9  从台账读回我改过的类别
  0  退出
==================================================
"""


def cmd_menu(cfg, args) -> int:
    while True:
        print(MENU)
        try:
            choice = input("请输入编号：").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "0":
            return 0
        try:
            if choice == "1":
                _one_click(cfg, open_db(cfg), "new")
            elif choice == "2":
                d = input("补抓到哪个日期之前（YYYY-MM-DD，回车则按 config 的 since）：").strip()
                _one_click(cfg, open_db(cfg), "backfill", before=d or None)
            elif choice == "3":
                cmd_body(cfg, argparse.Namespace(limit=None))
            elif choice == "4":
                cmd_classify(cfg, argparse.Namespace(replay=True))
            elif choice == "5":
                cmd_export(cfg, argparse.Namespace(pilot=0))
            elif choice == "6":
                cmd_status(cfg, argparse.Namespace())
            elif choice == "7":
                cmd_pilot(cfg, argparse.Namespace(n=10))
            elif choice == "8":
                cmd_verify(cfg, argparse.Namespace(since=None, max_pages=None))
            elif choice == "9":
                cmd_import(cfg, argparse.Namespace(path=None))
            else:
                print("没有这个编号")
        except login_mod.AuthError as exc:
            print(f"\n[登录失效] {exc}\n请重新运行本程序，按提示扫码。\n")
        except fetch.RateLimited as exc:
            print(f"\n[已熔断] {exc}\n")
        except fetch.FetchError as exc:
            print(f"\n[抓取出错] {exc}\n")
        except Exception as exc:  # 菜单里不中断整个程序
            print(f"\n[错误] {type(exc).__name__}: {exc}\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="公众号大事记助手")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("login", help="扫码登录并把登录态写入 data/auth.json")
    sp.add_argument("--force", action="store_true", help="强制重新扫码")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("list", help="抓取发表记录列表")
    group = sp.add_mutually_exclusive_group()
    group.add_argument("--pilot", type=int, metavar="N", help="只抓最近 N 篇")
    group.add_argument("--full", action="store_true", help="按 since 全量往前翻")
    group.add_argument("--backfill", action="store_true", help="补历史")
    group.add_argument("--new", action="store_true", help="只抓新增（默认）")
    sp.add_argument("--before", metavar="YYYY-MM-DD", help="backfill 的目标日期")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("verify", help="对账补漏：后台有、库里没有的补进来")
    sp.add_argument("--since", metavar="YYYY-MM-DD", help="只对账该日期之后（默认取 config 的 since）")
    sp.add_argument("--max-pages", type=int, default=None, help="单次最多翻几页")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("import-xlsx", help="从台账读回人工改过的类别")
    sp.add_argument("--path", default=None, help="台账路径，默认 out/<brand>大事记台账_年份.xlsx")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("body", help="抓正文")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_body)

    sp = sub.add_parser("classify", help="按规则重新分类")
    sp.add_argument("--replay", action="store_true", help="忽略增量，全部重算")
    sp.set_defaults(func=cmd_classify)

    sp = sub.add_parser("export", help="导出报告")
    sp.add_argument("--pilot", type=int, nargs="?", const=10, default=0)
    sp.add_argument("--pilot-only", action="store_true")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("pilot", help="试跑：抓最近 N 篇并出报告")
    sp.add_argument("--n", type=int, default=10)
    sp.set_defaults(func=cmd_pilot)

    sp = sub.add_parser("status", help="查看库内状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("menu", help="交互菜单")
    sp.set_defaults(func=cmd_menu)

    return p


def main(argv=None) -> int:
    cfg = load_cfg()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        return cmd_menu(cfg, args)
    try:
        return args.func(cfg, args) or 0
    except login_mod.AuthError as exc:
        print(f"\n[登录失效] {exc}")
        return EXIT_AUTH
    except fetch.RateLimited as exc:
        print(f"\n[已熔断] {exc}")
        return EXIT_RATE_LIMITED
    except fetch.FetchError as exc:
        print(f"\n[抓取出错] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断，进度已保存，下次运行会从断点继续。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
