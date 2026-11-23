import { useCallback, useEffect, useRef, useState } from "react";
import { localStorageGet, localStorageSet } from "./api";

/* Keep the screen awake (Screen Wake Lock API) until the person turns it off. The browser drops the lock whenever the
   tab is hidden or the window minimised; it is taken again as soon as QC is visible, for as long as the toggle is on.
   The choice is remembered on this computer. Needs a secure page (https, or QC on this machine: localhost / 127.0.0.1). */

type Sentinel = { released: boolean; release: () => Promise<void>; addEventListener: (t: "release", f: () => void) => void };
type Nav = Navigator & { wakeLock?: { request: (type: "screen") => Promise<Sentinel> } };

export type WakeState = "off" | "on" | "waiting" | "unsupported" | "refused";

export function wakeLockSupported(): boolean {
  return typeof navigator !== "undefined" && "wakeLock" in navigator && (typeof window === "undefined" || window.isSecureContext);
}

export function useScreenAwake() {
  const [wanted, setWanted] = useState(() => localStorageGet("qc6-awake") === "1");
  const [state, setState] = useState<WakeState>(wakeLockSupported() ? "off" : "unsupported");
  const sentinel = useRef<Sentinel | null>(null);

  const acquire = useCallback(async () => {
    if (!wakeLockSupported()) { setState("unsupported"); return; }
    if (document.visibilityState !== "visible") { setState("waiting"); return; }
    if (sentinel.current && !sentinel.current.released) { setState("on"); return; }
    try {
      const s = await (navigator as Nav).wakeLock!.request("screen");
      sentinel.current = s;
      s.addEventListener("release", () => { setState((cur) => (cur === "off" ? "off" : "waiting")); });
      setState("on");
    } catch {
      setState("refused");                     // e.g. battery saver, or the browser's policy says no
    }
  }, []);

  const release = useCallback(async () => {
    const s = sentinel.current;
    sentinel.current = null;
    setState(wakeLockSupported() ? "off" : "unsupported");
    if (s && !s.released) { try { await s.release(); } catch { /* already gone */ } }
  }, []);

  useEffect(() => {
    if (wanted) acquire(); else release();
    if (!wanted) return;
    const again = () => { if (document.visibilityState === "visible") acquire(); else setState("waiting"); };
    document.addEventListener("visibilitychange", again);
    window.addEventListener("focus", again);
    return () => { document.removeEventListener("visibilitychange", again); window.removeEventListener("focus", again); };
  }, [wanted, acquire, release]);

  useEffect(() => () => { sentinel.current?.release().catch(() => undefined); }, []);

  const toggle = useCallback(() => {
    setWanted((w) => { localStorageSet("qc6-awake", w ? "0" : "1"); return !w; });
  }, []);
  return { wanted, state, toggle };
}

export const WAKE_TEXT: Record<WakeState, string> = {
  on: "Screen stays on",
  waiting: "On again when QC is in front",
  off: "Screen may sleep",
  unsupported: "This browser or address can't keep the screen on",
  refused: "The browser said no (battery saver?)",
};
