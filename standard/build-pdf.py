"""Renders Structural-CAD-Standard-J1153.md to a print-ready HTML, then PDF.

Usage:  python build-pdf.py
Handles the subset of Markdown used in that document: headings, tables, bullet and
numbered lists, fenced code, horizontal rules, **bold**, `code`.
"""
import html as _html
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "Structural-CAD-Standard-J1153.md")
HTM = os.path.join(HERE, "Structural-CAD-Standard-J1153.html")
PDF = os.path.join(HERE, "Structural-CAD-Standard-J1153.pdf")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def inline(s):
    s = _html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r"<em>\1</em>", s)
    return s


def convert(md):
    out = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]

        if ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(_html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre>" + "\n".join(buf) + "</pre>")
            continue

        if ln.strip() in ("---", "***", "___"):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        # table: a | row followed by a |---| row
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(
                r"^\s*\|[\s:\-\|]+\|\s*$", lines[i + 1]):
            def cells(row):
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(ln)
            i += 2
            body = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            t = ["<table>", "<thead><tr>"]
            t += [f"<th>{inline(c)}</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in body:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        if re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue

        if re.match(r"^\s*\d+\.\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue

        if ln.strip() == "":
            i += 1
            continue

        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith((
                "#", "|", "```", "- ", "* ")) and not re.match(r"^\s*\d+\.\s", lines[i]) \
                and lines[i].strip() not in ("---", "***", "___"):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
        else:
            i += 1
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 20mm 18mm 18mm 18mm; }
* { box-sizing: border-box; }
body { font-family: 'IBM Plex Sans', Calibri, Arial, sans-serif; font-size: 10pt;
       line-height: 1.5; color: #16202B; max-width: 178mm; margin: 0 auto; }
h1 { font-size: 21pt; line-height: 1.15; margin: 0 0 4mm; color: #16202B;
     border-bottom: 2.5pt solid #1F6FB2; padding-bottom: 3mm; }
h2 { font-size: 14pt; margin: 9mm 0 3mm; color: #1F6FB2; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; color: #16202B; page-break-after: avoid; }
p { margin: 0 0 3mm; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin-bottom: 1.2mm; }
hr { border: 0; border-top: 0.6pt solid #DCD9D0; margin: 6mm 0; }
code { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 9pt;
       background: #EFEEE9; padding: 0.4mm 1.2mm; border-radius: 1mm; }
pre { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 9pt;
      background: #F4F3EF; border-left: 2pt solid #1F6FB2; padding: 3mm 4mm;
      margin: 0 0 4mm; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; margin: 0 0 4mm; font-size: 9pt;
        page-break-inside: avoid; }
th { background: #16202B; color: #F7F6F2; text-align: left; padding: 1.8mm 2.2mm;
     font-weight: 600; }
td { padding: 1.8mm 2.2mm; border-bottom: 0.4pt solid #DCD9D0; vertical-align: top; }
tbody tr:nth-child(even) { background: #F7F6F2; }
strong { font-weight: 600; }
em { color: #4C5A66; }
h2 + p em, p > em:only-child { display: block; background: #F4F3EF;
     border-left: 2pt solid #C2601F; padding: 2.5mm 3mm; margin-bottom: 3mm; }
"""

with open(SRC, "r", encoding="utf-8") as f:
    md = f.read()

doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Structural CAD Standard for Automated Revit Modelling - J-1153</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&family=JetBrains+Mono&display=swap">
<style>{CSS}</style></head><body>
{convert(md)}
</body></html>"""

with open(HTM, "w", encoding="utf-8") as f:
    f.write(doc)
print("wrote", HTM)

if os.path.exists(CHROME):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--virtual-time-budget=20000", f"--print-to-pdf={PDF}",
                    "file:///" + HTM.replace("\\", "/").replace(" ", "%20")],
                   capture_output=True)
    print("wrote", PDF if os.path.exists(PDF) else "(pdf failed)")
else:
    print("Chrome not found; open the HTML and print to PDF.", file=sys.stderr)
