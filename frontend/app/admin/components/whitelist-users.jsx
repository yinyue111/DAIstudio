"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "../../../lib/api";
import { promptPassword } from "./admin-helpers";
import { Card, Th } from "./admin-ui";

export function Whitelist() {
  const [list, setList] = useState([]);
  const [phone, setPhone] = useState("");
  const [note, setNote] = useState("");
  const [department, setDept] = useState("");
  const [msg, setMsg] = useState("");
  const [saving, setSaving] = useState(false);
  const [removingPhone, setRemovingPhone] = useState("");

  const load = () => api.adminWhitelist().then(setList).catch((e) => setMsg(e.message));
  useEffect(() => { load(); }, []);

  async function add() {
    if (saving) return;
    setMsg("");
    setSaving(true);
    try {
      await api.adminAddWhitelist({ phone, note, department });
      setPhone(""); setNote(""); setDept("");
      load();
    } catch (e) { setMsg(e.message); }
    finally { setSaving(false); }
  }

  async function remove(phone) {
    if (removingPhone) return;
    if (!window.confirm(`确认移除白名单 ${phone}？`)) return;
    setMsg("");
    setRemovingPhone(phone);
    try {
      await api.adminRemoveWhitelist(phone, {});
      load();
    } catch (e) { setMsg(e.message); }
    finally { setRemovingPhone(""); }
  }

  return (
    <Card>
      <div className="mb-4 flex flex-wrap gap-2">
        <input className="input w-40" placeholder="手机号" value={phone} onChange={(e) => setPhone(e.target.value)} />
        <input className="input w-40" placeholder="备注 / 姓名" value={note} onChange={(e) => setNote(e.target.value)} />
        <input className="input w-32" placeholder="部门" value={department} onChange={(e) => setDept(e.target.value)} />
        <button onClick={add} disabled={saving} className="btn-primary disabled:opacity-50">
          {saving ? "添加中" : "添加"}
        </button>
      </div>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line"><Th>手机号</Th><Th>备注</Th><Th>部门</Th><Th>操作</Th></tr></thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.phone} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3 text-snow">{r.phone}</td><td className="pr-3">{r.note}</td><td className="pr-3">{r.department}</td>
                <td>
                  <button
                    onClick={() => remove(r.phone)}
                    disabled={Boolean(removingPhone)}
                    className="btn-ghost btn-sm disabled:opacity-50"
                  >
                    {removingPhone === r.phone ? "移除中" : "移除"}
                  </button>
                </td>
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
  const [q, setQ] = useState("");
  const [status, setStatusFilter] = useState("");
  const [onlyAdmin, setOnlyAdmin] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [bulkAmount, setBulkAmount] = useState("");
  const [bulkNote, setBulkNote] = useState("");
  const [amounts, setAmounts] = useState({});
  const [notes, setNotes] = useState({});
  const [grantKeys, setGrantKeys] = useState({});
  const [bulkGrantKey, setBulkGrantKey] = useState(null);
  const [msg, setMsg] = useState("");
  const [granting, setGranting] = useState(null);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const loadSeqRef = useRef(0);
  const PAGE = 50;
  const load = (nextOffset = 0, overrides = {}) => {
    const seq = ++loadSeqRef.current;
    const query = overrides.q ?? q;
    const rowStatus = overrides.status ?? status;
    const adminFilter = overrides.onlyAdmin ?? onlyAdmin;
    setLoadingUsers(true);
    return api.adminUsers({
    q: query,
    status: rowStatus,
    is_admin: adminFilter === "" ? "" : adminFilter === "true",
    limit: PAGE,
    offset: nextOffset,
  }).then((rows) => {
    if (seq !== loadSeqRef.current) return rows;
    setList(rows);
    setOffset(nextOffset);
    setHasMore(rows.length === PAGE);
    setSelected(new Set());
    return rows;
  }).catch((e) => {
    if (seq === loadSeqRef.current) setMsg(e.message);
  }).finally(() => {
    if (seq === loadSeqRef.current) setLoadingUsers(false);
  });
  };
  useEffect(() => { load(0); }, []);

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

  function toggleSelected(uid) {
    setBulkGrantKey(null);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });
  }

  async function bulkGrant() {
    const ids = [...selected].sort((a, b) => Number(a) - Number(b));
    const amount = Number(bulkAmount || 0);
    const note = bulkNote.trim();
    if (!ids.length) return setMsg("请先选择用户");
    if (!amount || amount <= 0) return setMsg("请填写批量发放额度");
    if (!note) return setMsg("请填写批量发放原因");
    if (!window.confirm(`确认给 ${ids.length} 个用户每人发放 ${amount} 积分？\n原因：${note}`)) return;
    setMsg("");
    try {
      const signature = `${ids.join(",")}:${amount}:${note}`;
      const freshKey = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const idempotencyKey = bulkGrantKey?.signature === signature ? bulkGrantKey.key : freshKey;
      setBulkGrantKey({ signature, key: idempotencyKey });
      const res = await api.adminBulkGrant({
        items: ids.map((user_id) => ({ user_id, amount, note })),
        idempotency_key: idempotencyKey,
      });
      const ok = res.granted?.length || 0;
      const failed = res.failed?.length || 0;
      setMsg(failed ? `已发放 ${ok} 个，${failed} 个失败。` : `已批量发放 ${ok} 个用户。`);
      setBulkAmount("");
      setBulkNote("");
      setSelected(new Set());
      setBulkGrantKey(null);
      load(offset);
    } catch (e) {
      setMsg(e.message);
    }
  }

  return (
    <Card>
      {msg && <p className="mb-2 text-sm text-bad">{msg}</p>}
      <div className="mb-4 grid gap-2 lg:grid-cols-[1fr_auto_auto_auto_auto]">
        <input
          className="input"
          placeholder="搜索手机号、昵称、部门"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(0); }}
        />
        <select className="input" value={status} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="pending">pending</option>
          <option value="disabled">disabled</option>
        </select>
        <select className="input" value={onlyAdmin} onChange={(e) => setOnlyAdmin(e.target.value)}>
          <option value="">全部账号</option>
          <option value="true">管理员</option>
          <option value="false">普通用户</option>
        </select>
        <button onClick={() => load(0)} disabled={loadingUsers} className="btn-secondary btn-sm disabled:opacity-50">
          {loadingUsers ? "加载中" : "搜索"}
        </button>
        <button
          onClick={() => { setQ(""); setStatusFilter(""); setOnlyAdmin(""); load(0, { q: "", status: "", onlyAdmin: "" }); }}
          disabled={loadingUsers}
          className="btn-ghost btn-sm disabled:opacity-50"
        >
          重置
        </button>
      </div>
      {selected.size > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl2 border border-iris/30 bg-iris/10 p-3">
          <span className="text-sm text-mist">已选择 {selected.size} 个用户</span>
          <input className="input w-28" placeholder="每人额度" value={bulkAmount} onChange={(e) => {
            setBulkAmount(e.target.value);
            setBulkGrantKey(null);
          }} />
          <input className="input w-52" placeholder="批量发放原因" value={bulkNote} onChange={(e) => {
            setBulkNote(e.target.value);
            setBulkGrantKey(null);
          }} />
          <button onClick={bulkGrant} className="btn-primary btn-sm">批量发放</button>
          <button onClick={() => {
            setSelected(new Set());
            setBulkGrantKey(null);
          }} className="btn-ghost btn-sm">取消选择</button>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead><tr className="border-b border-line">
              <Th>选择</Th><Th>ID</Th><Th>手机号</Th><Th>部门</Th><Th>状态</Th><Th>余额</Th><Th>发放</Th><Th>账号</Th>
          </tr></thead>
          <tbody>
            {list.map((u) => (
              <tr key={u.id} className="border-b border-line/60 text-mist transition-colors hover:bg-white/5">
                <td className="py-2 pr-3">
                  <input
                    type="checkbox"
                    checked={selected.has(u.id)}
                    onChange={() => toggleSelected(u.id)}
                    className="h-4 w-4 accent-brand"
                    aria-label={`选择用户 ${u.phone}`}
                  />
                </td>
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
      <div className="mt-4 flex items-center justify-between">
        <button onClick={() => load(Math.max(0, offset - PAGE))} disabled={loadingUsers || offset <= 0} className="btn-secondary btn-sm disabled:opacity-50">
          上一页
        </button>
        <span className="text-xs text-fog">第 {Math.floor(offset / PAGE) + 1} 页</span>
        <button onClick={() => load(offset + PAGE)} disabled={loadingUsers || !hasMore} className="btn-secondary btn-sm disabled:opacity-50">
          下一页
        </button>
      </div>
    </Card>
  );
}
