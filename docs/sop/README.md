# QC V6 SOP and User Guide

| File | What it is |
|---|---|
| `QC_V6_SOP.docx` | Standard Operating Procedure: scope, roles, installation (Windows / macOS / Linux), operating procedure, reports, speed rules, raising tickets, data management, troubleshooting, updates, status codes, glossary |
| `QC_V6_SOP.pptx` | The SOP as a 13-slide deck, with speaker notes |
| `QC_V6_User_Guide.docx` | Task-by-task guide with screenshots: run a check, read the result, raise tickets, reports, settings |

They describe QC with the Tickets tab, Keep screen awake and Clear all data (PRs #19, #20, #21).
Screenshots were taken from a 2,000-page test site (`shop.example.test`, `scripts/bench_site.py`).

`generator/` has the scripts that built them (docx and pptxgenjs, Node). They read screenshots from a local folder set at
the top of each script; update the text there and run them again to refresh the documents. The table of contents in the
Word files updates when the file is opened in Word.
