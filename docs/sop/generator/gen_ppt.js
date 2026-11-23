const pptxgen = require("pptxgenjs");
const fs = require("fs");
const path = require("path");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const fa = require("react-icons/fa");
const { applyTheme } = require(process.argv[3] + "/scripts/apply_theme.js");

const OUT = process.argv[2];
const SHOTS = "/tmp/claude-0/-home-user/000be443-7185-5799-9970-0537d979befc/scratchpad/docshots/";
const THEME = { name: "QC V6", headFontFace: "Calibri", bodyFontFace: "Calibri",
  colors: { dk1: "25201E", lt1: "FFFFFF", dk2: "0B4D3E", lt2: "F4EBE2", accent1: "09644F", accent2: "A43C27", accent3: "996300",
    accent4: "3D5E78", accent5: "5A4B42", accent6: "75DAC0", hlink: "09644F", folHlink: "5A4B42" } };
const HEX = { teal: "09644F", deep: "0B4D3E", sand: "F4EBE2", ink: "25201E", muted: "5A4B42", coral: "A43C27", ochre: "996300", white: "FFFFFF", line: "D9CBBF" };

async function icon(Comp, color) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color: "#" + color, size: 256 }));
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}
const shot = (f) => "image/jpeg;base64," + fs.readFileSync(path.join(SHOTS, f.replace(/\.png$/, ".jpg"))).toString("base64");
function ratio(f) { const b = fs.readFileSync(path.join(SHOTS, f)); return b.readUInt32BE(20) / b.readUInt32BE(16); }

