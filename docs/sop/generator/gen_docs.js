// Builds QC_V6_SOP.docx and QC_V6_User_Guide.docx
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell, WidthType, ShadingType,
  BorderStyle, LevelFormat, ImageRun, PageBreak, Header, Footer, PageNumber, TableOfContents, TabStopType,
} = require("docx");

const SHOTS = "/tmp/claude-0/-home-user/000be443-7185-5799-9970-0537d979befc/scratchpad/docshots/";
const OUT = process.argv[2];
const TEAL = "09644F", INK = "25201E", MUTED = "5A4B42", SAND = "F4EBE2", LINE = "D9CBBF";
const PAGE_W = 11906, MARGIN = 1134, CONTENT = PAGE_W - 2 * MARGIN;   // A4, 2 cm margins

// ---------------------------------------------------------------- helpers
const t = (text, o = {}) => new TextRun({ text, ...o });
function runs(s) {             // **bold** and `code` inline
  const out = [];
  s.split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach((p) => {
    if (!p) return;
    if (p.startsWith("**")) out.push(t(p.slice(2, -2), { bold: true }));
    else if (p.startsWith("`")) out.push(t(p.slice(1, -1), { font: "Consolas", size: 20, color: TEAL }));
    else out.push(t(p));
  });
  return out;
}
const P = (s, o = {}) => new Paragraph({ children: runs(s), spacing: { after: 120 }, ...o });
const H1 = (s) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [t(s)], pageBreakBefore: true });
const H1n = (s) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [t(s)] });
const H2 = (s) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [t(s)] });
const H3 = (s) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [t(s)] });
const B = (s, lvl = 0) => new Paragraph({ numbering: { reference: "bul", level: lvl }, children: runs(s), spacing: { after: 60 } });
let numSeq = 0;
function steps(items) {       // numbered list that restarts each time
  const ref = `num${numSeq++}`;
  NUMS.push({ reference: ref, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
    style: { paragraph: { indent: { left: 540, hanging: 360 } } } }] });
  return items.map((s) => new Paragraph({ numbering: { reference: ref, level: 0 }, children: runs(s), spacing: { after: 80 } }));
}
const NUMS = [];
const border = { style: BorderStyle.SINGLE, size: 4, color: LINE };
const borders = { top: border, bottom: border, left: border, right: border };
function table(head, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  const w = widths.map((x) => Math.round((x / total) * CONTENT));
  w[w.length - 1] = CONTENT - w.slice(0, -1).reduce((a, b) => a + b, 0);
  const cell = (s, i, hdr) => new TableCell({
    borders, width: { size: w[i], type: WidthType.DXA }, margins: { top: 70, bottom: 70, left: 110, right: 110 },
    shading: hdr ? { fill: TEAL, type: ShadingType.CLEAR, color: "auto" } : undefined,
    children: String(s).split("\n").map((line) => new Paragraph({ children: hdr ? [t(line, { bold: true, color: "FFFFFF" })] : runs(line), spacing: { after: 40 } })),
  });
  return new Table({
    width: { size: CONTENT, type: WidthType.DXA }, columnWidths: w,
    rows: [new TableRow({ tableHeader: true, children: head.map((h, i) => cell(h, i, true)) }),
      ...rows.map((r) => new TableRow({ children: r.map((c, i) => cell(c, i, false)) }))],
  });
}
const gap = () => new Paragraph({ children: [], spacing: { after: 60 } });
function note(label, s) {
  return new Table({ width: { size: CONTENT, type: WidthType.DXA }, columnWidths: [CONTENT], rows: [new TableRow({ children: [
    new TableCell({ width: { size: CONTENT, type: WidthType.DXA }, borders: { top: border, bottom: border, left: border, right: border },
      shading: { fill: SAND, type: ShadingType.CLEAR, color: "auto" }, margins: { top: 100, bottom: 100, left: 160, right: 160 },
      children: [new Paragraph({ children: [t(label + "  ", { bold: true, color: TEAL }), ...runs(s)] })] })] })] });
}
function img(file, widthIn, caption) {
  const p = path.join(SHOTS, file);
  if (!fs.existsSync(p)) return [];
  const png = fs.readFileSync(p);                                  // size from the PNG, pixels from the smaller JPEG
  const wPx = png.readUInt32BE(16), hPx = png.readUInt32BE(20);
  const buf = fs.readFileSync(p.replace(/\.png$/, ".jpg"));
  const w = Math.round(widthIn * 96), h = Math.round((w * hPx) / wPx);
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 40 }, keepNext: true,
      children: [new ImageRun({ type: "jpg", data: buf, transformation: { width: w, height: h },
        altText: { title: caption, description: caption, name: file } })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [t(caption, { italics: true, size: 18, color: MUTED })] }),
  ];
}
function coverPage(title, subtitle, meta) {
  return [
    new Paragraph({ spacing: { before: 2400, after: 120 }, children: [t("QC V6", { bold: true, size: 28, color: TEAL, characterSpacing: 40 })] }),
    new Paragraph({ spacing: { after: 200 }, children: [t(title, { bold: true, size: 64, color: INK })] }),
    new Paragraph({ spacing: { after: 600 }, children: [t(subtitle, { size: 28, color: MUTED })] }),
    table(["Item", "Detail"], meta, [1, 3]),
    new Paragraph({ children: [new PageBreak()] }),
    new Paragraph({ heading: HeadingLevel.HEADING_1, children: [t("Contents")] }),
    new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
    new Paragraph({ children: [t("Right-click the table and choose Update Field if page numbers are missing.", { italics: true, size: 18, color: MUTED })] }),
  ];
}
function build(docTitle, children) {
  return new Document({
    creator: "QC V6", title: docTitle, features: { updateFields: true },
    styles: {
      default: { document: { run: { font: "Calibri", size: 22, color: INK } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: "Calibri", size: 36, bold: true, color: TEAL }, paragraph: { spacing: { before: 240, after: 160 }, keepNext: true, outlineLevel: 0 } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: "Calibri", size: 28, bold: true, color: INK }, paragraph: { spacing: { before: 240, after: 120 }, keepNext: true, outlineLevel: 1 } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { font: "Calibri", size: 24, bold: true, color: MUTED }, paragraph: { spacing: { before: 180, after: 80 }, keepNext: true, outlineLevel: 2 } },
      ],
    },
    numbering: { config: [
      { reference: "bul", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 300 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "–", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 1000, hanging: 300 } } } }] },
      ...NUMS] },
    sections: [{
      properties: { page: { size: { width: PAGE_W, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
        children: [t(docTitle, { size: 16, color: MUTED })] })] }) },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
        children: [t("Page ", { size: 16, color: MUTED }), new TextRun({ children: [PageNumber.CURRENT], size: 16, color: MUTED }),
          t(" of ", { size: 16, color: MUTED }), new TextRun({ children: [PageNumber.TOTAL_PAGES], size: 16, color: MUTED })] })] }) },
      children,
    }],
  });
}

