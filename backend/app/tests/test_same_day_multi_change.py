"""same_day_multi_change 同库 stdlib 测试（不引第三方依赖）。

运行：
  cd backend
  DATA_DIR=<tmp> python -m unittest app.tests.test_same_day_multi_change -v
"""

import json
import os
import tempfile
import unittest
from uuid import uuid4

_tmp = tempfile.mkdtemp(prefix="hourspan-test-")
os.environ["DATA_DIR"] = _tmp
_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "..", "fixtures")
os.environ.setdefault("FIXTURES_DIR", os.path.abspath(_FIXTURES))

from app.db import connect  # noqa: E402
from app.modules.same_day_multi_change import (  # noqa: E402
    SameDayGateError,
    apply_same_day,
    attest_chain,
)
from app.seed import init_db, row_hash  # noqa: E402
from app.services.ingest import run_batch  # noqa: E402

DAY = "2026-11-16"


class SameDayMultiChangeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        run_batch("same_day_multi_change_batch.json")

    def setUp(self):
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    # ---- 夹具 1：ST-EAST-02 同日三变（含一条 hash 相同 no-op）----
    def test_01_east02_three_segments_chain(self):
        res = apply_same_day(self.conn, "ST-EAST-02", DAY)
        self.assertTrue(res["ok"], res)
        # 4 条快照里 1 条属性未变 → 3 段
        self.assertEqual(res["segment_count"], 3)
        self.assertEqual([s["segment_no"] for s in res["segments"]], [1, 2, 3])

        hist = self.conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
            ("ST-EAST-02",),
        ).fetchall()
        # 基线 1 行 + 同日 3 段 = 4 行；禁止只留末日一行
        self.assertEqual(len(hist), 4)

        # 相邻段 valid_to = 下段 valid_from：不重叠不留洞
        base, s1, s2, s3 = hist
        self.assertEqual(base["valid_to"], s1["valid_from"])
        self.assertEqual(s1["valid_to"], s2["valid_from"])
        self.assertEqual(s2["valid_to"], s3["valid_from"])
        self.assertIsNone(s3["valid_to"])
        self.assertEqual([h["is_current"] for h in hist], [0, 0, 0, 1])

        # 中间段必须数得出来，且属性各自不同
        self.assertEqual(s1["source_snapshot_id"], "SNAP-SD-EAST02-01")
        self.assertEqual(s2["source_snapshot_id"], "SNAP-SD-EAST02-03")
        self.assertEqual(s3["source_snapshot_id"], "SNAP-SD-EAST02-04")
        self.assertNotEqual(s1["row_hash"], s2["row_hash"])
        self.assertNotEqual(s2["row_hash"], s3["row_hash"])

        # 当前镜只指向该日时间最末段
        mirror = self.conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", ("ST-EAST-02",)
        ).fetchone()
        self.assertEqual(mirror["hist_id"], s3["id"])
        self.assertEqual(mirror["as_of_ts"], "2026-11-16T18:45:00Z")
        self.assertEqual(mirror["row_hash"], s3["row_hash"])

        # 禁止双 is_current
        cur_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist "
            "WHERE store_id=? AND is_current=1",
            ("ST-EAST-02",),
        ).fetchone()["c"]
        self.assertEqual(cur_n, 1)

    def test_02_east02_attestation_readonly(self):
        att = attest_chain(self.conn, "ST-EAST-02", DAY)
        self.assertTrue(att["found"])
        self.assertEqual(att["segment_count"], 3)
        self.assertEqual(att["last_hist_id"], att["segments"][-1]["hist_id"])
        self.assertEqual(att["last_hash"], att["segments"][-1]["row_hash"])
        self.assertTrue(att["valid"], att["violations"])
        self.assertEqual(att["violations"], [])
        # 段区间字段齐备
        for seg in att["segments"]:
            self.assertTrue(seg["valid_from"])
            self.assertIn("valid_to", seg)
            self.assertTrue(seg["row_hash"])

    # ---- 写闸：同一店同日只允许一条链，禁止覆盖合并 ----
    def test_03_duplicate_apply_gate(self):
        with self.assertRaises(SameDayGateError):
            apply_same_day(self.conn, "ST-EAST-02", DAY)

    # ---- 夹具 2：ST-WEST-01 同日两变 ----
    def test_04_west01_two_segments(self):
        res = apply_same_day(self.conn, "ST-WEST-01", DAY)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["segment_count"], 2)
        hist = self.conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
            ("ST-WEST-01",),
        ).fetchall()
        self.assertEqual(len(hist), 3)
        base, s1, s2 = hist
        self.assertEqual(base["valid_to"], s1["valid_from"])
        self.assertEqual(s1["valid_to"], s2["valid_from"])
        self.assertIsNone(s2["valid_to"])
        self.assertEqual([h["is_current"] for h in hist], [0, 0, 1])
        att = attest_chain(self.conn, "ST-WEST-01", DAY)
        self.assertTrue(att["valid"], att["violations"])

    # ---- 写闸：无当日快照 / 未知门店 ----
    def test_05_gate_missing_snapshot_and_store(self):
        with self.assertRaises(SameDayGateError):
            apply_same_day(self.conn, "ST-WEST-02", DAY)  # 当日无快照
        with self.assertRaises(SameDayGateError):
            apply_same_day(self.conn, "ST-NOPE-99", DAY)

    # ---- 写闸：相同 as_of_ts 链序歧义 ----
    def test_06_gate_duplicate_as_of_ts(self):
        sid = "SNAP-DUP-" + uuid4().hex[:6]
        # 与夹具 SNAP-SD-NORTH01-01 同一 as_of_ts，制造链序歧义
        self.conn.execute(
            """INSERT INTO source_snapshots(
                 id, batch_id, store_id, as_of_ts, open_time, close_time,
                 weekday_mask, biz_status, payload_json, row_hash
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, "B-DUP", "ST-NORTH-01", f"{DAY}T09:30:00Z",
                "10:00", "19:00", "1111100", "open", "{}",
                row_hash("10:00", "19:00", "1111100", "open"),
            ),
        )
        with self.assertRaises(SameDayGateError):
            apply_same_day(self.conn, "ST-NORTH-01", DAY)
        # 闸不过：无链、无新 hist
        chain_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM same_day_chains WHERE store_id=? AND day=?",
            ("ST-NORTH-01", DAY),
        ).fetchone()["c"]
        self.assertEqual(chain_n, 0)
        self.conn.rollback()

    # ---- 写闸：hash 相对基线全不变 → 拒绝空链，不落任何行 ----
    def test_07_gate_all_hash_equal_noop(self):
        before_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?",
            ("ST-WEST-02",),
        ).fetchone()["c"]
        base = self.conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
            ("ST-WEST-02",),
        ).fetchone()
        for i, ts in enumerate(("09:00:00Z", "14:00:00Z")):
            self.conn.execute(
                """INSERT INTO source_snapshots(
                     id, batch_id, store_id, as_of_ts, open_time, close_time,
                     weekday_mask, biz_status, payload_json, row_hash
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"SNAP-NOOP-{i}", "B-NOOP", "ST-WEST-02", f"{DAY}T{ts}",
                    base["open_time"], base["close_time"],
                    base["weekday_mask"], base["biz_status"], "{}", base["row_hash"],
                ),
            )
        with self.assertRaises(SameDayGateError):
            apply_same_day(self.conn, "ST-WEST-02", DAY)
        after_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?",
            ("ST-WEST-02",),
        ).fetchone()["c"]
        self.assertEqual(before_n, after_n)
        self.conn.rollback()

    # ---- 失败闭：半截失败整日回滚到变更前 ----
    def test_08_fail_rolls_back_whole_day(self):
        store = "ST-NORTH-01"
        before_mirror = self.conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store,)
        ).fetchone()
        before_hist_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store,)
        ).fetchone()["c"]
        before_chain_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM same_day_chains WHERE store_id=? AND day=?",
            (store, DAY),
        ).fetchone()["c"]

        res = apply_same_day(self.conn, store, DAY, simulate_fail_after_mirror=True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertTrue(res["rolled_back_clean"], json.dumps(res, ensure_ascii=False))

        # 镜不能已是末日属性而中间 hist 缺失
        after_mirror = self.conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store,)
        ).fetchone()
        self.assertEqual(dict(after_mirror), dict(before_mirror))
        after_hist_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id=?", (store,)
        ).fetchone()["c"]
        self.assertEqual(after_hist_n, before_hist_n)
        after_chain_n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM same_day_chains WHERE store_id=? AND day=?",
            (store, DAY),
        ).fetchone()["c"]
        self.assertEqual(after_chain_n, before_chain_n)
        # 回滚后该日无核证链
        att = attest_chain(self.conn, store, DAY)
        self.assertFalse(att["found"])

        # 回滚后可干净重放（同一事务路径仍可用）
        again = apply_same_day(self.conn, store, DAY)
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["segment_count"], 1)

    # ---- 链被后续正常 SCD 开闭接管后，历史链核证仍须有效（形态 B）----
    def test_09_chain_remains_valid_after_supersede(self):
        from app.services import scd

        later_snap = "SNAP-LATER-WEST01"
        later_ts = "2026-11-20T00:00:00Z"
        rh = row_hash("07:00", "22:00", "1111100", "open")
        self.conn.execute(
            """INSERT INTO source_snapshots(
                 id, batch_id, store_id, as_of_ts, open_time, close_time,
                 weekday_mask, biz_status, payload_json, row_hash
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (later_snap, "B-LATER", "ST-WEST-01", later_ts,
             "07:00", "22:00", "1111100", "open", "{}", rh),
        )
        run_id = "DET-TEST-LATER"
        self.conn.execute(
            "INSERT INTO detect_runs(id, batch_id, created_at, changed_count, report_json) "
            "VALUES (?,?,?,?,?)",
            (run_id, "B-LATER", "2026-11-20T01:00:00Z", 1,
             json.dumps({"changes": [
                 {"store_id": "ST-WEST-01", "kind": "attr_change",
                  "snapshot_id": later_snap, "from_hash": None, "to_hash": rh}
             ]})),
        )
        self.conn.commit()

        out = scd.apply_type2(run_id)  # 走底座正常 Type2 路径，接管链末段
        self.assertTrue(out["ok"], out)

        att = attest_chain(self.conn, "ST-WEST-01", DAY)
        self.assertTrue(att["valid"], att["violations"])
        self.assertEqual(att["chain_state"], "superseded")
        self.assertEqual(att["segment_count"], 2)
        # 中间段仍在，区间不重叠不留洞
        self.assertEqual(att["segments"][0]["valid_to"], att["segments"][1]["valid_from"])
        last_hist = self.conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?",
            (att["last_hist_id"],),
        ).fetchone()
        self.assertEqual(last_hist["is_current"], 0)
        self.assertIsNotNone(last_hist["valid_to"])
        # 镜指向接管行，而非已关闭的链末段
        mirror = self.conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", ("ST-WEST-01",)
        ).fetchone()
        self.assertNotEqual(mirror["hist_id"], att["last_hist_id"])
        cur = self.conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (mirror["hist_id"],)
        ).fetchone()
        self.assertEqual(cur["is_current"], 1)
        self.assertEqual(cur["valid_from"], last_hist["valid_to"])


if __name__ == "__main__":
    unittest.main()
