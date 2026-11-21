import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function SameDay() {
  const [chains, setChains] = useState([]);
  const [storeId, setStoreId] = useState("ST-EAST-02");
  const [day, setDay] = useState("2026-11-16");
  const [att, setAtt] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reloadChains = () =>
    api("/api/same-day/chains")
      .then((r) => setChains(r.chains || []))
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reloadChains(); }, []);

  const attest = () => {
    setErr("");
    setMsg("");
    const q = `?store_id=${encodeURIComponent(storeId.trim())}&day=${encodeURIComponent(day.trim())}`;
    return api(`/api/same-day/chain${q}`)
      .then(setAtt)
      .catch((e) => setErr(String(e.message || e)));
  };

  const apply = async (simulateFail = false) => {
    setErr("");
    setMsg("");
    try {
      const r = await api("/api/same-day/apply", {
        method: "POST",
        body: JSON.stringify({
          store_id: storeId.trim(),
          day: day.trim(),
          simulate_fail_after_mirror: simulateFail,
        }),
      });
      setMsg(JSON.stringify(r, null, 2));
    } catch (e) {
      // 409 = 失败闭演练已整日回滚；400 = 写闸拦截，均需可观测
      setMsg(String(e.message || e));
    }
    await Promise.all([attest(), reloadChains()]);
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>同日多段链</h1>
          <p>
            同店同日多份快照按 as_of 升序关旧开新：hist 连续多行，相邻段
            valid_to = 下段 valid_from，当前镜只指末段；整日开闭与镜刷新同事务。
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="actions">
          <input
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            placeholder="store_id，如 ST-EAST-02"
            style={{ minWidth: 200, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <input
            value={day}
            onChange={(e) => setDay(e.target.value)}
            placeholder="day YYYY-MM-DD"
            style={{ minWidth: 160, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={() => apply(false)}>同日链应用</button>
          <button onClick={() => apply(true)} className="secondary">
            失败闭演练（注入半截失败）
          </button>
          <button className="secondary" onClick={attest}>只读核证</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>

      {att && (
        <div className="panel">
          <h2>
            链核证（只读）· {att.store_id} · {att.day}
            <span className={`badge ${att.valid ? "ok" : "bad"}`} style={{ marginLeft: 10 }}>
              {att.found ? (att.valid ? "链有效" : "链违规") : "无链"}
            </span>
          </h2>
          {!att.found ? (
            <p className="muted">该店该日尚无同日链。</p>
          ) : (
            <>
              <p className="mono" style={{ lineHeight: 1.8 }}>
                chain_id: {att.chain_id} · 段数: {att.segment_count} ·
                链态: {att.chain_state || "—"} ·
                末段 hist: {att.last_hist_id} · 末段 hash: {att.last_hash}
                <br />
                镜指 hist: {att.mirror?.hist_id || "—"} · 镜 hash: {att.mirror?.row_hash || "—"}
              </p>
              {att.violations?.length > 0 && (
                <p className="error">违规项：{att.violations.join(" / ")}</p>
              )}
              <table>
                <thead>
                  <tr>
                    <th>段</th><th>hist_id</th><th>前序 hist</th><th>源快照</th>
                    <th>valid_from</th><th>valid_to</th><th>当前</th><th>row_hash</th>
                  </tr>
                </thead>
                <tbody>
                  {att.segments.map((s) => (
                    <tr key={s.hist_id}>
                      <td>{s.segment_no}</td>
                      <td className="mono">{s.hist_id}</td>
                      <td className="mono">{s.prev_hist_id || "—"}</td>
                      <td className="mono">{s.source_snapshot_id}</td>
                      <td>{s.valid_from}</td>
                      <td>{s.valid_to || "—（末段开放）"}</td>
                      <td>{s.hist_is_current ? "是" : "否"}</td>
                      <td className="mono">{s.row_hash}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}

      <div className="panel">
        <h2>已落库同日链（{chains.length}）</h2>
        <table>
          <thead>
            <tr>
              <th>链</th><th>门店</th><th>日</th><th>段数</th>
              <th>首 as_of</th><th>末 as_of</th><th>末段 hash</th><th>核证</th>
            </tr>
          </thead>
          <tbody>
            {chains.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.id}</td>
                <td>{c.store_id}</td>
                <td>{c.day}</td>
                <td>{c.segment_count}</td>
                <td>{c.first_as_of_ts}</td>
                <td>{c.last_as_of_ts}</td>
                <td className="mono">{c.last_hash}</td>
                <td>
                  <span className={`badge ${c.attestation?.valid ? "ok" : "bad"}`}>
                    {c.attestation?.valid ? "通过" : `违规 ${c.attestation?.violations?.length || 0}`}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
