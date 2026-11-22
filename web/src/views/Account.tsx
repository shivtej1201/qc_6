import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, useLoad, useToast } from "../ui";

/* QC's own sign-in (QC6_AUTH=local): the sign-in screen, and "Your account" (password, two-factor, sign out). */

export function SignIn({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<"password" | "code">("password");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (step === "code") codeRef.current?.focus(); }, [step]);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      if (step === "password") {
        const r = await api.login(email, password);
        setPassword("");
        if (r.two_factor) setStep("code"); else onDone();
      } else {
        const r = await api.secondFactor(code);
        if (r.recovery_codes_left != null) window.alert(`You used a recovery code. ${r.recovery_codes_left} left; make new ones under Your account if that's low.`);
        onDone();
      }
    } catch (x: any) {
      setErr(x.detail ? `${x.title}. ${x.detail}` : x.title || x.message);
      if (x.status === 401 && step === "code" && /Sign in again/.test(x.title)) setStep("password");
    } finally { setBusy(false); }
  };
  return (
    <main className="signin" id="main">
      <form className="panel grid" onSubmit={submit} aria-labelledby="si-h" style={{ gap: 14 }}>
        <div className="logo"><div className="logo-mark">QC</div><div><b>QC V6</b><span>Site quality</span></div></div>
        <h1 id="si-h" style={{ fontSize: 24 }}>{step === "password" ? "Sign in" : "Two-factor code"}</h1>
        {step === "password" ? (
          <>
            <label className="grid" style={{ gap: 4 }}>E-mail
              <input className="input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
            <label className="grid" style={{ gap: 4 }}>Password
              <input className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          </>
        ) : (
          <label className="grid" style={{ gap: 4 }}>The 6-digit code from your authenticator app, or a recovery code
            <input ref={codeRef} className="input" inputMode="numeric" autoComplete="one-time-code" required value={code} onChange={(e) => setCode(e.target.value)} /></label>
        )}
        {err && <div className="error-box" role="alert">{err}</div>}
        <button className="btn primary" type="submit" disabled={busy}>{busy ? "Checking…" : step === "password" ? "Sign in" : "Continue"}</button>
        {step === "password" && <button type="button" className="btn quiet small" style={{ justifySelf: "start", padding: 0 }}
          onClick={async () => {
            if (!email) { setErr("Type your e-mail first, then choose Forgot your password."); return; }
            try { const r = await api.forgot(email); setErr(null); setSent(r.mail ? "If that address has an account, a link to choose a new password is on its way (valid 30 minutes)."
              : "E-mail isn't set up on this QC. Ask whoever runs it to reset your password."); }
            catch (x: any) { setErr(x.detail ? `${x.title}. ${x.detail}` : x.title || x.message); }
          }}>Forgot your password?</button>}
        {sent && <p role="status" style={{ margin: 0, fontSize: 14 }}>{sent}</p>}
        <p className="faint" style={{ fontSize: 13, margin: 0 }}>Lost your phone? Whoever runs QC can turn two-factor off with
          <span className="mono"> python -m qc6.admin reset-2fa</span>.</p>
      </form>
    </main>
  );
}

