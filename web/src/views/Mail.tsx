import { useEffect, useState } from "react";
import { api, type MailForm, type MailSettings, type Site } from "../api";
import { useViewMotion } from "../motion";
import { ErrorBox, Icon, useLoad, useToast } from "../ui";

/* Email summary: SMTP settings for the workspace and the crawl summary e-mail sent after every check, with the
   Management report attached. The password is encrypted on the server and never comes back to the browser. */

const EMPTY: MailForm = { enabled: false, host: "", port: 587, security: "starttls", username: "", password: null, sender: "",
  recipients: [], attach_report: true };
const PORTS: Record<MailForm["security"], number> = { starttls: 587, ssl: 465, none: 25 };

export function MailView({ site }: { site: Site; params: URLSearchParams }) {
  const toast = useToast();
  const cur = useLoad(() => api.mail(), []);
  const runs = useLoad(() => api.runs(site.id), [site.id]);
  const [f, setF] = useState<MailForm>(EMPTY);
  const [rcpt, setRcpt] = useState("");
  const [pw, setPw] = useState("");
  const [clearPw, setClearPw] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "test" | "send">("");
  const [err, setErr] = useState<{ title: string; detail: string } | null>(null);
  const ref = useViewMotion<HTMLDivElement>([]);
  const d: MailSettings | null = cur.data;

  useEffect(() => {
    if (!d) return;
    const next = { ...EMPTY, ...d, password: null } as MailForm;
    setF(next); setRcpt((d.recipients || []).join(", ")); setPw(""); setClearPw(false);
  }, [d]);

  if (cur.error) return <ErrorBox text={cur.error} onRetry={cur.reload} />;
  if (!d) return <p className="faint">Loading…</p>;
  const last = (runs.data?.runs || []).find((r) => r.status === "completed" && (r.kind === "coverage" || r.kind === "full"));
  const form = (): MailForm => ({ ...f, recipients: rcpt.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean),
    password: clearPw ? "" : pw ? pw : null });
  const set = <K extends keyof MailForm>(k: K, v: MailForm[K]) => setF({ ...f, [k]: v });
  const act = async (what: "save" | "test" | "send") => {
    setBusy(what); setErr(null);
    try {
      if (what === "save") { await api.saveMail(form()); toast(f.enabled ? "Saved. The summary goes out after every check." : "Saved. E-mail is off."); cur.reload(); }
      else if (what === "test") { const r = await api.testMail(form()); toast(`Test e-mail sent to ${r.to.join(", ")}.`); }
      else if (last) { const r = await api.sendSummary(last.id); toast(`Summary of check ${last.id} sent to ${r.to} recipient(s). ${r.report_note}.`); cur.reload(); }
    } catch (e: any) { setErr({ title: e.title || "Something went wrong", detail: e.detail || e.message }); }
    finally { setBusy(""); }
  };
  const lr = d.last_result || {};

  return (
    <div className="view" ref={ref}>
      <div className="head">
        <div>
          <h1>Email summary</h1>
          <p className="lede">When a check finishes, QC e-mails a crawl summary (pages, status codes, DNS / network / timeout
            failures, duplicates skipped, timings, report and ticket status) with the Management report (Excel) attached.</p>
        </div>
      </div>

      {lr.at && <p className={lr.ok ? "good-note" : "error-box"} role="status" data-reveal>
        Last e-mail: check {lr.run_id}, {new Date(lr.at).toLocaleString()} — {lr.ok ? `sent to ${lr.to} recipient(s)` : `not sent: ${lr.error}`}</p>}

      {!d.can_edit ? (
        <section className="panel" data-reveal>
          <p>E-mail is <b>{d.enabled ? "on" : "off"}</b>{d.enabled && d.attach_report ? ", with the Management report attached" : ""}.
            Only an owner can change the SMTP settings.</p>
        </section>
      ) : (
        <form className="panel mail-form" data-reveal onSubmit={(e) => { e.preventDefault(); act("save"); }}>
          <label className="switch-row">
            <input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            <span><b>Send the summary after every check</b><small>Every check of more than one page: sitemap, sheet or typed pages.</small></span>
          </label>

          <fieldset>
            <legend>SMTP server</legend>
            <div className="mail-grid">
              <label>SMTP host<input value={f.host} placeholder="smtp.example.com" autoComplete="off" onChange={(e) => set("host", e.target.value.trim())} /></label>
              <label>Security
                <select value={f.security} onChange={(e) => { const v = e.target.value as MailForm["security"]; setF({ ...f, security: v, port: PORTS[v] }); }}>
                  <option value="starttls">STARTTLS (usually port 587)</option>
                  <option value="ssl">SSL/TLS (usually port 465)</option>
                  <option value="none">None (local relay only)</option>
                </select></label>
              <label>Port<input type="number" min={1} max={65535} value={f.port} onChange={(e) => set("port", Number(e.target.value))} /></label>
              <label>Username<input value={f.username} autoComplete="off" placeholder="optional" onChange={(e) => set("username", e.target.value)} /></label>
              <label>Password
                <input type="password" value={pw} autoComplete="new-password" disabled={clearPw}
                  placeholder={d.password_set ? "saved (leave empty to keep it)" : "optional"} onChange={(e) => setPw(e.target.value)} />
              </label>
              {d.password_set && <label className="check"><input type="checkbox" checked={clearPw} onChange={(e) => setClearPw(e.target.checked)} />Remove the saved password</label>}
            </div>
            {!d.secret_key_set && <p className="faint">The server needs QC6_SECRET_KEY set to store a password (it is encrypted with it).</p>}
            {f.security === "none" && <p className="faint">Without TLS the password and e-mail travel unencrypted: use it only for a relay on your own network.</p>}
          </fieldset>

          <fieldset>
            <legend>Who gets it</legend>
            <div className="mail-grid">
              <label>Sender e-mail<input type="email" value={f.sender} placeholder="qc@example.com" onChange={(e) => set("sender", e.target.value.trim())} /></label>
              <label className="wide">Recipients (comma separated, up to 20)
                <textarea rows={2} value={rcpt} placeholder="seo-team@example.com, lead@example.com" onChange={(e) => setRcpt(e.target.value)} /></label>
            </div>
            <label className="check"><input type="checkbox" checked={f.attach_report} onChange={(e) => set("attach_report", e.target.checked)} />
              Attach the Management report (Excel; checked to be complete before it's attached, up to {d.max_attach_mb ?? 20} MB)</label>
          </fieldset>

          {err && <div className="error-box" role="alert"><b>{err.title}</b><p>{err.detail}</p></div>}
          <div className="row" style={{ flexWrap: "wrap" }}>
            <button className="btn primary" disabled={!!busy}>{busy === "save" ? "Saving…" : "Save settings"}</button>
            <button type="button" className="btn" disabled={!!busy || !f.host || !f.sender || !rcpt.trim()} onClick={() => act("test")}>
              <Icon name="mail" />{busy === "test" ? "Sending…" : "Send test e-mail"}</button>
            {last && <button type="button" className="btn quiet" disabled={!!busy || !d.host} onClick={() => act("send")}>
              {busy === "send" ? "Sending…" : `Send the summary of check ${last.id} now`}</button>}
          </div>
        </form>
      )}
    </div>
  );
}
