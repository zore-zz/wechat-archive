# -*- coding: utf-8 -*-
"""SQLite 存取层：建表、迁移与全部 DAO。

约定：除本模块外，其他模块不得直接执行 SQL。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  biz             TEXT    NOT NULL,
  mid             TEXT    NOT NULL,
  idx             INTEGER NOT NULL DEFAULT 1,
  sn              TEXT,
  title           TEXT    NOT NULL DEFAULT '',
  channel         TEXT,
  digest          TEXT,
  author          TEXT,
  account         TEXT,
  publish_ts      INTEGER,
  publish_date    TEXT,
  link            TEXT    NOT NULL,
  cover           TEXT,
  item_cnt        INTEGER NOT NULL DEFAULT 1,
  group_key       TEXT,
  content_text    TEXT,
  content_len     INTEGER NOT NULL DEFAULT 0,
  img_count       INTEGER NOT NULL DEFAULT 0,
  body_status     INTEGER NOT NULL DEFAULT 0,
  body_msg        TEXT,
  body_tries      INTEGER NOT NULL DEFAULT 0,
  body_fetched_at TEXT,
  flags           TEXT,
  first_seen_at   TEXT,
  last_seen_at    TEXT,
  UNIQUE (biz, mid, idx)
);
CREATE INDEX IF NOT EXISTS ix_art_date ON articles(publish_ts DESC);
CREATE INDEX IF NOT EXISTS ix_art_body ON articles(body_status);

CREATE TABLE IF NOT EXISTS list_tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  biz         TEXT    NOT NULL DEFAULT '',
  begin_ts    INTEGER NOT NULL DEFAULT 0,
  end_ts      INTEGER NOT NULL DEFAULT 0,
  next_offset INTEGER NOT NULL DEFAULT 0,
  total_count INTEGER,
  pages_done  INTEGER NOT NULL DEFAULT 0,
  new_items   INTEGER NOT NULL DEFAULT 0,
  status      TEXT    NOT NULL DEFAULT 'running',
  last_ret    INTEGER,
  last_err    TEXT,
  updated_at  TEXT,
  UNIQUE (biz, begin_ts, end_ts)
);

CREATE TABLE IF NOT EXISTS classifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  run_id     INTEGER,
  category   TEXT    NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  score      REAL    NOT NULL DEFAULT 0,
  confidence TEXT    NOT NULL DEFAULT '',
  zone       TEXT    NOT NULL DEFAULT 'normal',
  reason     TEXT,
  matched    TEXT,
  created_at TEXT,
  UNIQUE (article_id, category)
);
CREATE INDEX IF NOT EXISTS ix_cls_cat ON classifications(category, zone);

CREATE TABLE IF NOT EXISTS reviews (
  article_id     INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  decision       TEXT NOT NULL DEFAULT '未复核',
  final_category TEXT,
  event_title    TEXT,
  note           TEXT,
  updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  params      TEXT,
  stats       TEXT,
  note        TEXT
);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn) -> None:
    """给老库补新列，避免重建数据库。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    for col, ddl in (("channel", "ALTER TABLE articles ADD COLUMN channel TEXT"),):
        if col not in cols:
            conn.execute(ddl)


# ---------------- 文章 ----------------

def article_exists(conn, biz: str, mid: str, idx: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM articles WHERE biz=? AND mid=? AND idx=? LIMIT 1",
        (str(biz), str(mid), int(idx)),
    ).fetchone()
    return row is not None


