import re
import os
import time
import json
import math
import html
import requests
import tldextract
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser

# ---------------------------
# Config and constants
# ---------------------------

APP_NAME = "Lead Radar: Clients & Developers (Last 30 Days)"

DEFAULT_KEYWORDS = [
    "need app developer",
    "outsourcing software project",
    "mvp build",
    "looking for software agency",
    "hire developer for project",
    "outsourcing",
    "contract developer",
    "build our app",
    "build my app",
    "web app developer",
    "mobile app developer",
    "full-stack developer",
    "looking for dev shop",
    "cto for hire",
    "fixed bid",
    "greenfield",
    "prototype",
    "poC",
]

CLIENT_HINTS = [
    "need developer",
    "looking for developer",
    "hiring contractor",
    "outsourcing",
    "mvp",
    "prototype",
    "build an app",
    "agency",
    "dev shop",
    "help us build",
    "[hiring]",   # r/forhire convention
]

CANDIDATE_HINTS = [
    "for hire",
    "open to work",
    "available for freelance",
    "available for contract",
    "[for hire]",  # r/forhire convention
]

TRIGGER_KEYWORDS = {
    "funding": ["seed", "pre-seed", "series a", "series b", "venture funding", "raised", "funding", "round"],
    "launch": ["launched", "launching", "product hunt", "beta", "v1", "public launch", "go live"],
    "hiring_freeze": ["hiring freeze", "budget freeze", "cost cutting", "contractors only"],
    "scale_up": ["scale", "scaling", "increasing demand", "rapid growth", "high growth"],
    "deadline": ["deadline", "urgent", "immediately", "asap", "deliver by"],
}

MAX_DAYS = 30
NOW = datetime.now(timezone.utc)
EARLIEST_TS = NOW - timedelta(days=MAX_DAYS)

REQUEST_HEADERS = {
    "User-Agent": "LeadRadar/1.0 (+https://example.com)"
}

# Optional API keys (leave empty to skip)
CLEARBIT_API_KEY = os.getenv("CLEARBIT_API_KEY", "")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# ---------------------------
# Utilities
# ---------------------------

def safe_get(url, params=None, headers=None, timeout=20):
    try:
        r = requests.get(url, params=params, headers=headers or REQUEST_HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        return None

def extract_urls(text):
    if not text:
        return []
    regex = r"(https?://[^\s)]+)"
    return re.findall(regex, text)

def extract_domain(url):
    try:
        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
    except Exception:
        pass
    return None

def html_to_text(s):
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)

def within_30_days(dt):
    if not dt:
        return False
    return dt >= EARLIEST_TS

