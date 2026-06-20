"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, setToken } from "../../lib/api";

function Logo({ className = "" }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <defs>
        <linearGradient id="loginlogo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#7b61ff" />
          <stop offset="50%" stopColor="#b65cff" />
          <stop offset="100%" stopColor="#ff5fa2" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="28" height="28" rx="9" fill="url(#loginlogo)" />
      <path
        d="M16 8l1.9 4.6 4.6 1.9-4.6 1.9L16 21l-1.9-4.6L9.5 14.5l4.6-1.9L16 8z"
        fill="#fff"
        fillOpacity="0.95"
      />
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState("login"); // login | register
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [smsCode, setSmsCode] = useState("");
  const [nickname, setNickname] = useState("");
  const [smsSending, setSmsSending] = useState(false);
  const [smsCooldown, setSmsCooldown] = useState(0);
  const [smsAuthEnabled, setSmsAuthEnabled] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState(true);
  const [featuresLoaded, setFeaturesLoaded] = useState(false);
  const [featuresError, setFeaturesError] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);

  const isRegister = mode === "register";

  useEffect(() => {
    api.authFeatures()
      .then((f) => {
        setSmsAuthEnabled(!!(f.sms_required_for_registration ?? f.sms_auth_enabled));
        setRegistrationEnabled(f.registration_enabled !== false);
        setFeaturesLoaded(true);
        setFeaturesError("");
      })
      .catch(() => {
        setRegistrationEnabled(false);
        setFeaturesLoaded(false);
        setFeaturesError("注册配置加载失败，请刷新重试");
      });
  }, []);

  useEffect(() => {
    if (smsCooldown <= 0) return undefined;
    const timer = setInterval(() => {
      setSmsCooldown((n) => Math.max(0, n - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [smsCooldown]);

  async function submit() {
    setMsg("");
    if (!/^1[3-9]\d{9}$/.test(phone)) return setMsg("请输入正确的手机号");
    if (password.length < 6) return setMsg("密码至少 6 位");
    if (isRegister && (!featuresLoaded || featuresError)) return setMsg(featuresError || "注册配置加载中，请稍后重试");
    if (isRegister && !registrationEnabled) return setMsg("注册暂未开放，请联系管理员开启短信验证码注册");
    if (isRegister && smsAuthEnabled && !smsCode.trim()) return setMsg("请输入短信验证码");
    setLoading(true);
    try {
      const r = isRegister
        ? await api.register(phone, password, smsAuthEnabled ? smsCode.trim() : undefined, nickname || undefined)
        : await api.login(phone, password);
      setToken(r.access_token);
      router.push("/");
    } catch (e) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function sendCode() {
    setMsg("");
    if (!/^1[3-9]\d{9}$/.test(phone)) return setMsg("请输入正确的手机号");
    setSmsSending(true);
    try {
      const r = await api.sendSmsCode(phone);
      if (r.disabled) {
        setMsg("短信验证未开启");
        return;
      }
      setSmsCooldown(Number(r.retry_after || 60));
      setMsg(r.code ? `验证码已发送，本地测试码: ${r.code}` : "验证码已发送");
    } catch (e) {
      if (e.retryAfter) setSmsCooldown(e.retryAfter);
      setMsg(e.message);
    } finally {
      setSmsSending(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm animate-fadeup">
        <div className="mb-7 flex flex-col items-center text-center">
          <a href="/" className="mb-5 flex items-center gap-2.5">
            <Logo className="h-9 w-9 drop-shadow-[0_4px_14px_rgba(123,97,255,0.5)]" />
            <span className="font-display text-[17px] font-bold tracking-tight">
              造梦<span className="text-fog font-medium"> Studio</span>
            </span>
          </a>
          <h1 className="text-3xl font-extrabold leading-tight sm:text-4xl">
            欢迎<span className="text-grad">回来</span>
          </h1>
          <p className="mt-2 text-[15px] text-mist">
            {isRegister
              ? featuresError ? "注册账号 · 配置加载失败" : !registrationEnabled ? "注册账号 · 暂未开放" : smsAuthEnabled ? "注册账号 · 白名单手机号 + 验证码" : "注册账号 · 白名单手机号"
              : "手机号 + 密码登录"}
          </p>
        </div>

        <div className="panel p-6">
          <div className="mb-5 flex rounded-full border border-line bg-base2/50 p-1 text-sm">
            {[["login", "登录"], ["register", "注册"]].map(([k, label]) => (
              <button
                key={k}
                onClick={() => { setMode(k); setMsg(""); }}
                className={`flex-1 rounded-full px-3 py-1.5 font-display font-medium transition-all ${
                  mode === k ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:text-snow"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <label className="label">手机号</label>
          <input
            className="input mb-4"
            placeholder="11 位手机号"
            value={phone}
            onChange={(e) => setPhone(e.target.value.trim())}
            inputMode="numeric"
          />

          {isRegister && (
            <>
              <label className="label">昵称(可选)</label>
              <input
                className="input mb-4"
                placeholder="显示名称"
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
              />
              {featuresError && (
                <p className="mb-4 rounded-xl border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
                  {featuresError}
                </p>
              )}
              {featuresLoaded && !registrationEnabled && (
                <p className="mb-4 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                  当前未开放自助注册，请联系管理员开启短信验证码注册。
                </p>
              )}
              {smsAuthEnabled && (
                <>
                  <label className="label">短信验证码</label>
                  <div className="mb-4 grid grid-cols-[1fr_auto] gap-2">
                    <input
                      className="input"
                      placeholder="6 位验证码"
                      value={smsCode}
                      onChange={(e) => setSmsCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                      inputMode="numeric"
                    />
                    <button
                      onClick={sendCode}
                      disabled={smsSending || smsCooldown > 0}
                      type="button"
                      className="btn-secondary whitespace-nowrap"
                    >
                      {smsSending ? "发送中…" : smsCooldown > 0 ? `${smsCooldown}s` : "获取验证码"}
                    </button>
                  </div>
                </>
              )}
            </>
          )}

          <label className="label">密码</label>
          <input
            type="password"
            className="input mb-5"
            placeholder="至少 6 位"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />

          <button onClick={submit} disabled={loading || (isRegister && (!featuresLoaded || !!featuresError || !registrationEnabled))} className="btn-primary btn-lg w-full">
            {loading ? "请稍候…" : isRegister ? "注册并登录" : "登录"}
          </button>

          {msg && <p className="mt-4 text-center text-sm text-bad">{msg}</p>}
        </div>

        <p className="mt-6 text-center text-xs text-fog">内部工具 · 仅限授权员工使用</p>
      </div>
    </div>
  );
}