// ---------------------------------------------------------------- shared facts
const SPEEDS = [
  ["Careful", "8 – 10", "A site or CDN you have not measured yet"],
  ["Standard", "12", "Default for lists under 50,000 pages: 10,000 pages in about 16 minutes"],
  ["Fast", "14", "A site known to take it"],
  ["Large site", "16 – 18", "Default from 50,000 pages: 3 lakh pages in about 5 – 6 hours (Standard: about 7½ h)"],
];
const SHEETS = [
  ["Summary", "The check in plain numbers and which sheet to open next"],
  ["Ops Ticket", "One ticket text for the whole check, ready to paste"],
  ["Broken Links", "Every broken link: status in words, on how many pages, link text, example pages, what to do"],
  ["Broken Images", "Every image that does not load, with its pages"],
  ["Redirects", "Every link that redirects, where it goes, on how many pages"],
  ["Pages Not OK", "Pages in the list that do not answer 200"],
  ["Tickets", "One row per issue ticket (QC-number) with its text"],
  ["Status Codes", "Every exact status code seen for pages, links and images"],
  ["All URLs (Unique) / Images (Unique)", "Every address found, whatever it answered, with first page and example pages"],
  ["Executive Summary … Validation Cache Reuse", "The detailed QC V5-style sheets, for analysts"],
];
const CODES = [
  ["200", "OK", "Working"], ["301", "Moved Permanently", "Redirect: update the link to the final address"],
  ["302 / 307", "Found / Temporary Redirect", "Temporary redirect: check it is meant to be temporary"],
  ["308", "Permanent Redirect", "As 301"], ["403", "Forbidden", "Blocked, often by a firewall or CDN"],
  ["404", "Not Found", "Broken: restore the page or remove / fix the link"], ["410", "Gone", "Removed on purpose: remove the link"],
  ["429", "Too Many Requests", "The site asked QC to slow down; QC slows by itself"],
  ["500", "Internal Server Error", "Server problem: check the server logs"], ["502 / 503 / 504", "Bad Gateway / Unavailable / Timeout", "Server or CDN overloaded or down"],
  ["Timeout", "No answer in time", "Slow or unreachable page"], ["DNS / Network", "Address not found", "Wrong host name or network problem"],
];
const PRI = [["Critical", "P1 - Critical"], ["High", "P2 - High"], ["Medium", "P3 - Medium"], ["Low", "P4 - Low"]];