def upsert_article(conn, rec: dict) -> bool:
    """返回 True 表示新文章。已存在则只刷新标题/摘要/链接与最后见到时间。"""
    exists = article_exists(conn, rec["biz"], rec["mid"], rec["idx"])
    ts = now_str()
    if exists:
        conn.execute(
            """UPDATE articles SET title=?, channel=?, digest=?, link=?, cover=?,
                   publish_ts=?, publish_date=?, last_seen_at=?
               WHERE biz=? AND mid=? AND idx=?""",
            (
                rec.get("title", ""), rec.get("channel"), rec.get("digest"),
                rec.get("link", ""),
                rec.get("cover"), rec.get("publish_ts"), rec.get("publish_date"), ts,
                str(rec["biz"]), str(rec["mid"]), int(rec["idx"]),
            ),
        )
        return False
    conn.execute(
        """INSERT INTO articles
           (biz, mid, idx, sn, title, channel, digest, author, account, publish_ts,
            publish_date, link, cover, group_key, flags, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(rec["biz"]), str(rec["mid"]), int(rec["idx"]), rec.get("sn"),
            rec.get("title", ""), rec.get("channel"), rec.get("digest"),
            rec.get("author"),
            rec.get("account"), rec.get("publish_ts"), rec.get("publish_date"),
            rec.get("link", ""), rec.get("cover"), rec.get("group_key"),
            rec.get("flags"), ts, ts,
        ),
    )
    return True


def refresh_item_cnt(conn) -> None:
    conn.execute(
        """UPDATE articles SET item_cnt = (
               SELECT COUNT(*) FROM articles b
               WHERE b.biz = articles.biz AND b.mid = articles.mid)"""
    )


def add_flag(conn, article_id: int, flag: str) -> None:
    row = conn.execute("SELECT flags FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row:
        return
    flags = [f for f in (row["flags"] or "").split(",") if f]
    if flag in flags:
        return
    flags.append(flag)
    conn.execute("UPDATE articles SET flags=? WHERE id=?", (",".join(flags), article_id))


def get_articles(conn, since=None, until=None, limit=None,
                 order: str = "DESC", only_ids=None):
    sql = "SELECT * FROM articles WHERE 1=1"
    args: list = []
    if since:
        sql += " AND publish_ts >= ?"
        args.append(int(since))
    if until:
        sql += " AND publish_ts <= ?"
        args.append(int(until))
    if only_ids:
        placeholders = ",".join("?" * len(only_ids))
        sql += f" AND id IN ({placeholders})"
        args.extend(only_ids)
    sql += f" ORDER BY publish_ts {order}, id {order}"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def pending_body(conn, limit: int):
    return conn.execute(
        """SELECT * FROM articles
           WHERE body_status IN (0, 4) AND body_tries < 3
           ORDER BY publish_ts DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()


def save_body(conn, article_id: int, status: int, text: str | None = None,
              msg: str | None = None, img_count: int = 0,
              publish_ts: int | None = None) -> None:
    text = text or ""
    if status == 1:
        conn.execute(
            """UPDATE articles SET content_text=?, content_len=?, img_count=?,
                   body_status=1, body_msg=?, body_fetched_at=?,
                   body_tries=body_tries+1,
                   publish_ts=COALESCE(?, publish_ts) WHERE id=?""",
            (text, len("".join(text.split())), img_count, msg, now_str(),
             publish_ts, article_id),
        )
    elif status == 3:  # 环境异常不消耗重试次数
        conn.execute(
            "UPDATE articles SET body_status=3, body_msg=? WHERE id=?",
            (msg, article_id),
        )
    else:
        conn.execute(
            """UPDATE articles SET body_status=?, body_msg=?, body_fetched_at=?,
                   body_tries=body_tries+1 WHERE id=?""",
            (status, msg, now_str(), article_id),
        )


def mark_skip(conn, article_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE articles SET body_status=5, body_msg=? WHERE id=?",
        (reason, article_id),
    )


def count_body_today(conn) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM articles
           WHERE body_status=1 AND substr(body_fetched_at,1,10)=date('now','localtime')"""
    ).fetchone()
    return int(row["n"] or 0)


def get_primary_category(conn, article_id: int) -> str:
    row = conn.execute(
        "SELECT category FROM classifications WHERE article_id=? AND is_primary=1 LIMIT 1",
        (article_id,),
    ).fetchone()
    return row["category"] if row else ""


# ---------------- 列表任务 ----------------

def get_task(conn, biz: str, begin_ts: int, end_ts: int):
    return conn.execute(
        "SELECT * FROM list_tasks WHERE biz=? AND begin_ts=? AND end_ts=?",
        (biz, int(begin_ts), int(end_ts)),
    ).fetchone()


def ensure_task(conn, biz: str, begin_ts: int = 0, end_ts: int = 0):
    row = get_task(conn, biz, begin_ts, end_ts)
    if row:
        return row
    conn.execute(
        """INSERT INTO list_tasks (biz, begin_ts, end_ts, updated_at)
           VALUES (?,?,?,?)""",
        (biz, int(begin_ts), int(end_ts), now_str()),
    )
    conn.commit()
    return get_task(conn, biz, begin_ts, end_ts)


def update_task(conn, task_id: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    conn.execute(
        f"UPDATE list_tasks SET {cols}, updated_at=? WHERE id=?",
        list(kw.values()) + [now_str(), task_id],
    )


# ---------------- 分类与复核 ----------------

def replace_classifications(conn, article_id: int, rows: list[dict],
                            run_id: int | None = None) -> None:
    conn.execute("DELETE FROM classifications WHERE article_id=?", (article_id,))
    ts = now_str()
    for r in rows:
        conn.execute(
            """INSERT OR REPLACE INTO classifications
               (article_id, run_id, category, is_primary, score, confidence,
                zone, reason, matched, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                article_id, run_id, r["category"], int(r.get("is_primary", 0)),
                float(r.get("score", 0)), r.get("confidence", ""),
                r.get("zone", "normal"), r.get("reason"), r.get("matched"), ts,
            ),
        )


