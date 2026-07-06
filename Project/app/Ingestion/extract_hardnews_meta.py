"""
extract_hardnews_meta.py — Hardnews .docx metadata extractor

Handles two document formats found in the Hardnews archive:

  Format 1 (clean CMS export):
    Para[0]  title
    Para[1]  "Published: Mon, MM/DD/YYYY - HH:MM ..."
    Para[2]  standfirst
    Para[3]  "Author Name  City" or "Author Name, Hardnews City"

  Format 2 (scraped with nav header):
    Para[0..8]  navigation junk (Skip to main content / India / Business ...)
    Para[9]     title
    Para[10]    "Published: ..."
    Para[11]    standfirst
    Para[12]    "Author Name & Co-author  City (State)" or "Author  City"

Usage:
    python3 extract_hardnews_meta.py \
        --source-dir "sample_docs/hardnews/HARD NEWS ARTICLES" \
        --output hardnews_metadata.csv
"""
import argparse, csv, re, sys
from pathlib import Path
from datetime import date as DateObj

try:
    from docx import Document
except ImportError:
    print("pip install python-docx --break-system-packages"); sys.exit(1)

# ── Date patterns ────────────────────────────────────────────────────────────
DATE_PATTERNS = [
    re.compile(r'Published:\s+\w+,\s+(\d{1,2}/\d{1,2}/\d{4})', re.I),
    re.compile(r'Submitted by .+ on \w+,\s+(\d{1,2}/\d{1,2}/\d{4})', re.I),
    re.compile(r'(\d{1,2}/\d{1,2}/\d{4})'),
]

# ── Location patterns ────────────────────────────────────────────────────────
INDIAN_CITIES = [
    "New Delhi","Delhi","Mumbai","Kolkata","Chennai","Bengaluru","Bangalore",
    "Hyderabad","Lucknow","Patna","Bhopal","Jaipur","Chandigarh","Dehradun",
    "Bhubaneswar","Raipur","Ranchi","Shimla","Srinagar","Guwahati","Pune",
    "Ahmedabad","Kochi","Thiruvananthapuram","Nagpur","Indore","Varanasi",
    "Allahabad","Prayagraj","Agra","Meerut","Amritsar","Ludhiana","Coimbatore",
    "Visakhapatnam","Vijayawada","Jammu","Leh","Imphal","Shillong","Aizawl",
    "Itanagar","Kohima","Gangtok","Panaji","Goa","Noida","Gurugram","Gurgaon",
    "Faridabad","Muzaffarpur","Gorakhpur","Jodhpur","Udaipur","Bikaner","Hooghly",
    "Howrah","Siliguri","Darjeeling","Haridwar","Rishikesh","Mussoorie","Nainital",
    "Mangaluru","Hubli","Mysuru","Mysore","Belgaum","Belagavi","Tiruchirappalli",
    "Madurai","Salem","Tirunelveli","Kannur","Kozhikode","Thrissur","Kollam",
    "Bhopal","Gwalior","Jabalpur","Surat","Vadodara","Rajkot","Bhavnagar",
]

CITY_PAT = re.compile(
    r'(?:^|\s)(' + '|'.join(re.escape(c) for c in sorted(INDIAN_CITIES, key=len, reverse=True)) + r')'
    r'(?:\s*[\(/][\w\s]+[\)/])?',
    re.I
)

# Navigation items to strip in Format 2
NAV_ITEMS = {
    "skip to main content","search","india","business","crime","elections",
    "sports","world","foreign policy","economy","politics","culture","opinion",
    "environment","health","education","science","technology","entertainment",
    "international","national","features","columnists","interviews",
}

