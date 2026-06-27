"use client";

import { useEffect, useState } from "react";
import { api } from "../../../lib/api";
import { promptPassword } from "./admin-helpers";
import { Card, Th } from "./admin-ui";

export function Whitelist() {
  const [list, setList] = useState([]);
  const [phone, setPhone] = useState("");
  const [note, setNote] = useState("");
  const [department, setDept] = useState("");
  const [msg, setMsg] = useState("");

  const load = () => api.adminWhitelist().then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function add() {
    setMsg("");
    try {
      await api.adminAddWhitelist({ phone, note, department });
      setPhone(""); setNote(""); setDept("");
      load();
    } catch (e) { setMsg(e.message); }
  }

  async function remove(phone) {
    if (!window.confirm(`确认移除白名单 ${phone}？`)) return;
    setMsg("");
    try {
      await api.adminRemoveWhitelist(phone, {});
      load();
    } catch (e) { setMsg(e.message); }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className="input w-40" placeholder="手机号" value={phone} onChange={(e) => setPhone(e.target.value)} />
        <input className="input w-40" placeholder="备注 / 姓名" value={note} onChange={(e) => setNote(e.target.value)} />
        <input className="input w-32" placeholder="部门" value={department} onChange={(e) => setDept(e.target.value)} />
        <button onClick={add} className="btn-primary">添加</button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line"><Th>手机号</Th><Th>备注</Th><Th>部门</Th><Th>操作</Th></tr></thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.phone} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">{r.phone}</td><td className="pr-3">{r.note}</td><td className="pr-3">{r.department}</td>
                <td><button onClick={() => remove(r.phone)} className="btn-ghost btn-sm">移除</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function Users() {
  const [list, setList] = useState([]);
  const [amounts, setAmounts] = useState({});
  const [notes, setNotes] = useState({});
  const [grantKeys, setGrantKeys] = useState({});
  const [msg, setMsg] = useState("");
  const [granting, setGranting] = useState(null);
  const load = () => api.adminUsers().then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function grant(uid) {
    if (granting === uid) return;
    const amount = Number(amounts[uid] || 0);
    const note = (notes[uid] || "").trim();
    if (!amount) return;
    if (!note) {
      setMsg("请填写额度发放原因");
      return;
    }
    setGranting(uid);
    if (!window.confirm(`确认给用户 ${uid} 发放 ${amount} 积分？\n原因：${note}`)) {
      setGranting(null);
      return;
    }
    setMsg("");
    try {
      const signature = `${uid}:${amount}:${note}`;
      const existing = grantKeys[uid];
      const freshKey = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const idempotencyKey = existing?.signature === signature ? existing.key : freshKey;
      setGrantKeys((prev) => ({ ...prev, [uid]: { signature, key: idempotencyKey } }));
      await api.adminGrant({
        user_id: uid,
        amount,
        note,
        idempotency_key: idempotencyKey,
      });
      setAmounts({ ...amounts, [uid]: "" });
      setNotes({ ...notes, [uid]: "" });
      setGrantKeys((prev) => {
        const next = { ...prev };
        delete next[uid];
        return next;
      });
      load();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setGranting(null);
    }
  }

  async function resetPw(uid) {
    const pw = await promptPassword("为该用户设置新密码(≥6 位),将强制其重新登录", "新密码");
    if (!pw) return;
    if (pw.length < 6) {
      window.alert("新密码至少 6 位");
      return;
    }
    try {
      await api.adminResetPassword(uid, { password: pw });
      window.alert("已重置");
    } catch (e) {
      window.alert(e.message);
    }
  }

  async function setStatus(user, status) {
    const action = status === "disabled" ? "禁用" : status === "active" ? "启用" : "设为待审核";
    if (!window.confirm(`确认${action}用户 ${user.phone}？该操作会使其已登录令牌失效。`)) return;
    setMsg("");
    try {
      await api.adminSetUserStatus(user.id, { status });
      load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <Card>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
              <Th>ID</Th><Th>手机号</Th><Th>部门</Th><Th>状态</Th><Th>余额</Th><Th>发放</Th><Th>账号</Th>
          </tr></thead>
          <tbody>
            {list.map((u) => (
              <tr key={u.id} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">{u.id}</td>
                <td className="pr-3 text-snow">
                  {u.phone}
                  {u.is_admin && <span className="badge ml-1 bg-iris/15 text-iris-400">admin</span>}
                </td>
                <td className="pr-3">{u.department}</td>
                <td className="pr-3">
                  <span className={`badge ${u.status === "active" ? "bg-ok/15 text-ok" : u.status === "disabled" ? "bg-bad/15 text-bad" : "bg-warn/15 text-warn"}`}>
                    {u.status}
                  </span>
                </td>
                <td className="pr-3 font-display text-snow">{u.balance_credits}</td>
                <td className="py-1">
                  <input
                    className="input inline-block w-20"
                    value={amounts[u.id] || ""} placeholder="额度"
                    onChange={(e) => {
                      setAmounts({ ...amounts, [u.id]: e.target.value });
                      setGrantKeys((prev) => {
                        const next = { ...prev };
                        delete next[u.id];
                        return next;
                      });
                    }}
                  />
                  <input
                    className="input ml-1.5 inline-block w-36"
                    value={notes[u.id] || ""} placeholder="原因"
                    onChange={(e) => {
                      setNotes({ ...notes, [u.id]: e.target.value });
                      setGrantKeys((prev) => {
                        const next = { ...prev };
                        delete next[u.id];
                        return next;
                      });
                    }}
                  />
                  <button onClick={() => grant(u.id)} disabled={granting === u.id}
                    className="btn-secondary btn-sm ml-1.5">
                    {granting === u.id ? "发放中" : "发放"}
                  </button>
                  <button onClick={() => resetPw(u.id)} className="btn-ghost btn-sm ml-1">重置密码</button>
                </td>
                <td className="py-1">
                  {u.status === "disabled" ? (
                    <button onClick={() => setStatus(u, "active")} className="btn-secondary btn-sm">启用</button>
                  ) : u.is_admin ? (
                    <button disabled title="管理员账号需要至少保留一个可用入口" className="btn-ghost btn-sm opacity-50">
                      管理员
                    </button>
                  ) : (
                    <button onClick={() => setStatus(u, "disabled")} className="btn-ghost btn-sm">禁用</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
