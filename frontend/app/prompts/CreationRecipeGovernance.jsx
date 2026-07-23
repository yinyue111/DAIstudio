"use client";

import { useState } from "react";
import { Copy, Link2, Send, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "../../lib/api";
import { creationRecipeShareUrl } from "../../lib/creationRecipeTransfer";
import { formatLocalDateTime } from "../../lib/datetime";

function expiresAt(option) {
  const days = Number(option);
  if (!Number.isFinite(days) || days <= 0) return null;
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}

export default function CreationRecipeGovernance({ recipe, version, onRecipeChange }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [sharesOpen, setSharesOpen] = useState(false);
  const [sharesLoaded, setSharesLoaded] = useState(false);
  const [shares, setShares] = useState([]);
  const [expiration, setExpiration] = useState("30");

  async function run(action, callback) {
    if (busy) return null;
    setBusy(action);
    setError("");
    try {
      return await callback();
    } catch (cause) {
      setError(cause?.message || "操作失败，请稍后重试");
      return null;
    } finally {
      setBusy("");
    }
  }

  async function submitReview() {
    const updated = await run("review", () => api.submitCreationRecipeReview(recipe.id));
    if (updated) onRecipeChange?.(updated);
  }

  async function toggleShares() {
    const next = !sharesOpen;
    setSharesOpen(next);
    if (!next || sharesLoaded) return;
    const rows = await run("shares", () => api.creationRecipeShares(recipe.id));
    if (Array.isArray(rows)) {
      setShares(rows);
      setSharesLoaded(true);
    }
  }

  async function createShare() {
    const created = await run("create-share", () => api.createCreationRecipeShare(recipe.id, {
      version: Number(version) || recipe.current_version,
      expires_at: expiresAt(expiration),
    }));
    if (created) setShares((rows) => [created, ...rows]);
  }

  async function copyShare(share) {
    await run(`copy-${share.id}`, async () => {
      await navigator.clipboard.writeText(creationRecipeShareUrl(share.slug, window.location.origin));
      return true;
    });
  }

  async function revokeShare(share) {
    const revoked = await run(`revoke-${share.id}`, () => (
      api.revokeCreationRecipeShare(recipe.id, share.id)
    ));
    if (revoked) {
      setShares((rows) => rows.map((row) => row.id === revoked.id ? revoked : row));
    }
  }

  const canSubmit = recipe.visibility === "public"
    && ["draft", "rejected"].includes(recipe.moderation_status);

  return (
    <div className="mt-2 border-t border-line pt-2">
      {canSubmit && (
        <button type="button" className="btn-ghost btn-sm min-h-10" onClick={submitReview} disabled={Boolean(busy)}>
          <Send size={14} aria-hidden="true" />
          {recipe.moderation_status === "rejected" ? "重新提交审核" : "提交公开审核"}
        </button>
      )}
      {recipe.moderation_status === "pending" && (
        <p className="inline-flex min-h-10 items-center gap-1.5 text-xs text-warn" role="status">
          <ShieldCheck size={14} aria-hidden="true" /> 公开审核中
        </p>
      )}
      {recipe.moderation_status === "rejected" && recipe.review_note && (
        <p className="mt-1 text-xs text-bad">审核意见：{recipe.review_note}</p>
      )}

      <button type="button" className="btn-ghost btn-sm min-h-10" onClick={toggleShares} aria-expanded={sharesOpen} disabled={Boolean(busy)}>
        <Link2 size={14} aria-hidden="true" /> 分享管理
      </button>

      {sharesOpen && (
        <div className="mt-2 border-t border-line pt-2">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-32 flex-1">
              <label htmlFor={`recipe-share-expiry-${recipe.id}`} className="text-[11px] font-medium text-mist">链接有效期</label>
              <select
                id={`recipe-share-expiry-${recipe.id}`}
                className="input mt-1 w-full px-2.5 py-2 text-xs"
                value={expiration}
                onChange={(event) => setExpiration(event.target.value)}
                disabled={Boolean(busy)}
              >
                <option value="7">7 天</option>
                <option value="30">30 天</option>
                <option value="90">90 天</option>
                <option value="0">长期有效</option>
              </select>
            </div>
            <button type="button" className="btn-secondary btn-sm min-h-10" onClick={createShare} disabled={Boolean(busy)}>
              <Link2 size={14} aria-hidden="true" /> 新建链接
            </button>
          </div>

          {shares.length === 0 && busy !== "shares" && (
            <p className="mt-2 text-xs text-fog" role="status">尚未创建分享链接</p>
          )}
          <div className="mt-2 space-y-2">
            {shares.map((share) => {
              const expired = share.expires_at && new Date(share.expires_at).getTime() <= Date.now();
              const active = share.status === "active" && !expired;
              return (
                <div key={share.id} className="flex min-w-0 items-center gap-2 border-t border-line/70 pt-2 text-xs">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-mist">v{share.version} · {active ? "有效" : "已失效"}</p>
                    <p className="truncate text-[11px] text-fog">
                      {share.expires_at ? `${formatLocalDateTime(share.expires_at)} 到期` : "长期有效"}
                    </p>
                  </div>
                  <button type="button" className="icon-btn h-9 w-9" aria-label="复制分享链接" onClick={() => copyShare(share)} disabled={Boolean(busy) || !active}>
                    <Copy size={14} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-btn h-9 w-9 text-bad" aria-label="撤销分享链接" onClick={() => revokeShare(share)} disabled={Boolean(busy) || !active}>
                    <Trash2 size={14} aria-hidden="true" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {error && <p className="mt-2 text-xs text-bad" role="alert">{error}</p>}
    </div>
  );
}