(async () => {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";            // 13.33 x 7.5
  pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
  pres.title = "QC V6 – Standard Operating Procedure";
  pres.company = "QC V6";
  const C = pres.SchemeColor;

  pres.defineSlideMaster({ title: "QC_TITLE", background: { color: HEX.deep },
    objects: [{ placeholder: { options: { name: "title", type: "title", x: 0.8, y: 2.3, w: 11.7, h: 1.6, fontSize: 48, bold: true, color: C.background1, valign: "bottom", align: "left", margin: 0 }, text: "" } },
      { placeholder: { options: { name: "body", type: "body", x: 0.8, y: 4.1, w: 11.7, h: 1.2, fontSize: 20, color: C.background2, valign: "top", align: "left", margin: 0 }, text: "" } }] });
  pres.defineSlideMaster({ title: "QC_CONTENT", background: { color: HEX.white },
    objects: [{ placeholder: { options: { name: "title", type: "title", x: 0.6, y: 0.4, w: 12.1, h: 0.9, fontSize: 36, bold: true, color: C.text1, valign: "middle", align: "left", margin: 0 }, text: "" } },
      { text: { text: "QC V6 · Standard Operating Procedure", options: { x: 0.6, y: 7.0, w: 6, h: 0.3, fontSize: 10, color: C.accent5, margin: 0 } } }],
    slideNumber: { x: 12.2, y: 7.0, w: 0.5, h: 0.3, fontSize: 10, color: HEX.muted } });

  const I = {
    page: await icon(fa.FaFileAlt, HEX.white), link: await icon(fa.FaLink, HEX.white), image: await icon(fa.FaImage, HEX.white),
    ban: await icon(fa.FaBan, HEX.white), user: await icon(fa.FaUserCheck, HEX.white), tools: await icon(fa.FaTools, HEX.white),
    crown: await icon(fa.FaUserShield, HEX.white), win: await icon(fa.FaWindows, HEX.white), apple: await icon(fa.FaApple, HEX.white),
    ticket: await icon(fa.FaTicketAlt, HEX.white), trash: await icon(fa.FaTrashAlt, HEX.white), db: await icon(fa.FaDatabase, HEX.white),
    sun: await icon(fa.FaSun, HEX.white), check: await icon(fa.FaCheck, HEX.white),
  };
  const circle = (s, x, y, d, img, fill = HEX.teal, name = "icon") => {
    s.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color: fill }, line: { color: fill }, objectName: name + " circle" });
    s.addImage({ data: img, x: x + d * 0.25, y: y + d * 0.25, w: d * 0.5, h: d * 0.5, objectName: name });
  };
  const card = (s, x, y, w, h, name, fill = HEX.sand) =>
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.12, fill: { color: fill }, line: { color: fill }, objectName: name });
  const text = (s, str, o) => s.addText(str, { isTextBox: true, margin: 0, color: HEX.ink, fontSize: 16, valign: "top", ...o });
  const bullets = (s, items, o) => s.addText(items.map((it, i) => ({ text: it, options: { bullet: true, breakLine: i < items.length - 1 } })),
    { isTextBox: true, margin: 0, color: HEX.ink, fontSize: 16, paraSpaceAfter: 8, valign: "top", ...o });
  const numbered = (s, items, o) => s.addText(items.map((it, i) => ({ text: it, options: { bullet: { type: "number" }, breakLine: i < items.length - 1 } })),
    { isTextBox: true, margin: 0, color: HEX.ink, fontSize: 16, paraSpaceAfter: 8, valign: "top", ...o });
  const pic = (s, f, x, y, w, name) => { const h = w * ratio(f); s.addImage({ data: shot(f), x, y, w, h, objectName: name,
    altText: name, rounding: false }); return h; };

  // 1 title
  pres.addSection({ title: "Introduction" });
  let s = pres.addSlide({ masterName: "QC_TITLE", sectionTitle: "Introduction" });
  s.addText("Standard Operating Procedure", { placeholder: "title" });
  s.addText("Website URL quality checks with QC V6 – from sitemap to Ops ticket", { placeholder: "body" });
  s.addNotes("This deck summarises the SOP. The full SOP and the User Guide are the Word documents shared with it.");

  // 2 what QC checks
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Introduction" });
  s.addText("What QC checks", { placeholder: "title" });
  const cards = [[I.page, "Every page in the list", "Status, redirects, title, headings and other page rules"],
    [I.link, "Every link on them", "Each unique address checked once, with the pages it is on"],
    [I.image, "Every image", "Images that do not load, with the pages that use them"],
    [I.ban, "Not checked", "CSS, JavaScript, fonts, preloads, source maps"]];
  cards.forEach(([ic, h, b], i) => {
    const x = 0.6 + i * 3.1;
    card(s, x, 1.7, 2.85, 3.6, `card ${h}`);
    circle(s, x + 0.3, 2.0, 0.9, ic, i === 3 ? HEX.muted : HEX.teal, h);
    text(s, h, { x: x + 0.3, y: 3.15, w: 2.3, h: 0.9, fontSize: 20, bold: true });
    text(s, b, { x: x + 0.3, y: 4.1, w: 2.3, h: 1.0, fontSize: 15, color: HEX.muted });
  });
  s.addNotes("Sources: sitemap (indexes are followed), a sheet (CSV, Excel, text) or one URL. Code files are skipped to save requests.");

  // 3 roles
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Introduction" });
  s.addText("Who does what", { placeholder: "title" });
  [[I.user, "QC analyst", "Runs checks, reviews results, raises tickets with attachments, marks them raised, re-checks after fixes"],
    [I.tools, "Ops / web team", "Receives tickets, fixes links, redirects, images and pages, says when the fix is live"],
    [I.crown, "QC owner", "Installs and updates QC, manages accounts, backups and Clear all data, agrees speeds with the CDN team"]]
    .forEach(([ic, h, b], i) => {
      const x = 0.6 + i * 4.1;
      circle(s, x, 1.8, 1.0, ic, HEX.teal, h);
      text(s, h, { x, y: 3.0, w: 3.7, h: 0.6, fontSize: 22, bold: true });
      text(s, b, { x, y: 3.7, w: 3.7, h: 2.6, fontSize: 16, color: HEX.muted });
    });

  // 4 flow
  pres.addSection({ title: "Procedure" });
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Procedure" });
  s.addText("The procedure at a glance", { placeholder: "title" });
  const flow = ["Start QC", "Get the URLs", "Pick the speed", "Crawl & QC", "Read the result", "Raise tickets", "Re-check"];
  flow.forEach((f, i) => {
    const x = 0.6 + i * 1.75;
    s.addShape(pres.shapes.OVAL, { x: x + 0.45, y: 2.2, w: 0.8, h: 0.8, fill: { color: i === 5 ? HEX.coral : HEX.teal }, line: { color: i === 5 ? HEX.coral : HEX.teal }, objectName: `step ${i + 1}` });
    text(s, String(i + 1), { x: x + 0.45, y: 2.2, w: 0.8, h: 0.8, fontSize: 24, bold: true, color: HEX.white, align: "center", valign: "middle" });
    text(s, f, { x, y: 3.2, w: 1.7, h: 0.8, fontSize: 16, bold: true, align: "center" });
    if (i < flow.length - 1) s.addShape(pres.shapes.LINE, { x: x + 1.35, y: 2.6, w: 0.75, h: 0, line: { color: HEX.line, width: 2, endArrowType: "triangle" }, objectName: `arrow ${i + 1}` });
  });
  card(s, 0.6, 4.4, 12.1, 1.9, "summary card");
  text(s, "A check of 10,000 pages takes about 16 minutes at the Standard speed. Issues become one ticket per issue type with every URL in an Excel attachment; QC closes the ticket when a later check finds the issue fixed.",
    { x: 0.9, y: 4.65, w: 11.5, h: 1.4, fontSize: 18 });

  // 5 install
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Procedure" });
  s.addText("Install once per computer", { placeholder: "title" });
  [[I.win, "Windows", ["Python 3.11+ (tick Add to PATH)", "PostgreSQL 16 (note the postgres password)", "Double-click start.bat, type the password", "Keep the server and worker windows open"]],
    [I.apple, "macOS", ["brew install python@3.12 postgresql@16", "brew services start postgresql@16", "Right-click start.command → Open (first time)", "Ctrl+C stops QC and the worker"]]]
    .forEach(([ic, h, items], i) => {
      const x = 0.6 + i * 6.15;
      card(s, x, 1.6, 5.95, 4.9, `${h} card`);
      circle(s, x + 0.35, 1.9, 0.9, ic, HEX.teal, h);
      text(s, h, { x: x + 1.45, y: 2.05, w: 4.0, h: 0.6, fontSize: 24, bold: true });
      numbered(s, items, { x: x + 0.4, y: 3.1, w: 5.2, h: 3.2 });
    });
  s.addNotes("Both run the same application. QC opens at http://127.0.0.1:8003. Node is not needed; Chromium is downloaded on first start.");

  // 6 run a check
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Procedure" });
  s.addText("Run a check", { placeholder: "title" });
  numbered(s, ["New check → Sitemap, One page or Upload a file", "Get the URLs; download them to verify", "Pick the speed (time shown per speed)", "Start crawling", "Pause, Resume or Cancel any time"],
    { x: 0.6, y: 1.7, w: 4.6, h: 4.6, fontSize: 18 });
  pic(s, "02-ready-to-start.png", 5.6, 1.5, 7.1, "Screenshot: ready to start");

  // 7 speed
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Procedure" });
  s.addText("Speed rules", { placeholder: "title" });
  [["16 min", "10,000 pages at Standard (12 requests/s), measured"], ["5 – 6 h", "3 lakh pages at Large site (16 – 18/s)"]].forEach(([n, l], i) => {
    text(s, n, { x: 0.6, y: 1.6 + i * 2.2, w: 4.6, h: 1.1, fontSize: 60, bold: true, color: HEX.teal });
    text(s, l, { x: 0.6, y: 2.7 + i * 2.2, w: 4.6, h: 0.8, fontSize: 16, color: HEX.muted });
  });
  const hdr = (v) => ({ text: v, options: { bold: true, color: HEX.white, fill: { color: HEX.teal } } });
  s.addTable([[hdr("Speed"), hdr("Req/s"), hdr("Use for")], ["Careful", "8 – 10", "A CDN not yet measured"], ["Standard", "12", "Default under 50,000 pages"],
    ["Fast", "14", "A site known to take it"], ["Large site", "16 – 18", "Default from 50,000 pages"]],
  { x: 5.6, y: 1.7, w: 7.1, colW: [1.8, 1.3, 4.0], fontSize: 16, color: HEX.ink, border: { type: "solid", color: HEX.line, pt: 1 }, rowH: 0.55, objectName: "speed table" });
  text(s, "QC slows down by itself on slow answers or 429. Agree speeds above Standard with the CDN team.", { x: 5.6, y: 4.9, w: 7.1, h: 1.0, fontSize: 16, color: HEX.muted });

  // 8 result
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Procedure" });
  s.addText("Read the result", { placeholder: "title" });
  pic(s, "04-result.png", 0.6, 1.5, 7.1, "Screenshot: result");
  bullets(s, ["Pages not OK: list pages that do not answer 200", "Broken links: 4xx, 5xx, no answer", "Broken images: images that do not load", "Redirecting links: go through a 3xx",
    "Ops ticket tab: one summary ticket", "Download report → Excel workbook"], { x: 8.1, y: 1.7, w: 4.6, h: 4.8 });

  // 9 tickets
  pres.addSection({ title: "Tickets and reports" });
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Tickets and reports" });
  s.addText("One ticket per issue type", { placeholder: "title" });
  pic(s, "07-tickets.png", 0.6, 1.5, 7.1, "Screenshot: tickets");
  numbered(s, ["Open the ticket in Tickets → To raise", "Copy title, copy the whole ticket", "Download attachment (Excel), attach it", "Type the Ops number, Mark raised", "Re-check: QC closes it when fixed"],
    { x: 8.1, y: 1.7, w: 4.6, h: 3.6 });
  card(s, 8.1, 5.3, 4.6, 1.2, "example card");
  text(s, "100 redirecting links = 1 ticket + 1 sheet", { x: 8.35, y: 5.45, w: 4.1, h: 0.9, fontSize: 16, bold: true, color: HEX.teal, valign: "middle" });

  // 10 reports
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Tickets and reports" });
  s.addText("The Excel report", { placeholder: "title" });
  const sheets = [["Summary", "The check in plain numbers"], ["Ops Ticket", "Summary ticket, ready to paste"], ["Broken Links", "Every broken link with its pages"],
    ["Broken Images", "Every image that does not load"], ["Redirects", "Every redirecting link and where it goes"], ["Pages Not OK", "List pages not answering 200"],
    ["Tickets", "One row per issue ticket"], ["Status Codes / All URLs", "Every code and every address, for analysts"]];
  sheets.forEach(([h, b], i) => {
    const col = i % 2, row = Math.floor(i / 2), x = 0.6 + col * 6.15, y = 1.6 + row * 1.25;
    card(s, x, y, 5.95, 1.05, `sheet ${h}`);
    text(s, h, { x: x + 0.3, y: y + 0.15, w: 2.6, h: 0.75, fontSize: 17, bold: true, valign: "middle" });
    text(s, b, { x: x + 2.9, y: y + 0.15, w: 2.9, h: 0.75, fontSize: 15, color: HEX.muted, valign: "middle" });
  });

  // 11 data
  pres.addSection({ title: "Operations" });
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Operations" });
  s.addText("Data and the computer", { placeholder: "title" });
  [[I.trash, "Clear all data", "Owners: type DELETE. Erases sites, checks, reports, problems, tickets and files. Accounts and audit log stay. Cannot be undone.", HEX.coral],
    [I.db, "Back up first", "pg_dump -U postgres qc6 > qc6-backup.sql and copy the var folder.", HEX.teal],
    [I.sun, "Keep screen awake", "Switch in the menu. Laptops: never sleep while plugged in; a check resumes where it stopped.", HEX.teal]]
    .forEach(([ic, h, b, fill], i) => {
      const y = 1.6 + i * 1.65;
      circle(s, 0.6, y, 1.0, ic, fill, h);
      text(s, h, { x: 1.9, y: y + 0.02, w: 10.6, h: 0.5, fontSize: 20, bold: true });
      text(s, b, { x: 1.9, y: y + 0.55, w: 10.6, h: 0.9, fontSize: 16, color: HEX.muted });
    });

  // 12 troubleshooting
  s = pres.addSlide({ masterName: "QC_CONTENT", sectionTitle: "Operations" });
  s.addText("Troubleshooting", { placeholder: "title" });
  s.addTable([[hdr("Symptom"), hdr("Action")],
    ["The QC worker isn't running", "Start QC again; keep the worker window open"], ["Could not reach PostgreSQL", "Start the PostgreSQL service, check the password"],
    ["Sitemap refused (403)", "Save it from the browser and use Upload a file; send the allow rule to the CDN team"],
    ["Crawl slower than expected", "Site slow or asked QC to slow down; check speed and CDN"], ["Check stopped after sleep", "Start the worker: it carries on where it stopped"],
    ["Keep screen awake greyed", "Open QC at 127.0.0.1 or over https"]],
  { x: 0.6, y: 1.6, w: 12.1, colW: [4.2, 7.9], fontSize: 16, color: HEX.ink, border: { type: "solid", color: HEX.line, pt: 1 }, rowH: 0.62, objectName: "troubleshooting table" });

  // 13 checklist (dark)
  s = pres.addSlide({ masterName: "QC_TITLE", sectionTitle: "Operations" });
  s.addText("Every check, every time", { placeholder: "title" });
  s.addText("Start QC · verify the URL list · pick the agreed speed · read the four numbers · raise one ticket per issue with its attachment · mark raised · re-check after the fix", { placeholder: "body" });

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("ok");
})();
