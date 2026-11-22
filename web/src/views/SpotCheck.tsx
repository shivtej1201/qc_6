import { useEffect, useRef, useState } from "react";
import { api, type Site } from "../api";
import { go } from "../main";
import { Icon, useToast } from "../ui";

/* Check one page, a pasted list, or a sheet of URLs. A spot check: its own results, site-wide figures untouched. */

export function SpotCheckButton({ site, primary = false }: { site: Site; primary?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className={`btn ${primary ? "primary" : ""}`} onClick={() => setOpen(true)}><Icon name="page" />Check pages</button>
      {open && <SpotCheckDialog site={site} onClose={() => setOpen(false)} />}
    </>
  );
}

function SpotCheckDialog({ site, onClose }: { site: Site; onClose: () => void }) {
  const [tab, setTab] = useState<"paste" | "file">("paste");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState("coverage");
  const [whole, setWhole] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const toast = useToast();

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLElement>("textarea, input, button")?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && box.current) {                       // keep focus inside the dialog
        const els = [...box.current.querySelectorAll<HTMLElement>("button, input, textarea, select, a[href]")].filter((x) => !x.hasAttribute("disabled"));
        if (!els.length) return;
        const first = els[0], last = els[els.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); prev?.focus?.(); };
  }, []);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const r = tab === "paste" ? await api.checkUrls(site.id, text, kind) : await api.checkUpload(site.id, file!, kind, whole);
      toast(`${r.urls === 1 ? "Checking that page" : `Checking ${r.urls.toLocaleString("en-IN")} pages`}.` +
        (r.other_hosts ? ` ${r.other_hosts} address${r.other_hosts === 1 ? " wasn't" : "es weren't"} on ${site.primary_host} and ${r.other_hosts === 1 ? "was" : "were"} left out.` : ""));
      onClose();
      go("live", { run: r.run_id });
    } catch (x: any) {
      setErr(x.detail || x.title || x.message);
    } finally { setBusy(false); }
  };
  const ready = tab === "paste" ? text.trim().length > 8 : !!file;
  return (
    <>
      <div className="palette-back" onClick={onClose} />
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="spot-title" ref={box}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
          <h2 id="spot-title">Check pages</h2>
          <button className="icon-btn" aria-label="Close" onClick={onClose}><Icon name="close" /></button>
        </div>
        <p className="dim" style={{ margin: "4px 0 14px" }}>Check one page or a list of pages of {site.primary_host} now. This is a spot check:
          you get its results, and the site's health score and fixes list stay as the last full check left them.</p>
        <form className="grid" onSubmit={start}>
          <div className="seg" role="group" aria-label="How">
            <button type="button" aria-pressed={tab === "paste"} onClick={() => setTab("paste")}>Type or paste addresses</button>
            <button type="button" aria-pressed={tab === "file"} onClick={() => setTab("file")}>Upload a sheet</button>
          </div>
          {tab === "paste" ? (
            <label className="grid" style={{ gap: 4 }}><b>Addresses</b>
              <textarea className="input" rows={5} value={text} onChange={(e) => setText(e.target.value)}
                placeholder={`${site.base_url}/\n${site.base_url}/personal-loan`} />
              <span className="faint" style={{ fontSize: 13 }}>One per line (or separated by spaces). Only addresses on {site.primary_host} and its subdomains are checked.</span>
            </label>
          ) : (
            <>
              <label className="grid" style={{ gap: 4 }}><b>Sheet</b>
                <input className="input" type="file" accept=".csv,.tsv,.txt,.xlsx,.xlsm,text/csv"
                  onChange={(e) => setFile(e.target.files?.[0] || null)} />
                <span className="faint" style={{ fontSize: 13 }}>CSV, TSV, TXT or Excel (.xlsx), up to 20 MB and 3,00,000 addresses. QC uses the column
                  headed URL, Link, Address or Page, or any cell starting with http.</span>
              </label>
              <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={whole} onChange={(e) => setWhole(e.target.checked)} />
                This list is the whole site: count it as a full check (health score, fixes list)</label>
            </>
          )}
          <fieldset className="row" style={{ border: 0, padding: 0, margin: 0, gap: 16 }}>
            <legend className="sr-only">What to check</legend>
            <label className="row" style={{ gap: 6 }}><input type="radio" name="spot-kind" checked={kind === "coverage"} onChange={() => setKind("coverage")} />
              Page check (fast)</label>
            <label className="row" style={{ gap: 6 }}><input type="radio" name="spot-kind" checked={kind === "full"} onChange={() => setKind("full")} />
              Page check and a real-browser visit</label>
          </fieldset>
          {err && <div className="error-box">{err}</div>}
          <div className="row"><button className="btn primary" type="submit" disabled={!ready || busy}>{busy ? "Starting…" : "Start the check"}</button>
            <button className="btn quiet" type="button" onClick={onClose}>Cancel</button></div>
        </form>
      </div>
    </>
  );
}