NOISE_AUTHORS = {
    "hardnews","staff","reporter","desk","bureau","correspondent","agency",
    "ani","pti","ians","hn","special","foreign policy","search","india",
    "business","crime","elections","sports","world","economy",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(text: str) -> str:
    for p in DATE_PATTERNS:
        m = p.search(text)
        if m:
            parts = m.group(1).split("/")
            if len(parts) == 3:
                try:
                    d = DateObj(int(parts[2]), int(parts[0]), int(parts[1]))
                    return d.strftime("%Y-%m-%d")
                except ValueError:
                    return m.group(1)
    return ""


def parse_location(text: str) -> str:
    """Find first Indian city mention in a line."""
    m = CITY_PAT.search(text)
    return m.group(1).strip().title() if m else ""


def parse_author(text: str) -> str:
    """
    Extract author from lines like:
      "Partha Mukherjee & Priyanka Mukherjee  Hooghly (West Bengal)"
      "Sadiq Naqvi Delhi"
      "Sanjay Kapoor, Hardnews Delhi"
      "Shibu Kumar Tripathi"
      "By Arundhati Roy"
    Returns cleaned author string or "".
    """
    t = text.strip()

    # Special case: "Hardnews Bureau [City]" is a valid attribution
    if re.search(r'Hardnews\s+Bureau', t, re.I):
        return "Hardnews Bureau"

    # Strip "By " prefix
    t = re.sub(r'^[Bb][Yy][:\s]+', '', t).strip()

    # Strip city + anything after (parenthetical state, slashes)
    city_m = CITY_PAT.search(t)
    if city_m:
        t = t[:city_m.start()].strip().rstrip(",/ ")

    # Strip trailing "Hardnews" or ", Hardnews"
    t = re.sub(r'[,\s]+[Hh]ardnews$', '', t).strip()

    # Normalise separators between co-authors: " & ", " and "
    t = re.sub(r'\s+[&]\s+', ' & ', t)
    t = re.sub(r'\s{2,}', ' ', t)

    # Validate: should look like a name (letters, spaces, & . -)
    if not t:
        return ""
    if any(kw in t.lower() for kw in NOISE_AUTHORS):
        return ""
    if re.search(r'\d', t):
        return ""
    if len(t) > 60 or len(t) < 4:
        return ""

    return t


def is_author_candidate(text: str) -> bool:
    if not text or len(text) > 80:
        return False
    low = text.lower()
    if any(kw in low for kw in [
        "http","©","copyright","subscribe","about","published:","submitted",
        "updated:","topic:","hardnews media","all rights","terms of service",
        "print edition","contact hardnews","read more","#tags","tags:",
    ]):
        return False
    if low in NAV_ITEMS:
        return False
    alpha_ratio = sum(c.isalpha() or c in " ,/-.&()" for c in text) / max(len(text), 1)
    return alpha_ratio > 0.72


def strip_nav_header(paras: list) -> list:
    """Remove navigation items from the start of Format 2 documents."""
    i = 0
    while i < len(paras) and (paras[i].lower() in NAV_ITEMS or len(paras[i]) < 3):
        i += 1
    return paras[i:]


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_meta(path: Path) -> dict:
    r = {
        "filename": path.name, "filepath": "", "publication": "Hardnews",
        "author": "", "date_published": "", "location": "", "title": "", "notes": "",
    }
    try:
        doc = Document(str(path))
    except Exception as e:
        r["notes"] = f"unreadable: {e}"; return r

    all_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not all_paras:
        r["notes"] = "empty"; return r

    # Detect Format 2 (nav header) and strip it
    if all_paras[0].lower() == "skip to main content":
        paras = strip_nav_header(all_paras)
    else:
        paras = all_paras

    if not paras:
        r["notes"] = "empty after strip"; return r

    # Title: first remaining paragraph
    r["title"] = paras[0][:200]

    # Date: check paras 1-4 for a date line
    for p in paras[1:5]:
        if "published" in p.lower() or "submitted" in p.lower():
            d = parse_date(p)
            if d:
                r["date_published"] = d
                break

    # Author + location: scan paras 2-9
    for p in paras[2:10]:
        if not is_author_candidate(p):
            continue
        loc = parse_location(p)
        author = parse_author(p)
        if author:
            r["author"] = author
            r["location"] = loc
            break

    missing = []
    if not r["author"]: missing.append("no author")
    if not r["date_published"]: missing.append("no date")
    if missing: r["notes"] = "; ".join(missing)
    return r


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", default="sample_docs/hardnews/HARD NEWS ARTICLES")
    ap.add_argument("--output", default="hardnews_metadata.csv")
    ap.add_argument("--recursive", action="store_true", help="Scan .docx files recursively")
    ap.add_argument("--limit", type=int, default=0, help="Maximum files to process; 0 means all")
    args = ap.parse_args()

    source_dir = Path(args.source_dir)
    files = sorted(source_dir.rglob("*.docx") if args.recursive else source_dir.glob("*.docx"))
    if args.limit > 0:
        files = files[: args.limit]
    print(f"Processing {len(files)} articles...")

    rows, needs_review = [], 0
    for path in files:
        meta = extract_meta(path)
        meta["filepath"] = str(path.relative_to(source_dir))
        rows.append(meta)
        if meta["notes"]: needs_review += 1

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename","filepath","publication","author","date_published","location","title","notes"])
        w.writeheader(); w.writerows(rows)

    clean = len(rows) - needs_review
    print(f"\n{clean}/{len(rows)} extracted cleanly  |  {needs_review} need manual review")
    print(f"CSV: {args.output}\n")

    # Breakdown
    import csv as csv_mod
    with open(args.output) as f:
        data = list(csv_mod.DictReader(f))
    no_a = sum(1 for r in data if "no author" in r["notes"])
    no_d = sum(1 for r in data if "no date" in r["notes"])
    both = sum(1 for r in data if "no author" in r["notes"] and "no date" in r["notes"])
    print(f"  Missing author only : {no_a - both}")
    print(f"  Missing date only   : {no_d - both}")
    print(f"  Missing both        : {both}")

    print("\nSample (first 12):")
    for r in rows[:12]:
        flag = "⚠ " if r["notes"] else "✓ "
        print(f"  {flag}{r['filename'][:40]:<40}  {r['author']:<24}  {r['date_published']:<12}  {r['location']}")

if __name__ == "__main__":
    main()
