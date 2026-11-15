# -*- coding: utf-8 -*-
"""删除选购记录：日志、AgentState 与会话归属一起清理，跨买家与执行中被拒绝。"""
import time
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.session.ports.session_store import SessionNotFound, SessionOwnerMismatch
from app.infrastructure.ag_ui_journal import AGUIJournal, JournalConflict, JournalForbidden, JournalNotFound
from app.infrastructure.persistence.sql.session_store import SqlFencedSessionStore
from app.presentation.ag_ui import register_ag_ui_routes


def body(run="r1", session="s1", buyer="b1", query="查询旅行背包"):
    return {"threadId": session, "runId": run, "messages": [{"id": f"u-{run}", "role": "user", "content": query}],
            "state": {}, "tools": [], "context": [], "forwardedProps": {"buyerId": buyer}}


async def finished_run(journal, run="r1", session="s1"):
    await journal.reserve(body(run=run, session=session), "b1", "owner")
    await journal.append(run, "owner", [
        {"type": "RUN_STARTED", "threadId": session, "runId": run},
        {"type": "TEXT_MESSAGE_START", "messageId": f"a-{run}", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": f"a-{run}", "delta": "已找到旅行背包"},
        {"type": "TEXT_MESSAGE_END", "messageId": f"a-{run}"},
        {"type": "RUN_FINISHED", "threadId": session, "runId": run},
    ])


@pytest.fixture
async def journal(tmp_path):
    return AGUIJournal(tmp_path / "runs.db")


@pytest.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'session-state.db'}")
    session_store = SqlFencedSessionStore(engine)
    yield session_store
    await engine.dispose()


async def count(journal, sql, params=()):
    async with aiosqlite.connect(journal.path) as db:
        async with db.execute(sql, params) as cursor:
            return (await cursor.fetchone())[0]


async def test_delete_session_removes_runs_events_and_keeps_other_sessions(journal):
    await finished_run(journal, "r1", "s1")
    await finished_run(journal, "r2", "s2")
    result = await journal.delete_session("s1", "b1")
    assert result == {"sessionId": "s1", "runs": 1}
    assert [item["id"] for item in await journal.sessions("b1")] == ["s2"]
    assert await count(journal, "SELECT COUNT(*) FROM agui_runs WHERE session_id='s1'") == 0
    assert await count(journal, "SELECT COUNT(*) FROM agui_events WHERE run_id='r1'") == 0
    # 另一段会话的事件不能被同一条子查询误删（RUN_STARTED/START/CONTENT/END/FINISHED）。
    assert await count(journal, "SELECT COUNT(*) FROM agui_events WHERE run_id='r2'") == 5
    with pytest.raises(JournalNotFound):
        await journal.session("s1", "b1")


async def test_delete_session_rejects_running_and_foreign_buyer(journal):
    await journal.reserve(body(), "b1", "owner")
    with pytest.raises(JournalConflict):
        await journal.delete_session("s1", "b1")
    assert [item["id"] for item in await journal.sessions("b1")] == ["s1"]
    with pytest.raises(JournalForbidden):
        await journal.delete_session("s1", "other")
    with pytest.raises(JournalNotFound):
        await journal.delete_session("missing", "b1")


