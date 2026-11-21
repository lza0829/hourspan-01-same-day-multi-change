"""same_day_multi_change · 同一业务键同一自然日多份营业属性快照的 Type2 同日链

落库形态（择一·甲）：dim_store_hours_hist 连续多行 —— 同日每次属性 hash 相对前序
变化都关旧开新落一条独立 hist 行，相邻段 valid_to = 下段 valid_from，不重叠不留洞；
same_day_chains / same_day_chain_segments 作为同日链台账挂前后 hist_id 并承载核证。
禁止只留末日一行，禁止原地覆盖合并。

失败闭（择一·甲）：整日多段开闭与当前镜刷新在同一事务内完成，任何一步失败整事务
回滚到变更前；禁止「镜已是末日属性但中间 hist 缺失」，禁止双 is_current。

只读核证：attest_chain() / GET /api/same-day/chain（店、日、段数、各段 valid 区间、
末段 hash）。
"""

import json
from uuid import uuid4

from app.seed import now_iso

CHAIN_PREFIX = "SAMEDAY"


class SameDayGateError(Exception):
    """历史维写闸拦截：不满足同日链开闭前提，拒绝写入。"""


def _day_of(as_of_ts: str) -> str:
    # ISO8601 Z 串前 10 位即自然日 YYYY-MM-DD
    return as_of_ts[:10]


def _fetch_day_snapshots(conn, store_id: str, day: str):
    return conn.execute(
        """SELECT * FROM source_snapshots
           WHERE store_id=? AND substr(as_of_ts, 1, 10)=?
           ORDER BY as_of_ts, id""",
        (store_id, day),
    ).fetchall()


def _write_gate(conn, store_id: str, day: str, snaps) -> dict:
    """历史维写闸：所有前提校验在此，闸不过不落任何 hist / mirror。"""
    store = conn.execute("SELECT id FROM stores WHERE id=?", (store_id,)).fetchone()
    if not store:
        raise SameDayGateError(f"门店不存在: {store_id}")

    if not snaps:
        raise SameDayGateError(f"{store_id} 在 {day} 无贴源快照，无法关旧开新")

    ts_list = [s["as_of_ts"] for s in snaps]
    if len(set(ts_list)) != len(ts_list):
        raise SameDayGateError(
            f"{store_id} {day} 存在相同 as_of_ts 快照，升序链序有歧义，拒绝写入"
        )

    if any(_day_of(ts) != day for ts in ts_list):
        raise SameDayGateError("自然日边界异常，快照 as_of_ts 与入参 day 不一致")

    # 同一店同日只允许一条同日链（UNIQUE 之外的应用层闸）
    existed = conn.execute(
        "SELECT id FROM same_day_chains WHERE store_id=? AND day=?",
        (store_id, day),
    ).fetchone()
    if existed:
        raise SameDayGateError(
            f"{store_id} {day} 同日链已落库（{existed['id']}），禁止重复开闭/覆盖合并"
        )

    baseline = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()
    if not baseline:
        raise SameDayGateError(f"{store_id} 无 is_current 基线历史行，无法关旧")
    if not mirror:
        raise SameDayGateError(f"{store_id} 无当前镜行，无法保证镜与链同事务刷新")
    if mirror["hist_id"] != baseline["id"] or mirror["row_hash"] != baseline["row_hash"]:
        raise SameDayGateError(
            f"{store_id} 当前镜与 is_current 基线不一致，拒绝在脏基线上开同日链"
        )

    current_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
        (store_id,),
    ).fetchone()["c"]
    if current_rows != 1:
        raise SameDayGateError(f"{store_id} is_current 行数={current_rows}，存在双 current，拒绝写入")

    return {"baseline": baseline, "mirror": mirror}


def _plan_segments(snaps, baseline) -> list[dict]:
    """按 as_of 升序挑出 hash 相对前序变化的快照，规划关旧开新段。"""
    plan = []
    prev_hash = baseline["row_hash"]
    for snap in snaps:
        if snap["row_hash"] == prev_hash:
            continue  # 同属性快照不产生新段
        plan.append(
            {
                "snapshot": snap,
                "valid_from": snap["as_of_ts"],
                "row_hash": snap["row_hash"],
            }
        )
        prev_hash = snap["row_hash"]
    if not plan:
        raise SameDayGateError("当日快照属性 hash 相对基线均无变化，无需开段，拒绝空链")
    return plan


