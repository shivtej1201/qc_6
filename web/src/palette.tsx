import { useEffect, useRef, useState } from "react";
import { api, type Site } from "./api";
import { go } from "./main";
import { Icon, Sev } from "./ui";

/* Quick jump (Ctrl/Cmd+K or "/"): screens, fixes and pages by address or title. A combobox + listbox, all keyboard. */

export type Screen = { view: string; icon: string; label: string; words: string };

type Hit = { key: string; kind: "screen" | "fix" | "page"; label: string; sub?: string; icon?: string; sev?: any; run: () => void };

export function Palette({ site, screens, onClose }: { site?: Site; screens: Screen[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    input.current?.focus();
    return () => prev?.focus?.();
  }, []);

  useEffect(() => {
    const text = q.trim().toLowerCase();
    const scr: Hit[] = screens
      .filter((s) => !text || s.label.toLowerCase().includes(text) || s.words.includes(text))
      .map((s) => ({ key: `s-${s.view}`, kind: "screen", label: s.label, icon: s.icon, run: () => go(s.view) }));
    setHits(scr);
    setActive(0);
    if (!site || text.length < 2) return;
    let live = true;
    const t = window.setTimeout(async () => {
      const [fx, pg] = await Promise.all([
        api.fixes(site.id, "all", text).catch(() => null),
        api.pages(site.id, text).catch(() => null),
      ]);
      if (!live) return;
      const more: Hit[] = [
        ...(fx?.fixes || []).slice(0, 5).map((f) => ({ key: `f-${f.id}`, kind: "fix" as const, label: f.title, sev: f.severity,
          sub: f.template_name || "Site-wide", run: () => go("fixes", { fix: f.id }) })),
        ...(pg?.pages || []).slice(0, 6).map((p) => ({ key: `p-${p.url}`, kind: "page" as const, label: path(p.url),
          sub: p.title || p.template_name || "", run: () => go("page", { url: p.url }) })),
      ];
      setHits([...scr, ...more]);
    }, 180);
    return () => { live = false; window.clearTimeout(t); };
  }, [q, site?.id]);

  useEffect(() => {
    list.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const pick = (h?: Hit) => { if (h) { onClose(); h.run(); } };
  const key = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(hits.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(hits[active]); }
    else if (e.key === "Escape") { e.preventDefault(); onClose(); }
    else if (e.key === "Tab") { e.preventDefault(); }                     // keep focus in the dialog
  };
  const group = (k: Hit["kind"]) => ({ screen: "Go to", fix: "Fixes", page: "Pages" })[k];

  return (
    <>
      <div className="palette-back" onClick={onClose} />
      <div className="palette" role="dialog" aria-modal="true" aria-label="Quick jump">
        <div className="palette-input">
          <Icon name="search" />
          <input ref={input} role="combobox" aria-expanded="true" aria-controls="palette-list" aria-autocomplete="list"
            aria-activedescendant={hits[active] ? `pal-${active}` : undefined} aria-label="Jump to a screen, fix or page"
            placeholder="Jump to a screen, fix or page…" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={key} />
          <kbd>Esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" ref={list} aria-label="Results">
          {hits.length === 0 && <li className="palette-empty" role="presentation">Nothing matches.</li>}
          {hits.map((h, i) => [
            (i === 0 || hits[i - 1].kind !== h.kind) &&
              <li key={`g-${h.kind}`} role="presentation" className="palette-group">{group(h.kind)}</li>,
            <li key={h.key} id={`pal-${i}`} data-i={i} role="option" aria-selected={i === active}
              className={i === active ? "on" : ""} onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(h); }}>
              <span className="palette-row">
                {h.kind === "screen" ? <Icon name={h.icon!} /> : h.kind === "fix" ? <Sev s={h.sev} /> : <Icon name="page" />}
                <span className="palette-label">{h.label}{h.sub && <span className="faint"> · {h.sub}</span>}</span>
              </span>
            </li>,
          ])}
        </ul>
        <p className="palette-foot faint">↑ ↓ to move · Enter to open · Ctrl K or / to open this from anywhere</p>
      </div>
    </>
  );
}

function path(u: string) {
  try { const x = new URL(u); return x.pathname + x.search; } catch { return u; }
}
