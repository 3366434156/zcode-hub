"""ZCode 会话库只读查询（会话监控页的数据源）。"""

from __future__ import annotations

import sqlite3

import pytest

from app import zsession


def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table session (
          id text primary key, title text, directory text,
          time_created integer, time_updated integer, last_outcome text,
          time_deleted integer
        );
        create table message (
          id text primary key, session_id text, time_created integer,
          time_updated integer, data text, sequence integer
        );
        """
    )
    for _i, row in enumerate(rows):
        con.execute(
            "insert into session(id,title,directory,time_created,time_updated,last_outcome,time_deleted)"
            " values (?,?,?,?,?,?,?)",
            (row["id"], row.get("title"), row.get("directory"), 0,
             row.get("updated", 0), row.get("outcome"), row.get("deleted")),
        )
        for n in range(row.get("messages", 0)):
            con.execute(
                "insert into message(id,session_id,time_created,time_updated,data,sequence)"
                " values (?,?,?,?,?,?)",
                (f"{row['id']}-m{n}", row["id"], 0, 0,
                 '{"role":"assistant","finish":"stop","time":{"completed":1000},'
                 '"tokens":{"total":130,"input":123,"output":7}}', n))
    con.commit()
    con.close()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    _make_db(path, [
        {"id": "s-old", "title": "旧对话", "directory": r"E:\proj\a",
         "updated": 1000, "outcome": "completed", "messages": 2},
        {"id": "s-new", "title": "新对话", "directory": r"E:\proj\a",
         "updated": 3000, "outcome": "error", "messages": 5},
        {"id": "s-other", "title": "别的项目", "directory": r"E:\proj\b",
         "updated": 2000, "outcome": "", "messages": 1},
        {"id": "s-del", "title": "已删除", "directory": r"E:\proj\a",
         "updated": 4000, "deleted": 1},
    ])
    monkeypatch.setattr(zsession, "DB_PATH", path)
    return path


def test_orders_by_updated_and_skips_deleted(db):
    rows = zsession.list_sessions(limit=10)
    assert [r["id"] for r in rows] == ["s-new", "s-other", "s-old"]
    assert rows[0]["messages"] == 5
    assert rows[0]["outcome"] == "error"
    assert rows[0]["updated_at"] == 3.0, "毫秒时间戳归一成秒"


def test_query_filters_title_and_directory(db):
    assert [r["id"] for r in zsession.list_sessions(q="新对话")] == ["s-new"]
    assert [r["id"] for r in zsession.list_sessions(q="proj\\b")] == ["s-other"]


def test_limit_is_bounded(db):
    assert len(zsession.list_sessions(limit=1)) == 1
    assert len(zsession.list_sessions(limit=9999)) == 3


def test_missing_db_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(zsession, "DB_PATH", tmp_path / "nope.sqlite")
    with pytest.raises(FileNotFoundError):
        zsession.list_sessions()


def test_get_session_and_last_message(db):
    sess = zsession.get_session("s-new")
    assert sess["directory"] == r"E:\proj\a"
    assert sess["updated_at"] == 3.0, "会话 time_updated 毫秒转秒"
    last = zsession.last_message("s-new")
    assert last["role"] == "assistant"
    assert last["finish"] == "stop"
    assert last["completed_at"] == 1.0
    assert last["tokens"]["input"] == 123
    assert last["tokens"]["output"] == 7
    assert zsession.get_session("nope") is None