// ================================================================= SOP
function sop() {
  const c = [];
  c.push(...coverPage("Standard Operating Procedure", "Website URL quality checks with QC V6: from sitemap to Ops ticket", [
    ["Document", "QC V6 – Standard Operating Procedure (SOP)"], ["Applies to", "QC analysts, Ops / web team, QC owner (admin)"],
    ["System", "QC V6 web application (Windows, macOS, Linux)"], ["Version", "1.0 – November 2026"],
    ["Related", "QC V6 User Guide; README.md in the project"],
  ]));
  c.push(H1("1. Purpose and scope"));
  c.push(P("This SOP describes how the team checks a website's URLs with QC V6 and gets problems fixed: getting the page list, running the check, reading the results, raising tickets for the Ops team, and confirming the fix."));
  c.push(H2("1.1 What QC checks"));
  [ "Every page in the list (from a sitemap, a sheet or one URL): its HTTP status, redirects, title, headings and other on-page rules.",
    "Every **link** and every **image** found on those pages. Each unique address is checked **once**, however many pages use it, and reported with the pages it is on.",
    "**Not checked:** CSS, JavaScript, fonts, preloads and source maps. They are not recorded or requested.",
  ].forEach((s) => c.push(B(s)));
  c.push(H2("1.2 Out of scope"));
  [ "Content loaded later by JavaScript from an API is not link-checked by the normal crawl (see Troubleshooting).",
    "QC never tries to get round a block. If a CDN or firewall refuses QC, the CDN team must allow it.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("2. Roles and responsibilities"));
  c.push(table(["Role", "Responsibilities"], [
    ["QC analyst", "Starts QC, runs checks, reviews results, raises tickets with attachments, marks them raised, re-checks after fixes."],
    ["Ops / web team", "Receives tickets, fixes links, redirects, images and pages, tells QC when the fix is live."],
    ["QC owner (admin)", "Installs and updates QC, manages sign-in accounts, owns backups and Clear all data, agrees crawl speeds with the CDN team."],
  ], [1, 3]));

  c.push(H1("3. System overview"));
  c.push(table(["Part", "What it does"], [
    ["Web app", "The screens you use in the browser (http://127.0.0.1:8003). Ships pre-built; Node is not needed."],
    ["API server", "Serves the web app and the data."],
    ["Worker", "Runs the checks in the background (its own window on Windows). Checks wait in the queue until a worker runs."],
    ["PostgreSQL 16", "Database \"qc6\": sites, checks, problems, tickets, accounts."],
    ["Data folder (var/)", "Crawl databases, Excel/ZIP reports, link data, screenshots."],
    ["Chromium", "Downloaded on first start; used only for optional real-browser checks."],
  ], [1, 3]));
  c.push(gap());
  c.push(note("Same code everywhere.", "Windows, macOS and Linux run exactly the same application. Only the start script differs: `start.bat` on Windows, `start.command` / `start.sh` on macOS and Linux."));

  c.push(H1("4. Installation (QC owner, once per computer)"));
  c.push(H2("4.1 Windows"));
  c.push(...steps([
    "Install **Python 3.11 or newer** from python.org and tick **Add python.exe to PATH**.",
    "Install **PostgreSQL 16** from postgresql.org and note the password you set for the `postgres` user.",
    "Unzip QC (or clone the repository) into a folder, for example `C:\\QC`.",
    "Double-click **start.bat**. The first start creates the environment, installs packages and Chromium (a few minutes).",
    "When asked, type the PostgreSQL password. The `qc6` database is created for you.",
    "The browser opens **http://127.0.0.1:8003**. Two windows stay open: the server and **QC V6 worker**. Keep both open while checks run.",
  ]));
  c.push(H2("4.2 macOS"));
  c.push(...steps([
    "Install Python 3.12 and PostgreSQL 16, for example `brew install python@3.12 postgresql@16` then `brew services start postgresql@16` (or use Postgres.app).",
    "Unzip QC. The first time, right-click **start.command** and choose **Open** (macOS asks once because the file came from the internet).",
    "QC finds the database by itself (Homebrew / Postgres.app need no password) and opens http://127.0.0.1:8003.",
    "Stop QC with **Ctrl+C** in that window; the worker stops with it.",
  ]));
  c.push(H2("4.3 Linux"));
  c.push(P("Install Python 3.11+ and PostgreSQL 16, then run `./start.sh` in the QC folder."));
  c.push(H2("4.4 Check the installation"));
  [ "The New check screen opens without the red **The QC worker isn't running** box.",
    "Run a check of one page (One page tab) and confirm it finishes with **Report ready**.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("5. Operating procedure"));
  c.push(H2("5.1 Before each session"));
  c.push(...steps([
    "Start QC (start.bat / start.command / start.sh). Keep the server and worker windows open.",
    "On a laptop: set power to **never sleep while plugged in**, and switch on **Keep screen awake** at the bottom of the menu. If the computer sleeps, the check pauses until QC runs again; it then carries on from where it stopped.",
    "Confirm the site is reachable and you know which speed its CDN accepts (section 7).",
  ]));
  c.push(H2("5.2 Get the URL list"));
  c.push(...steps([
    "Open **New check**.",
    "Choose the source: **Sitemap** (paste the https:// sitemap address; sitemap indexes are followed), **One page**, or **Upload a file** (CSV, Excel .xlsx, text, or sitemap files saved from the browser).",
    "Click **Get the URLs**. QC lists the addresses live, removes duplicates and tracking parameters, and shows the count.",
    "Optional: **Download URLs** (Excel / CSV) to confirm the list is what you expected.",
  ]));
  c.push(H2("5.3 Start the check"));
  c.push(...steps([
    "Pick the **Speed** (section 7). Each option shows how long this list will take.",
    "Optional: tick **Also open sample pages in a real browser** for speed, layout and console errors (slower).",
    "Click **Start crawling**. The screen shows pages done, speed and time left, updated every second.",
    "Use **Pause**, **Resume** or **Cancel** at any time. Nothing already checked is lost.",
  ]));
  c.push(H2("5.4 Review the results"));
  [ "The four numbers: **Pages not OK**, **Broken links**, **Broken images**, **Redirecting links**, each with how many pages have one.",
    "**Problems** tab: the top 10 of each list with what to do.",
    "**Ops ticket** tab: one summary ticket for the whole check, with Copy buttons.",
    "**Download report → Full audit workbook** for the Excel report (section 6).",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("6. Reports"));
  c.push(P("The full audit workbook opens with sheets anyone can read in a minute. Status codes are written in words, for example \"404 Not Found\" or \"301 Moved Permanently -> 200 OK\"."));
  c.push(table(["Sheet", "Contents"], SHEETS, [1, 2]));

  c.push(H1("7. Speed rules"));
  c.push(table(["Speed", "Requests / second", "Use for"], SPEEDS, [1, 1, 3]));
  c.push(gap());
  [ "Lists under 50,000 pages start at **Standard**; from 50,000 at **Large site**. A limit set for the site stays its limit.",
    "QC slows down by itself when the site answers slowly or says \"too many requests\" (429). It identifies itself honestly and respects robots.txt and Crawl-delay.",
    "Agree any speed above Standard with the CDN / hosting team before using it on production.",
    "Measured on a test site: 10,000 pages in 15 min 58 s at Standard. Real sites depend on their server and CDN.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("8. Raising tickets for the Ops team"));
  c.push(P("QC keeps **one ticket per issue type**, not one per URL. For example, all links that redirect (3xx) are one ticket, all broken links another, and each kind of page problem (soft 404, structured data missing) another. Every affected URL is in the ticket's Excel attachment."));
  c.push(H2("8.1 Procedure"));
  c.push(...steps([
    "Open **Tickets**. The **To raise** view lists every issue that still needs a ticket, worst first.",
    "Open a ticket. Check the issue, priority and the affected count, for example \"100 URLs on 10,000 pages\".",
    "Click **Copy title** and paste it as the ticket title in the Ops team's tool.",
    "Click **Copy the whole ticket** and paste it as the description.",
    "Click **Download attachment (Excel)** and attach the file to the ticket.",
    "Type the number the Ops tool gave (for example OPS-1234) and click **Mark raised**. The ticket moves to **Raised**.",
    "When Ops reports the fix is live, run the check again. QC moves the ticket to **Closed** when none of its addresses fail.",
  ]));
  c.push(H2("8.2 Priority"));
  c.push(table(["QC severity", "Ticket priority"], PRI, [1, 1]));
  c.push(H2("8.3 Ticket template"));
  c.push(P("The ticket text comes from the site's template (**Tickets → Ticket template**). Change it to match the Ops team's tool; `{placeholders}` such as `{issue}`, `{priority}`, `{affected}`, `{url_list}` and `{attachment}` are filled in. **Use QC's template** restores the default."));
  c.push(H2("8.4 Attachment contents"));
  [ "**About**: the ticket text.", "**Affected addresses**: each address, status or detail, where it redirects, on how many pages, example page, page type.",
    "**Where found**: every page and address pair. Page-type issues get **Affected pages** instead.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("9. Data management"));
  c.push(H2("9.1 Clear all data"));
  c.push(note("Irreversible.", "Clear all data erases every site, check, URL list, report, problem and ticket from the database, the stored files on disk, and QC's saved data in the browser. Sign-in accounts and the audit log stay."));
  c.push(gap());
  c.push(...steps([
    "Pause or cancel any running check (clearing is refused while one runs).",
    "Take a backup if the data may be needed later (9.2).",
    "Owner only: click **Clear all data…** at the bottom of the menu, type **DELETE** and confirm.",
  ]));
  c.push(H2("9.2 Backup and restore"));
  [ "Database: `pg_dump -U postgres qc6 > qc6-backup.sql` (restore with `psql -U postgres qc6 < qc6-backup.sql`).",
    "Files: copy the QC `var` folder.",
    "Single check or site: delete it from its screen instead of clearing everything.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("10. Troubleshooting"));
  c.push(table(["Symptom", "What to do"], [
    ["Red box: The QC worker isn't running", "Start QC again with the start script. On Windows keep the \"QC V6 worker\" window open."],
    ["Could not reach PostgreSQL", "Start the PostgreSQL service and check the password, then start QC again."],
    ["Check the address", "A sitemap or page address must start with https://."],
    ["Sitemap refused (HTTP 403 from the CDN)", "Open the sitemap in your browser, save it (Ctrl+S) and use Upload a file. For the crawl, use Show the allow rule and send it to the CDN team."],
    ["Crawl slower than expected", "The site is answering slowly or asked QC to slow down. Check the speed choice, and the site's server or CDN."],
    ["Check stuck after the computer slept or the worker closed", "Start the worker again: the check carries on from where it stopped."],
    ["Keep screen awake greyed out", "Open QC as http://127.0.0.1:8003 on the same computer, or over https."],
    ["Links inside API-loaded widgets not checked", "Known limit of the HTML crawl. Use the real-browser option for samples; a rendered check is planned."],
  ], [2, 3]));

  c.push(H1("11. Updates and change control"));
  [ "Changes go through pull requests on the project repository; CI runs the full test suite on Linux and on a real Mac.",
    "To update: get the latest main (git pull or a new zip) and start QC. Packages and database changes are applied automatically on start.",
    "After an update, run a one-page check to confirm QC works before starting long checks.",
  ].forEach((s) => c.push(B(s)));

  c.push(H1("Appendix A. Status codes"));
  c.push(table(["Code", "Meaning", "Action"], CODES, [1, 2, 3]));
  c.push(H1n("Appendix B. Glossary"));
  c.push(table(["Term", "Meaning"], [
    ["Check", "One run of QC over a list of pages."], ["Issue type", "A kind of problem, for example \"Links that redirect (3xx)\"."],
    ["Ticket (QC-0001)", "QC's record for one issue type on a site, raised in the Ops tool with an attachment."],
    ["Unique URL", "An address checked once, however many pages link to it."], ["Found on", "The pages where an address appears."],
    ["Worker", "The background process that runs checks."],
  ], [1, 3]));
  return build("QC V6 – Standard Operating Procedure", c);
}

// ================================================================= User guide
function guide() {
  const c = [];
  c.push(...coverPage("User Guide", "Check a website's pages, links and images and raise tickets for the Ops team", [
    ["Document", "QC V6 – User Guide"], ["For", "Everyone who runs checks or raises tickets"], ["Version", "1.0 – November 2026"],
    ["See also", "QC V6 Standard Operating Procedure (installation, roles, data management)"],
  ]));
  c.push(H1("1. Getting started"));
  c.push(P("Start QC with **start.bat** (Windows) or **start.command** (Mac). Your browser opens **http://127.0.0.1:8003**. Keep QC's windows open while checks run."));
  c.push(H2("The menu"));
  c.push(table(["Menu item", "Use it to"], [
    ["New check", "Start a check from a sitemap, a sheet or one URL, follow it, and see its result"],
    ["Problems", "Every problem QC found, worst first"], ["Tickets", "One ticket per issue type, ready to raise with the Ops team"],
    ["Links & images", "Explore every link and image and their status"], ["More tools", "Today, Insights, Growth, All pages, Journeys, Live run"],
    ["Keep screen awake", "Stop the screen from sleeping while QC is open"], ["Clear all data…", "Owners: erase all of QC's data (asks you to type DELETE)"],
  ], [1, 3]));
  c.push(...img("01-new-check.png", 6.2, "New check: choose Sitemap, One page or Upload a file"));

  c.push(H1("2. Run a check"));
  c.push(...steps([
    "Open **New check** and choose **Sitemap**, **One page** or **Upload a file**.",
    "Paste the address (it must start with https://) or pick your file, then click **Get the URLs**.",
    "Check the count. **Download URLs** gives the list as Excel or CSV.",
    "Pick a **Speed**: QC shows how long this list will take at each speed.",
    "Click **Start crawling**.",
  ]));
  c.push(...img("02-ready-to-start.png", 6.2, "Ready to start: the URL list, the speed choice and Start crawling"));
  c.push(H2("While it runs"));
  c.push(P("The screen shows pages done, pages per second and time left. **Pause** stops after the pages in hand; **Resume** carries on; **Cancel** stops for good. Nothing already checked is lost."));
  c.push(...img("03-crawling.png", 6.2, "A check in progress"));
  c.push(note("Tip.", "For long checks switch on **Keep screen awake** and keep the laptop plugged in. If the computer sleeps, the check carries on from where it stopped when QC runs again."));

  c.push(H1("3. Read the result"));
  c.push(P("When the check finishes you see four numbers. Red means there is something to fix."));
  c.push(table(["Number", "Means"], [
    ["Pages not OK", "Pages in your list that do not answer 200 (errors, redirects)"], ["Broken links", "Links on the pages that lead to 4xx, 5xx or no answer"],
    ["Broken images", "Images that do not load"], ["Redirecting links", "Links that go through a redirect (3xx) before the final page"],
  ], [1, 3]));
  c.push(...img("04-result.png", 6.2, "The result: four numbers, then the top problems with what to do"));
  c.push(H2("Ops ticket tab"));
  c.push(P("One ticket for the whole check, with **Copy title** and **Copy the whole ticket**. Use it when one summary ticket is enough; use **Tickets** for one ticket per issue type."));
  c.push(...img("05-ops-ticket.png", 6.2, "Ops ticket: a summary ticket for the whole check"));

  c.push(H1("4. Raise tickets"));
  c.push(P("**Tickets** has one ticket per issue type. For example, 100 redirecting links are **one** ticket, with every URL in its Excel attachment."));
  c.push(...img("07-tickets.png", 6.2, "Tickets: one per issue type, with how many URLs and pages"));
  c.push(...steps([
    "Click a ticket to open it.",
    "**Copy title** and **Copy the whole ticket** into the Ops team's tool.",
    "**Download attachment (Excel)** and attach it.",
    "Type the Ops ticket number, for example OPS-1234, and click **Mark raised**.",
    "After Ops fixes it, run the check again: QC closes the ticket when the issue is gone.",
  ]));
  c.push(...img("08-ticket-open.png", 6.2, "An open ticket: copy it, download the attachment, mark it raised"));
  c.push(H2("The attachment"));
  [ "**About**: the ticket.", "**Affected addresses**: each URL, its status, where it redirects, pages, example page.",
    "**Where found**: every page and URL pair.",
  ].forEach((s) => c.push(B(s)));
  c.push(H2("Change the ticket template"));
  c.push(P("Click **Ticket template** to change the title and text to match your Ops tool. Words in `{braces}` are filled in for each ticket; the list is under the editor. **Use QC's template** puts the default back."));
  c.push(...img("09-template.png", 6.2, "The ticket template editor"));

  c.push(H1("5. Problems and Links & images"));
  c.push(P("**Problems** lists every problem, worst first, with how many pages it touches. **Links & images** lets you explore every link and image found, with its status."));
  c.push(...img("10-problems.png", 6.2, "Problems"));

  c.push(H1("6. Download the Excel report"));
  c.push(P("On the result, click **Download report → Full audit workbook**. The first sheets are the simple ones:"));
  c.push(table(["Sheet", "Contents"], SHEETS, [1, 2]));

  c.push(H1("7. Settings and tools"));
  c.push(...img("12-menu-tools.png", 2.6, "Bottom of the menu"));
  [ "**Keep screen awake**: on until you switch it off. Works when QC is opened at 127.0.0.1 or over https.",
    "**Switch light / dark**: change the look.",
    "**Clear all data…** (owners): erases every site, check, report, problem and ticket, and their files. Type **DELETE** to confirm. It can't be undone.",
  ].forEach((s) => c.push(B(s)));
  c.push(...img("13-clear-dialog.png", 4.6, "Clear all data asks you to type DELETE"));
  c.push(H2("On a phone"));
  c.push(P("QC works on a phone screen: the menu moves to a bar at the bottom (Check, Problems, Tickets, More)."));
  c.push(...img("14-phone.png", 2.4, "QC on a phone"));

  c.push(H1("8. Questions"));
  c.push(table(["Question", "Answer"], [
    ["Why are CSS and JavaScript files not in the report?", "QC checks page URLs and image URLs only; code files are skipped to save requests."],
    ["How long will a check take?", "The Speed choice shows it before you start. Standard: 10,000 pages in about 16 minutes."],
    ["The site refused the sitemap (403).", "Open the sitemap in your browser, save it, and use Upload a file."],
    ["Why one ticket for 100 URLs?", "Ops fixes an issue type together; the attachment lists every URL and page."],
    ["A ticket says Closed.", "A later check found none of its addresses failing."],
    ["Does it work on Mac and Windows?", "Yes, the same application on both."],
  ], [2, 3]));
  return build("QC V6 – User Guide", c);
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, "QC_V6_SOP.docx"), await Packer.toBuffer(sop()));
  fs.writeFileSync(path.join(OUT, "QC_V6_User_Guide.docx"), await Packer.toBuffer(guide()));
  console.log("ok");
})();