def apply_same_day(conn, store_id: str, day: str, *, simulate_fail_after_mirror: bool = False) -> dict:
    """同日链应用：整日多段开闭 + 当前镜刷新，单事务成败一体。

    simulate_fail_after_mirror 仅供失败闭演练：在镜刷新后、提交前抛错，
    触发整事务回滚，并核对回滚后状态与变更前完全一致。
    """
    snaps = _fetch_day_snapshots(conn, store_id, day)
    gate = _write_gate(conn, store_id, day, snaps)
    baseline = gate["baseline"]
    mirror = gate["mirror"]
    plan = _plan_segments(snaps, baseline)

    before = {
        "hist_count": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store_id,)
        ).fetchone()["c"],
        "mirror_hist_id": mirror["hist_id"],
        "mirror_hash": mirror["row_hash"],
        "chain_count": conn.execute(
            "SELECT COUNT(*) AS c FROM same_day_chains WHERE store_id=? AND day=?",
            (store_id, day),
        ).fetchone()["c"],
        "proof_count": conn.execute(
            "SELECT COUNT(*) AS c FROM scd_proofs WHERE store_id=?", (store_id,)
        ).fetchone()["c"],
    }

    now = now_iso()
    chain_id = f"{CHAIN_PREFIX}-{uuid4().hex[:10]}"
    segment_rows = []
    try:
        # —— 同一事务：链台账、hist 多段开闭、镜刷新、核证条 ——
        # 首段关基线，其后每段关上一段刚开出的 hist：逐段关旧开新
        prev_hist_id = baseline["id"]
        last_hist_id = baseline["id"]
        for idx, seg in enumerate(plan, start=1):
            snap = seg["snapshot"]
            seg["prev_hist_id"] = prev_hist_id
            # 关旧：前段（首段为基线）valid_to = 本段 valid_from
            conn.execute(
                """UPDATE dim_store_hours_hist
                      SET valid_to=?, is_current=0, closed_by_run=?
                    WHERE id=?""",
                (seg["valid_from"], chain_id, prev_hist_id),
            )
            new_hist_id = f"HIST-SD-{uuid4().hex[:10]}"
            conn.execute(
                """INSERT INTO dim_store_hours_hist(
                     id, store_id, open_time, close_time, weekday_mask, biz_status,
                     valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_hist_id,
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    seg["valid_from"],
                    None,
                    1,
                    snap["id"],
                    seg["row_hash"],
                    None,
                ),
            )
            seg["hist_id"] = new_hist_id
            seg["segment_no"] = idx
            last_hist_id = new_hist_id
            segment_rows.append(seg)
            prev_hist_id = new_hist_id

        # 段间闭环：相邻段 valid_to = 下段 valid_from（更新中段 hist 行）
        for prev_seg, next_seg in zip(segment_rows, segment_rows[1:]):
            conn.execute(
                "UPDATE dim_store_hours_hist SET valid_to=? WHERE id=?",
                (next_seg["valid_from"], prev_seg["hist_id"]),
            )

        last_snap = segment_rows[-1]["snapshot"]
        # 当前镜只指向该日时间最末段
        conn.execute(
            """INSERT INTO dim_store_hours_current(
                 store_id, open_time, close_time, weekday_mask, biz_status,
                 hist_id, as_of_ts, row_hash
               ) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(store_id) DO UPDATE SET
                 open_time=excluded.open_time,
                 close_time=excluded.close_time,
                 weekday_mask=excluded.weekday_mask,
                 biz_status=excluded.biz_status,
                 hist_id=excluded.hist_id,
                 as_of_ts=excluded.as_of_ts,
                 row_hash=excluded.row_hash
            """,
            (
                store_id,
                last_snap["open_time"],
                last_snap["close_time"],
                last_snap["weekday_mask"],
                last_snap["biz_status"],
                last_hist_id,
                last_snap["as_of_ts"],
                last_snap["row_hash"],
            ),
        )

        # 链台账头
        conn.execute(
            """INSERT INTO same_day_chains(
                 id, store_id, day, first_as_of_ts, last_as_of_ts,
                 segment_count, last_hist_id, last_hash, status, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                chain_id,
                store_id,
                day,
                segment_rows[0]["valid_from"],
                segment_rows[-1]["valid_from"],
                len(segment_rows),
                last_hist_id,
                last_snap["row_hash"],
                "applied",
                now,
            ),
        )
        # 段号(1-based) → 该段 valid_to：下段 valid_from，末段为 None
        valid_to_by_no = {
            seg["segment_no"]: nxt["valid_from"]
            for seg, nxt in zip(segment_rows, segment_rows[1:])
        }
        # 链台账边：挂前后 hist_id
        for seg in segment_rows:
            conn.execute(
                """INSERT INTO same_day_chain_segments(
                     id, chain_id, store_id, segment_no, hist_id, prev_hist_id,
                     source_snapshot_id, valid_from, valid_to, row_hash
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"SDSEG-{uuid4().hex[:10]}",
                    chain_id,
                    store_id,
                    seg["segment_no"],
                    seg["hist_id"],
                    seg["prev_hist_id"],
                    seg["snapshot"]["id"],
                    seg["valid_from"],
                    valid_to_by_no.get(seg["segment_no"]),
                    seg["row_hash"],
                ),
            )

        # 核证条：每段一条，mirror_hist_id 统一记末段（镜只指末段）；
        # 与多段开闭、镜刷新同事务，提交前任何失败整日回滚
        for seg in segment_rows:
            conn.execute(
                """INSERT INTO scd_proofs(
                     store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    store_id,
                    seg["hist_id"],
                    last_hist_id,
                    seg["row_hash"],
                    1,
                    json.dumps(
                        {
                            "same_day_chain": chain_id,
                            "segment_no": seg["segment_no"],
                            "day": day,
                            "is_last_segment": seg["hist_id"] == last_hist_id,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

        # 提交前闸内自检：双 current / 镜末段错位 → 回滚
        current_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
            (store_id,),
        ).fetchone()["c"]
        if current_rows != 1:
            raise SameDayGateError(f"提交前自检失败：is_current 行数={current_rows}")
        mirror_after = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
        ).fetchone()
        if mirror_after["hist_id"] != last_hist_id:
            raise SameDayGateError("提交前自检失败：当前镜未指向末日最末段")

        if simulate_fail_after_mirror:
            raise RuntimeError("演练注入：镜已刷新但提交前失败，须整日回滚")

        conn.commit()
    except Exception:
        conn.rollback()
        after = _snapshot_state(conn, store_id, day)
        rolled_back_clean = after == before
        return {
            "ok": False,
            "rolled_back": True,
            "rolled_back_clean": rolled_back_clean,
            "chain_id": chain_id,
            "store_id": store_id,
            "day": day,
            "before": before,
            "after": after,
            "message": "同日链写入失败，整事务已回滚到变更前"
            if rolled_back_clean
            else "回滚后状态与变更前不一致，需人工介入",
        }

    attestation = attest_chain(conn, store_id, day)
    return {
        "ok": True,
        "rolled_back": False,
        "chain_id": chain_id,
        "store_id": store_id,
        "day": day,
        "segment_count": len(segment_rows),
        "segments": [
            {
                "segment_no": seg["segment_no"],
                "hist_id": seg["hist_id"],
                "prev_hist_id": seg["prev_hist_id"],
                "source_snapshot_id": seg["snapshot"]["id"],
                "valid_from": seg["valid_from"],
                "valid_to": valid_to_by_no.get(seg["segment_no"]),
                "row_hash": seg["row_hash"],
            }
            for seg in segment_rows
        ],
        "mirror": {
            "hist_id": last_hist_id,
            "as_of_ts": last_snap["as_of_ts"],
            "row_hash": last_snap["row_hash"],
        },
        "attestation": attestation,
    }


def _snapshot_state(conn, store_id: str, day: str) -> dict:
    mirror = conn.execute(
        "SELECT hist_id, row_hash FROM dim_store_hours_current WHERE store_id=?",
        (store_id,),
    ).fetchone()
    return {
        "hist_count": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store_id,)
        ).fetchone()["c"],
        "mirror_hist_id": mirror["hist_id"] if mirror else None,
        "mirror_hash": mirror["row_hash"] if mirror else None,
        "chain_count": conn.execute(
            "SELECT COUNT(*) AS c FROM same_day_chains WHERE store_id=? AND day=?",
            (store_id, day),
        ).fetchone()["c"],
        "proof_count": conn.execute(
            "SELECT COUNT(*) AS c FROM scd_proofs WHERE store_id=?", (store_id,)
        ).fetchone()["c"],
    }