def parse_unix_ts(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None

def parse_rfc3339(s):
    try:
        return dateparser.parse(s).astimezone(timezone.utc)
    except Exception:
        return None

def score_recency(dt):
    # 0..1: newer is better
    if not dt:
        return 0.0
    days = (NOW - dt).days + (NOW - dt).seconds / 86400
    return max(0.0, min(1.0, 1.0 - (days / MAX_DAYS)))

def score_trigger(text):
    if not text:
        return 0.0
    t = text.lower()
    score = 0.0
    for bucket, kws in TRIGGER_KEYWORDS.items():
        hits = sum(1 for k in kws if k in t)
        if hits:
            # Weighted per bucket
            weight = {
                "funding": 1.0,
                "launch": 0.8,
                "hiring_freeze": 0.7,
                "scale_up": 0.6,
                "deadline": 0.5
            }.get(bucket, 0.4)
            score += weight * min(hits, 3)
    # Normalize to ~0..1
    return min(1.0, score / 3.0)

def score_engagement(points=None, comments=None):
    p = (points or 0)
    c = (comments or 0)
    # Log-scale
    v = math.log1p(p + 0.5 * c)
    return min(1.0, v / 5.0)

def classify_post(title, text, subreddit_hint=None):
    s = " ".join([title or "", text or ""]).lower()

    # Subreddit strong signals:
    if subreddit_hint:
        sh = subreddit_hint.lower()
        # r/forhire uses [Hiring] (client) and [For Hire] (candidate)
        if sh == "forhire":
            if "[for hire]" in s:
                return "Developer Candidate"
            if "[hiring]" in s:
                return "Potential Client"

    # Keyword rules
    cand_hits = sum(1 for w in CANDIDATE_HINTS if w in s)
    client_hits = sum(1 for w in CLIENT_HINTS if w in s)

    if cand_hits > client_hits:
        return "Developer Candidate"
    if client_hits > 0:
        return "Potential Client"

    # Fallback heuristic
    if "hire" in s and "developer" in s:
        return "Potential Client"
    if any(w in s for w in ["available", "for hire"]) and "developer" in s:
        return "Developer Candidate"

    return None

def extract_company_info_from_text(text):
    """
    Heuristic: find first likely URL domain and derive a name-like string.
    """
    urls = extract_urls(text)
    domain = None
    website = None
    for u in urls:
        d = extract_domain(u)
        if d and not any(x in d for x in ["reddit.com", "news.ycombinator.com", "github.com", "medium.com"]):
            domain = d
            website = f"https://{d}"
            break
    company_name = None
    if domain:
        base = domain.split(".")[0]
        company_name = base.title()

    return {
        "company_name": company_name,
        "website": website,
        "domain": domain
    }

def guess_industry_from_text(text):
    t = (text or "").lower()
    patterns = {
        "Fintech": ["fintech", "payments", "ledger", "banking", "trading", "crypto", "defi"],
        "Healthtech": ["health", "med", "clinic", "ehr", "wellness", "fitness"],
        "E-commerce": ["shopify", "ecommerce", "storefront", "marketplace", "checkout"],
        "SaaS": ["saas", "subscription", "b2b", "multi-tenant"],
        "Edtech": ["education", "learning", "edtech", "course", "lms"],
        "AI/ML": ["ai", "ml", "model", "llm", "computer vision", "nlp"],
        "Logistics": ["logistics", "fleet", "delivery", "supply chain"],
        "Real Estate": ["real estate", "property", "proptech"],
        "Travel": ["travel", "booking", "itinerary"],
        "Social": ["social", "community", "messaging"],
    }
    for k, kws in patterns.items():
        if any(w in t for w in kws):
            return k
    return None

def detect_trigger_event(text):
    t = (text or "").lower()
    hits = []
    for label, kws in TRIGGER_KEYWORDS.items():
        if any(k in t for k in kws):
            hits.append(label.replace("_", " "))
    if hits:
        # Return first strongest
        order = ["funding", "launch", "hiring freeze", "scale up", "deadline"]
        hits_sorted = sorted(hits, key=lambda x: order.index(x) if x in order else 99)
        return hits_sorted[0]
    return None

def find_emails(text):
    if not text:
        return []
    # Simple email pattern
    pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    emails = set(re.findall(pattern, text))
    return list(emails)[:3]

def shorten(s, n=220):
    if not s:
        return ""
    s = s.strip()
    return s if len(s) <= n else s[:n-1].rstrip() + "…"

# ---------------------------
# Data sources fetchers
# ---------------------------

def fetch_hn_algolia(keywords, days=30, max_hits=150):
    """
    Hacker News via Algolia API (public).
    """
    url = "https://hn.algolia.com/api/v1/search_by_date"
    query = " OR ".join(f"\"{k}\"" for k in keywords)
    params = {
        "query": query,
        "tags": "story",
        "numericFilters": f"created_at_i>{int(EARLIEST_TS.timestamp())}",
        "hitsPerPage": 100,
        "page": 0
    }
    results = []
    for page in range(0, 3):
        params["page"] = page
        r = safe_get(url, params=params)
        if not r:
            break
        data = r.json()
        hits = data.get("hits", [])
        for h in hits:
            title = h.get("title") or ""
            url_ = h.get("url")
            author = h.get("author")
            created_at = parse_rfc3339(h.get("created_at"))
            text = ""  # Stories often lack body; rely on title + URL
            points = h.get("points") or 0
            comments = h.get("num_comments") or 0
            if not within_30_days(created_at):
                continue
            results.append({
                "source": "Hacker News",
                "title": title,
                "text": text,
                "url": url_ or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "author": author,
                "created_at": created_at,
                "points": points,
                "comments": comments,
                "subreddit": None
            })
            if len(results) >= max_hits:
                break
        if len(results) >= max_hits or len(hits) == 0:
            break
        time.sleep(0.3)
    return results

def fetch_reddit_forhire(limit=120):
    """
    Reddit r/forhire new posts. Public JSON endpoint, no auth required.
    """
    url = f"https://www.reddit.com/r/forhire/new.json"
    r = safe_get(url, params={"limit": str(limit)})
    if not r:
        return []
    data = r.json()
    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = html.unescape(d.get("title", ""))
        selftext = html.unescape(d.get("selftext", "")) or ""
        created = parse_unix_ts(d.get("created_utc"))
        if not within_30_days(created):
            continue
        items.append({
            "source": "Reddit",
            "title": title,
            "text": selftext,
            "url": f"https://www.reddit.com{d.get('permalink', '')}",
            "author": d.get("author"),
            "created_at": created,
            "points": d.get("score"),
            "comments": d.get("num_comments"),
            "subreddit": "forhire"
        })
    return items

def fetch_reddit_subreddit(subreddit, keywords, limit=120):
    """
    Generic subreddit scan with keyword filter.
    """
    url = f"https://www.reddit.com/r/{subreddit}/new.json"
    r = safe_get(url, params={"limit": str(limit)})
    if not r:
        return []
    data = r.json()
    items = []
    kws = [k.lower() for k in keywords]
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = html.unescape(d.get("title", ""))
        selftext = html.unescape(d.get("selftext", "")) or ""
        created = parse_unix_ts(d.get("created_utc"))
        if not within_30_days(created):
            continue
        blob = f"{title}\n{selftext}".lower()
        if any(k in blob for k in kws):
            items.append({
                "source": "Reddit",
                "title": title,
                "text": selftext,
                "url": f"https://www.reddit.com{d.get('permalink', '')}",
                "author": d.get("author"),
                "created_at": created,
                "points": d.get("score"),
                "comments": d.get("num_comments"),
                "subreddit": subreddit
            })
    return items

# ---------------------------
# Enrichment
# ---------------------------

def simple_company_enrichment(website):
    if not website:
        return {}
    resp = safe_get(website, timeout=10)
    info = {}
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else None
        desc = None
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            desc = md["content"].strip()
        info["site_title"] = title
        info["site_desc"] = desc
    return info

def optional_clearbit_company(domain):
    if not CLEARBIT_API_KEY or not domain:
        return {}
    try:
        r = requests.get(
            f"https://company.clearbit.com/v2/companies/find",
            params={"domain": domain},
            auth=(CLEARBIT_API_KEY, ""),
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "company_name": data.get("name"),
                "website": data.get("domain") and f"https://{data.get('domain')}",
                "industry": data.get("category", {}).get("industry"),
                "size": data.get("metrics", {}).get("employeesRange"),
                "location": data.get("location"),
                "linkedin": data.get("site", {}).get("linkedin"),
            }
    except Exception:
        pass
    return {}

def optional_hunter_email(domain):
    if not HUNTER_API_KEY or not domain:
        return []
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            emails = data.get("data", {}).get("emails", [])
            out = []
            for e in emails[:3]:
                out.append({
                    "value": e.get("value"),
                    "first_name": e.get("first_name"),
                    "last_name": e.get("last_name"),
                    "position": e.get("position"),
                    "linkedin": e.get("linkedin")
                })
            return out
    except Exception:
        pass
    return []

# ---------------------------
# Pipeline
# ---------------------------

def build_leads(keywords, extra_subreddits, max_items=100):
    # 1) Fetch
    hn_items = fetch_hn_algolia(keywords, max_hits=max_items)
    forhire_items = fetch_reddit_forhire(limit=120)
    other_reddits = []
    for sub in extra_subreddits:
        other_reddits.extend(fetch_reddit_subreddit(sub, keywords, limit=120))

    items = hn_items + forhire_items + other_reddits

    # 2) Classify and transform
    potential_clients = []
    developer_candidates = []

    for it in items:
        title = it["title"] or ""
        text = it["text"] or ""
        sub = it.get("subreddit")
        classification = classify_post(title, text, subreddit_hint=sub)
        if not classification:
            continue

        created_at = it["created_at"]
        if not within_30_days(created_at):
            continue

        trigger = detect_trigger_event(f"{title}\n{text}")
        urls = extract_urls(f"{title}\n{text}\n{it.get('url','')}")
        domain_guess = extract_company_info_from_text(f"{title}\n{text}\n{it.get('url','')}")
        industry_guess = guess_industry_from_text(f"{title}\n{text}")
        emails_inline = find_emails(text)

        # Ranking score
        score = (
            0.45 * score_trigger(f"{title}\n{text}") +
            0.35 * score_recency(created_at) +
            0.20 * score_engagement(it.get("points"), it.get("comments"))
        )

        doc = {
            "title": title,
            "text": text,
            "source": it["source"],
            "url": it["url"],
            "author": it.get("author"),
            "created_at": created_at,
            "points": it.get("points"),
            "comments": it.get("comments"),
            "subreddit": sub,
            "urls": urls,
            "domain_guess": domain_guess,
            "industry_guess": industry_guess,
            "emails_inline": emails_inline,
            "trigger": trigger,
            "score": round(score, 4),
        }

        if classification == "Potential Client":
            potential_clients.append(doc)
        elif classification == "Developer Candidate":
            developer_candidates.append(doc)

    # 3) Enrich clients (lightweight + optional)
    for doc in potential_clients:
        website = doc["domain_guess"].get("website")
        domain = doc["domain_guess"].get("domain")

        # Lightweight site scrape
        site_info = simple_company_enrichment(website) if website else {}
        doc["site_title"] = site_info.get("site_title")
        doc["site_desc"] = site_info.get("site_desc")

        # Optional Clearbit enrichment
        cb = optional_clearbit_company(domain)
        # Keep guessed if enrichment missing fields
        doc["company_name"] = cb.get("company_name") or doc["domain_guess"].get("company_name")
        doc["company_website"] = cb.get("website") or website
        doc["industry"] = cb.get("industry") or doc.get("industry_guess")
        doc["size"] = cb.get("size")
        doc["location"] = cb.get("location")
        doc["linkedin_company"] = cb.get("linkedin")

        # Decision-makers (very light heuristic from text + optional Hunter)
        decision_emails = optional_hunter_email(domain)
        contacts = []
        if decision_emails:
            for e in decision_emails:
                contacts.append({
                    "name": " ".join([e.get("first_name") or "", e.get("last_name") or ""]).strip() or None,
                    "role": e.get("position"),
                    "email": e.get("value"),
                    "linkedin": e.get("linkedin")
                })
        # Also include inline emails if present
        for e in doc.get("emails_inline", []):
            contacts.append({"name": None, "role": None, "email": e, "linkedin": None})

        doc["decision_makers"] = contacts[:3]

    # 4) Minimal enrichment for candidates
    for doc in developer_candidates:
        # Extract portfolios if present
        portfolios = [u for u in doc.get("urls", []) if any(x in u for x in ["github.com", "gitlab.com", "behance.net", "dribbble.com", "codepen.io", "linkedin.com", "personal."])]
        doc["portfolios"] = portfolios[:5]
        # Skills heuristic
        skills = []
        t = (doc["title"] + " " + doc["text"]).lower()
        skill_lib = ["python", "django", "flask", "fastapi", "javascript", "typescript", "react", "node", "next.js", "vue", "angular",
                     "java", "spring", "kotlin", "swift", "ios", "android", "react native", "flutter",
                     "aws", "gcp", "azure", "devops", "docker", "kubernetes",
                     "postgres", "mysql", "mongodb", "redis", "graphql",
                     "ai", "ml", "llm", "nlp", "computer vision"]
        for s in skill_lib:
            if s in t:
                skills.append(s)
        doc["skills_extracted"] = sorted(list(set(skills)))[:15]

        # Availability heuristic
        avail = "Immediate" if any(k in t for k in ["immediate", "asap", "available now"]) else "Notice Period"
        doc["availability"] = avail

        # Years of experience heuristic
        m = re.search(r"(\d{1,2})\+?\s*(?:years|yrs|y)", t)
        yoe = int(m.group(1)) if m else None
        doc["yoe"] = yoe

        # Location heuristic
        m2 = re.search(r"(remote|india|usa|europe|uk|canada|australia|singapore|germany|netherlands)", t)
        doc["location_guess"] = m2.group(1).title() if m2 else None

    # 5) Rank
    potential_clients = sorted(potential_clients, key=lambda x: x["score"], reverse=True)
    developer_candidates = sorted(developer_candidates, key=lambda x: x["score"], reverse=True)

    return potential_clients, developer_candidates

# ---------------------------
# Rendering
# ---------------------------

def render_output(clients, candidates, top_n_clients=20, top_n_candidates=20):
    out = []

    out.append("## Potential Clients")
    if not clients:
        out.append("- No results found in the last 30 days.")
    else:
        for doc in clients[:top_n_clients]:
            cname = doc.get("company_name") or doc["domain_guess"].get("company_name") or "(Company TBD)"
            website = doc.get("company_website") or doc["domain_guess"].get("website") or doc["url"]
            industry = doc.get("industry") or "Unknown"
            size = doc.get("size") or "Unknown"
            location = doc.get("location") or "Unknown"

            # Opportunity summary
            snippet = shorten(doc.get("text") or doc.get("site_desc") or doc.get("title"), 280)
            opp = f"{snippet} (Source: {doc['source']})"

            # Contact
            if doc.get("decision_makers"):
                c = doc["decision_makers"][0]
                contact_line = f"{(c.get('name') or 'Contact TBD')}, {(c.get('role') or 'Decision-maker')}, {c.get('email') or c.get('linkedin') or website}"
            else:
                # Fallback to author or website
                contact_line = f"{(doc.get('author') or 'Contact TBD')}, Point of contact, {website}"

            # Trigger
            trig = doc.get("trigger") or "Not specified"

            # Markdown block
            out.append(f"- **{cname}:** {website} | {industry} | {size} | {location}")
            out.append(f"  - **{cname} – Opportunity Summary:** {opp}")
            out.append(f"  - **Contact:** {contact_line}")
            out.append(f"  - **Trigger Event:** {trig}")
            out.append(f"  - **Post:** {doc['title']} | {doc['url']}")
            out.append("")

    out.append("## Developer Candidates")
    if not candidates:
        out.append("- No results found in the last 30 days.")
    else:
        for doc in candidates[:top_n_candidates]:
            name = (doc.get("author") or "Developer") + " (Reddit/HN)"
            skills = ", ".join(doc.get("skills_extracted", [])[:10]) or "Skills not specified"
            portfolios = doc.get("portfolios") or [doc.get("url")]
            portfolios_str = " | ".join(portfolios[:3])
            availability = doc.get("availability") or "Notice Period"
            yoe = f"{doc.get('yoe')} years" if doc.get("yoe") else "N/A"
            loc = doc.get("location_guess") or "Remote/Unspecified"

            # Summary line
            out.append(f"- **{name} – Skills Summary:** {skills}")
            out.append(f"  - **Portfolio:** {portfolios_str}")
            out.append(f"  - **Availability:** {availability} | **Experience:** {yoe} | **Location:** {loc}")
            out.append(f"  - **Post:** {doc['title']} | {doc['url']}")
            out.append("")

    return "\n".join(out)

# ---------------------------
# Streamlit UI
# ---------------------------

def main():
    st.set_page_config(page_title=APP_NAME, page_icon="🔎", layout="wide")
    st.title(APP_NAME)
    st.caption("Find outsourcing-ready clients and available developers from the last 30 days. Ranked by potential value.")

    with st.sidebar:
        st.header("Input")
        kw = st.text_area(
            "Keywords",
            value=", ".join(DEFAULT_KEYWORDS),
            help="Comma-separated phrases to search for."
        )
        extra_subs = st.text_input(
            "Extra subreddits",
            value="startups, entrepreneur, freelance, forhire",
            help="Comma-separated subreddit names (we always scan r/forhire)."
        )
        max_items = st.slider("Max HN items", min_value=50, max_value=300, value=150, step=50)
        top_clients = st.slider("Top clients to show", min_value=5, max_value=50, value=20, step=5)
        top_cands = st.slider("Top candidates to show", min_value=5, max_value=50, value=20, step=5)
        st.markdown("---")
        st.subheader("Optional enrichment keys")
        st.text_input("CLEARBIT_API_KEY", value=os.getenv("CLEARBIT_API_KEY", ""), type="password", key="cb_key")
        st.text_input("HUNTER_API_KEY", value=os.getenv("HUNTER_API_KEY", ""), type="password", key="hunter_key")
        st.caption("If provided, company and contact enrichment improves. Leave blank to skip.")

        run = st.button("Run discovery")

    if run:
        keywords = [x.strip() for x in kw.split(",") if x.strip()]
        extra_subreddits = [x.strip() for x in extra_subs.split(",") if x.strip()]
        # Set env keys for this run
        global CLEARBIT_API_KEY, HUNTER_API_KEY
        CLEARBIT_API_KEY = st.session_state.get("cb_key") or ""
        HUNTER_API_KEY = st.session_state.get("hunter_key") or ""

        with st.spinner("Searching sources and building ranked lead lists..."):
            clients, candidates = build_leads(keywords, extra_subreddits, max_items=max_items)
            md = render_output(clients, candidates, top_n_clients=top_clients, top_n_candidates=top_cands)

        st.success(f"Found {len(clients)} potential clients and {len(candidates)} developer candidates (last 30 days).")
        st.download_button("Download markdown", data=md.encode("utf-8"), file_name="lead_radar.md", mime="text/markdown")
        st.markdown(md)

        # Also show raw tables for quick scanning
        with st.expander("Raw client data"):
            st.dataframe([
                {
                    "Company": c.get("company_name") or c["domain_guess"].get("company_name"),
                    "Website": c.get("company_website") or c["domain_guess"].get("website"),
                    "Industry": c.get("industry"),
                    "Trigger": c.get("trigger"),
                    "Score": c.get("score"),
                    "Source": c.get("source"),
                    "Post": c.get("title"),
                    "URL": c.get("url"),
                    "Created": c.get("created_at"),
                } for c in clients
            ], use_container_width=True)

        with st.expander("Raw candidate data"):
            st.dataframe([
                {
                    "Author": d.get("author"),
                    "Skills": ", ".join(d.get("skills_extracted", [])),
                    "Availability": d.get("availability"),
                    "YoE": d.get("yoe"),
                    "Location": d.get("location_guess"),
                    "Score": d.get("score"),
                    "Source": d.get("source"),
                    "Post": d.get("title"),
                    "URL": d.get("url"),
                    "Created": d.get("created_at"),
                } for d in candidates
            ], use_container_width=True)

    else:
        st.info("Set your keywords and subreddits, then click Run discovery. Results are limited to the past 30 days.")

if __name__ == "__main__":
    main()