async def test_delete_session_discards_pending_learning_jobs(journal):
    await finished_run(journal)
    now = time.time()
    async with aiosqlite.connect(journal.path) as db:
        for job_id, status in (("job-pending", "pending"), ("job-processed", "processed")):
            await db.execute(
                "INSERT INTO learning_jobs(id,source_kind,source_ref,buyer_id,session_id,source_hash,"
                "payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, "agui", "r1", "b1", "s1", "hash", "{}", status, now, now))
        await db.commit()
    await journal.delete_session("s1", "b1")
    assert await count(journal, "SELECT COUNT(*) FROM learning_jobs WHERE id='job-pending'") == 0
    assert await count(journal, "SELECT COUNT(*) FROM learning_jobs WHERE id='job-processed'") == 1


async def test_session_store_delete_removes_snapshot_and_fencing(store):
    claim = await store.claim("s1", buyer_id="b1")
    await store.save_claim(claim, '{"summary":"旧上下文"}')
    assert await store.load("s1") == '{"summary":"旧上下文"}'
    with pytest.raises(SessionOwnerMismatch):
        await store.delete("s1", "buyer-other")
    assert await store.load("s1") == '{"summary":"旧上下文"}'
    await store.delete("s1", "b1")
    assert await store.load("s1") is None
    with pytest.raises(SessionNotFound):
        await store.delete("s1", "b1")


@pytest.fixture
async def api(journal, store):
    app = FastAPI()
    runtime = SimpleNamespace(journal=journal)
    register_ag_ui_routes(app, lambda: SimpleNamespace(), get_runtime=lambda: runtime)
    app.state.session_store = store
    app.state.session_owner_binding = True
    await finished_run(journal)
    await store.assert_owner("s1", "b1", create=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_http_delete_clears_journal_and_session_state(api, journal, store):
    response = await api.delete("/commerce/ag-ui/sessions/s1", params={"buyer_id": "b1"})
    assert response.status_code == 200, response.text
    assert response.json() == {"sessionId": "s1", "runs": 1, "deleted": True, "contextCleared": True}
    assert (await api.get("/commerce/ag-ui/sessions", params={"buyer_id": "b1"})).json() == {"sessions": []}
    assert await store.load("s1") is None
    again = await api.delete("/commerce/ag-ui/sessions/s1", params={"buyer_id": "b1"})
    assert again.status_code == 404


async def test_http_delete_is_scoped_to_the_owning_buyer(api, journal):
    other = await api.delete("/commerce/ag-ui/sessions/s1", params={"buyer_id": "b2"})
    assert other.status_code == 403
    assert [item["id"] for item in await journal.sessions("b1")] == ["s1"]


async def test_http_delete_allows_legacy_session_without_owner_binding(api, journal, store):
    # 旧会话可能只在日志目录里有归属，没有 AgentState 绑定；它仍应可删除。
    await store.delete("s1", "b1")
    response = await api.delete("/commerce/ag-ui/sessions/s1", params={"buyer_id": "b1"})
    assert response.status_code == 200, response.text
    assert response.json()["contextCleared"] is False
    assert [item["id"] for item in await journal.sessions("b1")] == []


async def test_http_delete_refuses_running_session(api, journal):
    await journal.reserve(body(run="r2"), "b1", "owner2")
    response = await api.delete("/commerce/ag-ui/sessions/s1", params={"buyer_id": "b1"})
    assert response.status_code == 409
    assert [item["id"] for item in await journal.sessions("b1")] == ["s1"]


async def test_http_batch_delete_removes_multiple_sessions(api, journal, store):
    await finished_run(journal, "r2", "s2")
    await store.assert_owner("s2", "b1", create=True)
    response = await api.post("/commerce/ag-ui/sessions/batch-delete",
                              params={"buyer_id": "b1"}, json={"session_ids": ["s1", "s2"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deletedCount"] == 2
    assert sorted(payload["deleted"]) == ["s1", "s2"]
    assert payload["failed"] == []
    assert (await api.get("/commerce/ag-ui/sessions", params={"buyer_id": "b1"})).json() == {"sessions": []}
    assert await store.load("s1") is None


async def test_http_batch_delete_treats_missing_session_as_deleted(api, journal):
    # 浏览器缓存可能残留服务端已清理的 ID，重复删除按成功计，用户不必逐条排查。
    response = await api.post("/commerce/ag-ui/sessions/batch-delete",
                              params={"buyer_id": "b1"}, json={"session_ids": ["s1", "nope"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert sorted(payload["deleted"]) == ["nope", "s1"]
    assert payload["failed"] == []


async def test_http_batch_delete_continues_when_one_session_is_running(api, journal):
    await finished_run(journal, "r2", "s2")
    await journal.reserve(body(run="r3", session="s2"), "b1", "owner3")
    response = await api.post("/commerce/ag-ui/sessions/batch-delete",
                              params={"buyer_id": "b1"}, json={"session_ids": ["s1", "s2"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted"] == ["s1"]
    assert payload["failed"] == [{"sessionId": "s2", "reason": "running"}]
    assert [item["id"] for item in await journal.sessions("b1")] == ["s2"]


async def test_http_batch_delete_rejects_foreign_session_only(api, journal):
    await journal.reserve(body(run="r2", session="s2", buyer="b2"), "b2", "owner2")
    response = await api.post("/commerce/ag-ui/sessions/batch-delete",
                              params={"buyer_id": "b1"}, json={"session_ids": ["s1", "s2"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deleted"] == ["s1"]
    assert payload["failed"] == [{"sessionId": "s2", "reason": "forbidden"}]
    assert [item["id"] for item in await journal.sessions("b2")] == ["s2"]


async def test_http_batch_delete_requires_non_empty_selection(api):
    empty = await api.post("/commerce/ag-ui/sessions/batch-delete",
                           params={"buyer_id": "b1"}, json={"session_ids": []})
    assert empty.status_code == 422