export function AccountView() {
  const acc = useLoad(() => api.account(), []);
  const ref = useViewMotion<HTMLDivElement>([!!acc.data]);
  const toast = useToast();
  const [pw, setPw] = useState({ current: "", next: "" });
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [offPw, setOffPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const run = async (f: () => Promise<void>) => { setErr(null); try { await f(); } catch (x: any) { setErr(x.detail ? `${x.title}. ${x.detail}` : x.title || x.message); } };
  if (acc.error) return <ErrorBox text={acc.error} onRetry={acc.reload} />;
  if (!acc.data) return <p className="faint">Loading…</p>;
  return (
    <div className="view" ref={ref} style={{ maxWidth: 760 }}>
      <div className="head"><div><h1>Your account</h1><p className="lede">Signed in as {acc.data.email}.</p></div>
        <button className="btn" onClick={() => run(async () => { await api.logout(); window.location.hash = "/today"; window.location.reload(); })}>Sign out</button></div>
      {err && <div className="error-box" role="alert" style={{ marginBottom: 14 }}>{err}</div>}
      <section className="panel grid" data-reveal>
        <h2>Password</h2>
        <form className="grid" style={{ gap: 10 }} onSubmit={(e) => { e.preventDefault(); run(async () => {
          await api.changePassword(pw.current, pw.next); setPw({ current: "", next: "" }); toast("Password changed. Your other sessions were signed out."); }); }}>
          <label className="grid" style={{ gap: 4 }}>Current password<input className="input" type="password" autoComplete="current-password"
            value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></label>
          <label className="grid" style={{ gap: 4 }}>New password <span className="faint" style={{ fontSize: 13 }}>at least 10 characters; a short sentence works well</span>
            <input className="input" type="password" autoComplete="new-password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /></label>
          <div><button className="btn primary" type="submit" disabled={!pw.current || pw.next.length < 10}>Change password</button></div>
        </form>
      </section>
      <section className="panel grid" data-reveal style={{ marginTop: 16 }}>
        <h2>Two-factor sign-in</h2>
        {codes ? (
          <>
            <p style={{ margin: 0 }}><b>Two-factor is on.</b> Save these recovery codes somewhere safe. Each works once if you lose your phone. They won't be shown again.</p>
            <pre className="ai-ev" style={{ fontSize: 14 }}>{codes.join("\n")}</pre>
            <div><button className="btn" onClick={() => { setCodes(null); acc.reload(); }}>I've saved them</button></div>
          </>
        ) : acc.data.two_factor ? (
          <>
            <p style={{ margin: 0 }}>On. Signing in asks for a code from your authenticator app. {acc.data.recovery_codes_left} recovery codes left.</p>
            <form className="row" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.twoFactorDisable(offPw); setOffPw(""); toast("Two-factor is off."); acc.reload(); }); }}>
              <label className="sr-only" htmlFor="off-pw">Your password</label>
              <input id="off-pw" className="input" type="password" placeholder="Your password" autoComplete="current-password" value={offPw} onChange={(e) => setOffPw(e.target.value)} />
              <button className="btn" type="submit" disabled={!offPw}>Turn off</button>
            </form>
          </>
        ) : setup ? (
          <>
            <ol className="steps">
              <li>Open an authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, 1Password…).</li>
              <li>Add an account with this key: <b className="mono" style={{ wordBreak: "break-all" }}>{setup.secret.replace(/(.{4})/g, "$1 ").trim()}</b>
                {" "}(on a phone you can <a href={setup.uri}>open it in the app</a>).</li>
              <li>Type the 6-digit code it shows:</li>
            </ol>
            <form className="row" onSubmit={(e) => { e.preventDefault(); run(async () => { const r = await api.twoFactorEnable(setup.secret, code); setCodes(r.recovery_codes); setSetup(null); setCode(""); }); }}>
              <label className="sr-only" htmlFor="tf-code">Code</label>
              <input id="tf-code" className="input" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" />
              <button className="btn primary" type="submit" disabled={code.replace(/\D/g, "").length !== 6}>Turn on</button>
              <button className="btn quiet" type="button" onClick={() => setSetup(null)}>Cancel</button>
            </form>
          </>
        ) : (
          <>
            <p style={{ margin: 0 }}>Off. With it on, signing in also needs a code from your phone, so a stolen password isn't enough.</p>
            <div><button className="btn primary" onClick={() => run(async () => setSetup(await api.twoFactorSetup()))}>Set up two-factor</button></div>
          </>
        )}
      </section>
    </div>
  );
}


/* The page an e-mailed reset link opens (#/reset?token=...). */
export function ResetPassword({ token }: { token: string }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  return (
    <main className="signin" id="main">
      <form className="panel grid" style={{ gap: 14 }} aria-labelledby="rp-h" onSubmit={async (e) => {
        e.preventDefault();
        try { await api.resetPassword(token, pw); setDone(true); }
        catch (x: any) { setErr(x.detail ? `${x.title}. ${x.detail}` : x.title || x.message); }
      }}>
        <div className="logo"><div className="logo-mark">QC</div><div><b>QC V6</b><span>Site quality</span></div></div>
        <h1 id="rp-h" style={{ fontSize: 24 }}>Choose a new password</h1>
        {done ? (<>
          <p role="status" style={{ margin: 0 }}>Done. Every place you were signed in has been signed out.</p>
          <a className="btn primary" href="#/start" onClick={() => window.setTimeout(() => window.location.reload(), 0)}>Sign in</a>
        </>) : (<>
          <label className="grid" style={{ gap: 4 }}>New password <span className="faint" style={{ fontSize: 13 }}>at least 10 characters</span>
            <input className="input" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus /></label>
          {err && <div className="error-box" role="alert">{err}</div>}
          <button className="btn primary" type="submit" disabled={pw.length < 10}>Save the new password</button>
        </>)}
      </form>
    </main>
  );
}