def clear_classifications(conn) -> None:
    conn.execute("DELETE FROM classifications")


def articles_for_export(conn, limit=None, since: str | None = None,
                        until: str | None = None):
    sql = """
    SELECT a.*,
           p.category  AS cat_id,
           p.score     AS score,
           p.confidence AS confidence,
           p.zone      AS zone,
           p.reason    AS reason,
           p.matched   AS matched,
           rv.decision    AS decision,
           rv.event_title AS event_title,
           rv.note        AS review_note,
           (SELECT GROUP_CONCAT(c2.category, ',')
              FROM classifications c2
             WHERE c2.article_id = a.id AND c2.zone != 'reject') AS all_cats
    FROM articles a
    LEFT JOIN classifications p ON p.article_id = a.id AND p.is_primary = 1
    LEFT JOIN reviews rv ON rv.article_id = a.id
    WHERE 1=1
    """
    params: list = []
    if since:
        sql += " AND a.publish_date >= ?"
        params.append(since)
    if until:
        sql += " AND a.publish_date <= ?"
        params.append(until)
    sql += " ORDER BY a.publish_ts DESC, a.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def get_review(conn, article_id: int):
    return conn.execute("SELECT * FROM reviews WHERE article_id=?", (article_id,)).fetchone()


def set_review(conn, article_id: int, decision: str,
               event_title: str | None = None, note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO reviews (article_id, decision, event_title, note, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(article_id) DO UPDATE SET
             decision=excluded.decision,
             event_title=COALESCE(excluded.event_title, reviews.event_title),
             note=COALESCE(excluded.note, reviews.note),
             updated_at=excluded.updated_at""",
        (article_id, decision, event_title, note, now_str()),
    )


# ---------------- 运行记录与统计 ----------------

def start_run(conn, kind: str, params: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, started_at, params) VALUES (?,?,?)",
        (kind, now_str(), params),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn, run_id: int, stats: str = "", note: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats=?, note=? WHERE id=?",
        (now_str(), stats, note, run_id),
    )
    conn.commit()


def stats(conn) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN body_status=1 THEN 1 ELSE 0 END) AS body_ok,
                  SUM(CASE WHEN body_status=0 THEN 1 ELSE 0 END) AS body_todo,
                  SUM(CASE WHEN body_status=2 THEN 1 ELSE 0 END) AS deleted,
                  SUM(CASE WHEN body_status=3 THEN 1 ELSE 0 END) AS blocked,
                  SUM(CASE WHEN body_status=4 THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN body_status=5 THEN 1 ELSE 0 END) AS skipped,
                  MIN(publish_date) AS first_date,
                  MAX(publish_date) AS last_date
           FROM articles"""
    ).fetchone()
    return dict(row) if row else {}


def category_counts(conn) -> dict:
    rows = conn.execute(
        """SELECT category, COUNT(*) AS n FROM classifications
           WHERE is_primary=1 GROUP BY category ORDER BY n DESC"""
    ).fetchall()
    return {r["category"]: int(r["n"]) for r in rows}
