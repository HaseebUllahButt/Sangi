import subprocess, tempfile, os, zipfile, datetime

now = datetime.datetime.now().strftime("%d %B %Y")

pdf_path = os.path.join("workspace", "usd_pkr_rate.pdf")
docx_path = os.path.join("workspace", "usd_pkr_rate.docx")

rates = [
    ("Interbank / Mid-market", "278.40"),
    ("Open market (PK)", "278.60"),
    ("52-week range", "277.70 - 285.20"),
]

conversions = [
    ("$100", "Rs. 27,860"),
    ("$500", "Rs. 139,300"),
    ("$1,000", "Rs. 278,600"),
]

# ---------------- PDF via PostScript -> ps2pdf ----------------
lines = [
    "%!PS-Adobe-3.0",
    "/Helvetica-Bold findfont 18 scalefont setfont",
    "72 780 moveto (USD to PKR - Exchange Rate) show",
    "/Helvetica findfont 11 scalefont setfont",
    "72 762 moveto (%s - indicative rates) show" % now,
    "/Helvetica-Bold findfont 12 scalefont setfont",
    "72 730 moveto (Exchange Rates) show",
    "/Helvetica findfont 11 scalefont setfont",
]
y = 712
for label, value in rates:
    lines.append("72 %d moveto (%s: %s) show" % (y, label, value))
    y -= 18

lines.append("/Helvetica-Bold findfont 12 scalefont setfont")
lines.append("72 %d moveto (Conversions) show" % (y - 4))
lines.append("/Helvetica findfont 11 scalefont setfont")
y -= 22
for amount, value in conversions:
    lines.append("72 %d moveto (%s = %s) show" % (y, amount, value))
    y -= 18

lines.append("/Helvetica-Oblique findfont 9 scalefont setfont")
lines.append("72 60 moveto (Rates move by the hour; indicative only.) show")
lines.append("showpage")

tmp_ps = tempfile.NamedTemporaryFile(suffix=".ps", delete=False).name
with open(tmp_ps, "w") as f:
    f.write("\n".join(lines) + "\n")
subprocess.run(["ps2pdf", tmp_ps, pdf_path], check=True)
os.unlink(tmp_ps)

# ---------------- DOCX (zip of XML parts) ----------------
def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

body = []
body.append("<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val=\"36\"/></w:rPr><w:t>USD to PKR - Exchange Rate</w:t></w:r></w:p>")
body.append("<w:p><w:r><w:t>%s - indicative rates</w:t></w:r></w:p>" % now)

body.append("<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Exchange Rates</w:t></w:r></w:p>")
for label, value in rates:
    body.append("<w:p><w:r><w:t>%s: %s</w:t></w:r></w:p>" % (label, value))

body.append("<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Conversions</w:t></w:r></w:p>")
for amount, value in conversions:
    body.append("<w:p><w:r><w:t>%s = %s</w:t></w:r></w:p>" % (amount, value))

body.append("<w:p><w:r><w:rPr><w:i/></w:rPr><w:t>Rates move by the hour; indicative only.</w:t></w:r></w:p>")

xml = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>%s</w:body></w:document>' % "".join(body)
)

parts = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    ),
    "word/document.xml": xml,
}

with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in parts.items():
        z.writestr(name, data)

print("PDF:", pdf_path, os.path.getsize(pdf_path))
print("DOCX:", docx_path, os.path.getsize(docx_path))
