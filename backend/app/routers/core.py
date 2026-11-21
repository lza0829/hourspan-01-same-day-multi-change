from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules.same_day_multi_change import SameDayGateError, apply_same_day, attest_chain, list_chains
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    run_id = body.get("detect_run_id")
    if not run_id:
        raise HTTPException(400, "需要 detect_run_id")
    return scd.apply_type2(run_id)


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.post("/same-day/apply")
def same_day_apply(body: dict = Body(...)):
    """同日链应用：整日多段开闭 + 当前镜刷新，单事务成败一体（失败闭·甲）。"""
    store_id = (body.get("store_id") or "").strip()
    day = (body.get("day") or "").strip()
    if not store_id or not day:
        raise HTTPException(400, "需要 store_id 与 day(YYYY-MM-DD)")
    simulate_fail = bool(body.get("simulate_fail_after_mirror"))
    conn = connect()
    try:
        result = apply_same_day(
            conn, store_id, day, simulate_fail_after_mirror=simulate_fail
        )
        if not result["ok"]:
            # 失败闭演练：已整日回滚，返回 409 + 回滚前后对照供核证页观测
            raise HTTPException(409, result)
        return result
    except SameDayGateError as exc:
        conn.rollback()
        raise HTTPException(400, {"ok": False, "gate": str(exc)})
    finally:
        conn.close()


@router.get("/same-day/chain")
def same_day_chain(store_id: str, day: str):
    """只读核证接口：店、日、段数、各段 valid 区间、末段 hash。"""
    conn = connect()
    try:
        return attest_chain(conn, store_id, day)
    finally:
        conn.close()


@router.get("/same-day/chains")
def same_day_chains():
    conn = connect()
    try:
        return {"chains": list_chains(conn)}
    finally:
        conn.close()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}