def attest_chain(conn, store_id: str, day: str) -> dict:
    """只读核证：店、日、段数、各段 valid 区间、末段 hash 与链/镜一致性。"""
    chain = conn.execute(
        "SELECT * FROM same_day_chains WHERE store_id=? AND day=?",
        (store_id, day),
    ).fetchone()
    if not chain:
        return {
            "store_id": store_id,
            "day": day,
            "found": False,
            "valid": False,
            "violations": ["same_day_chain_not_found"],
            "segment_count": 0,
            "segments": [],
            "last_hash": None,
        }

    seg_rows = conn.execute(
        "SELECT * FROM same_day_chain_segments WHERE chain_id=? ORDER BY segment_no",
        (chain["id"],),
    ).fetchall()
    hist_rows = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
        (store_id,),
    ).fetchall()
    hist_by_id = {h["id"]: h for h in hist_rows}
    mirror = conn.execute(
        "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
    ).fetchone()

    violations = []
    segments = []
    for seg in seg_rows:
        hist = hist_by_id.get(seg["hist_id"])
        item = {
            "segment_no": seg["segment_no"],
            "hist_id": seg["hist_id"],
            "prev_hist_id": seg["prev_hist_id"],
            "source_snapshot_id": seg["source_snapshot_id"],
            "valid_from": seg["valid_from"],
            "valid_to": seg["valid_to"],
            "row_hash": seg["row_hash"],
            "hist_is_current": hist["is_current"] if hist else None,
        }
        segments.append(item)
        if not hist:
            violations.append(f"segment_{seg['segment_no']}_hist_missing")
            continue
        if hist["valid_from"] != seg["valid_from"]:
            violations.append(f"segment_{seg['segment_no']}_valid_from_drift")
        if hist["row_hash"] != seg["row_hash"]:
            violations.append(f"segment_{seg['segment_no']}_hash_drift")

    # 相邻段 valid_to = 下段 valid_from：不重叠不留洞
    for prev, nxt in zip(segments, segments[1:]):
        if prev["valid_to"] != nxt["valid_from"]:
            violations.append(
                f"gap_or_overlap_between_segment_{prev['segment_no']}_{nxt['segment_no']}"
            )
        prev_hist = hist_by_id.get(prev["hist_id"])
        if prev_hist and (prev_hist["is_current"] != 0 or prev_hist["valid_to"] is None):
            violations.append(f"segment_{prev['segment_no']}_not_closed")

    # 首段必须接上基线（prev_hist 闭点 = 首段 valid_from）
    first = segments[0]
    prev_hist = hist_by_id.get(first["prev_hist_id"]) if first["prev_hist_id"] else None
    if not prev_hist:
        violations.append("baseline_hist_missing")
    elif prev_hist["valid_to"] != first["valid_from"] or prev_hist["is_current"] != 0:
        violations.append("baseline_not_closed_onto_first_segment")

    # 末段有两种合法形态：
    #  A. 链仍在生效 —— 末段 is_current=1、valid_to 为空、镜指末段；
    #  B. 链已被后续正常 SCD 开闭接管 —— 末段被关（valid_to 不空、is_current=0），
    #     且闭点与下一历史行 valid_from 严丝合缝，镜指向该行。两种形态之外即违规。
    last = segments[-1]
    last_hist = hist_by_id.get(last["hist_id"])
    current_rows = [h for h in hist_rows if h["is_current"] == 1]
    if len(current_rows) != 1:
        violations.append("double_or_missing_is_current")

    chain_still_open = bool(
        last_hist and last_hist["is_current"] == 1 and last_hist["valid_to"] is None
    )
    chain_superseded = bool(
        last_hist and last_hist["is_current"] == 0 and last_hist["valid_to"] is not None
    )

    if not last_hist:
        violations.append("last_segment_hist_missing")
    elif not (chain_still_open or chain_superseded):
        violations.append("last_segment_state_invalid")

    if chain_still_open:
        if current_rows and current_rows[0]["id"] != last["hist_id"]:
            violations.append("unique_current_is_not_last_segment")
        if not mirror:
            violations.append("mirror_missing")
        elif mirror["hist_id"] != last["hist_id"]:
            violations.append("mirror_not_pointing_last_segment")
        elif mirror["row_hash"] != last["row_hash"]:
            violations.append("mirror_hash_mismatch")
    else:
        # 已被接管：末段闭点 = 下一历史行开点，不重叠不留洞；镜指向接管行
        successor = next(
            (h for h in hist_rows if h["valid_from"] == last_hist["valid_to"]), None
        ) if last_hist else None
        if not successor:
            violations.append("superseded_last_segment_has_no_successor")
        elif current_rows and current_rows[0]["id"] != successor["id"]:
            violations.append("successor_not_unique_current")
        if not mirror:
            violations.append("mirror_missing")
        elif successor and mirror["hist_id"] != successor["id"]:
            violations.append("mirror_not_pointing_successor")

    if chain["segment_count"] != len(segments):
        violations.append("chain_segment_count_drift")
    if chain["last_hist_id"] != last["hist_id"] or chain["last_hash"] != last["row_hash"]:
        violations.append("chain_head_last_segment_drift")

    return {
        "found": True,
        "store_id": store_id,
        "day": day,
        "chain_id": chain["id"],
        "status": chain["status"],
        "segment_count": len(segments),
        "segments": segments,
        "first_as_of_ts": chain["first_as_of_ts"],
        "last_as_of_ts": chain["last_as_of_ts"],
        "last_hist_id": chain["last_hist_id"],
        "last_hash": chain["last_hash"],
        "chain_state": "open" if chain_still_open else ("superseded" if chain_superseded else "invalid"),
        "mirror": dict(mirror) if mirror else None,
        "valid": not violations,
        "violations": violations,
    }


def list_chains(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM same_day_chains ORDER BY day DESC, store_id"
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["attestation"] = attest_chain(conn, r["store_id"], r["day"])
        out.append(item)
    return out
