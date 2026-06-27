"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import BrandLogo from "../../components/BrandLogo";
import { api } from "../../lib/api";

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
    let cancelled = false;
    api.me()
      .then(() => {
        if (!cancelled) router.replace(currentNextPath() || "/");
      })
      .catch(() => {});
    api.authFeatures()
      .then((f) => {
        if (cancelled) return;
        setSmsAuthEnabled(!!(f.sms_required_for_registration ?? f.sms_auth_enabled));
        setRegistrationEnabled(f.registration_enabled !== false);
        setFeaturesLoaded(true);
        setFeaturesError("");
      })
      .catch(() => {
        if (cancelled) return;
        setRegistrationEnabled(false);
        setFeaturesLoaded(false);
        setFeaturesError("注册配置加载失败，请刷新重试");
      });
    return () => { cancelled = true; };
  }, [router]);

  useEffect(() => {
    if (smsCooldown <= 0) return undefined;
    const timer = setInterval(() => {
      setSmsCooldown((n) => Math.max(0, n - 1));
    }, 1000);
    return () => clearInterval(timer);
  }, [smsCooldown]);

  async function submit(event) {
    event?.preventDefault();
    const form = event?.currentTarget;
    const formPhone = form?.elements?.phone?.value?.trim() ?? phone.trim();
    const formPassword = form?.elements?.password?.value ?? password;
    const formSmsCode = form?.elements?.smsCode?.value?.trim() ?? smsCode.trim();
    const formNickname = form?.elements?.nickname?.value ?? nickname;
    setMsg("");
    if (!/^1[3-9]\d{9}$/.test(formPhone)) return setMsg("请输入正确的手机号");
    if (formPassword.length < 6) return setMsg("密码至少 6 位");
    if (isRegister && (!featuresLoaded || featuresError)) return setMsg(featuresError || "注册配置加载中，请稍后重试");
    if (isRegister && !registrationEnabled) return setMsg("注册暂未开放，请联系管理员");
    if (isRegister && smsAuthEnabled && !formSmsCode) return setMsg("请输入短信验证码");
    setPhone(formPhone);
    setPassword(formPassword);
    setSmsCode(formSmsCode);
    setNickname(formNickname);
    setLoading(true);
    try {
      if (isRegister) {
        await api.register(formPhone, formPassword, smsAuthEnabled ? formSmsCode : undefined, formNickname || undefined);
      } else {
        await api.login(formPhone, formPassword);
      }
      const nextPath = currentNextPath() || "/";
      router.replace(nextPath);
      router.refresh();
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
            <BrandLogo className="h-9 w-9" />
            <span className="font-display text-[17px] font-bold tracking-tight">
              造梦<span className="text-fog font-medium"> Studio</span>
            </span>
          </a>
          <h1 className="text-3xl font-extrabold leading-tight sm:text-4xl">
            {isRegister ? (
              <>创建<span className="text-grad">账号</span></>
            ) : (
              <>欢迎<span className="text-grad">回来</span></>
            )}
          </h1>
          <p className="mt-2 text-[15px] text-mist">
            {isRegister
              ? featuresError ? "注册账号 · 配置加载失败" : !registrationEnabled ? "注册账号 · 暂未开放" : smsAuthEnabled ? "注册账号 · 短信验证码" : "注册账号 · 手机号 + 密码"
              : "手机号 + 密码登录"}
          </p>
        </div>

        <form className="panel p-6" onSubmit={submit}>
          <div className="mb-5 flex rounded-full border border-line bg-base2/50 p-1 text-sm">
            {[["login", "登录"], ["register", "注册"]].map(([k, label]) => (
              <button
                key={k}
                type="button"
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
            name="phone"
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
                name="nickname"
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
                  当前未开放自助注册，请联系管理员。
                </p>
              )}
              {smsAuthEnabled && (
                <>
                  <label className="label">短信验证码</label>
                  <div className="mb-4 grid grid-cols-[1fr_auto] gap-2">
                    <input
                      className="input"
                      name="smsCode"
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
            name="password"
            placeholder="至少 6 位"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />

          <button type="submit" disabled={loading || (isRegister && (!featuresLoaded || !!featuresError || !registrationEnabled))} className="btn-primary btn-lg w-full">
            {loading ? "请稍候…" : isRegister ? "注册并登录" : "登录"}
          </button>

          {msg && <p className="mt-4 text-center text-sm text-bad">{msg}</p>}
        </form>

        <p className="mt-6 text-center text-xs text-fog">AI 图片视频生成工作台</p>
      </div>
    </div>
  );
}

function safeNextPath(value) {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "";
  try {
    const url = new URL(value, "http://local");
    if (url.origin !== "http://local") return "";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch (e) {
    return "";
  }
}

function currentNextPath() {
  if (typeof window === "undefined") return "";
  try {
    return safeNextPath(new URLSearchParams(window.location.search).get("next"));
  } catch (e) {
    return "";
  }
}
