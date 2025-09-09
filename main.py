# app.py
import os
import re
import time
import math
import html
import json
import queue
import random
import string
import hashlib
import feedparser
import phonenumbers
import threading
import tldextract
import pandas as pd
import streamlit as st
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as dateparser

APP_NAME = "Lead Radar Pro (Free Sources) — Outsourcing Clients & Developers"

# ---------------------------
# Config: sources & constants
# ---------------------------

MAX_DAYS = 30
NOW = datetime.now(timezone.utc)
EARLIEST_TS = NOW - timedelta(days=MAX_DAYS)

DEFAULT_CLIENT_KEYWORDS = [
    "need app developer",
    "outsourcing software project",
    "mvp build",
    "looking for software agency",
    "hire developer for project",
    "build our app",
    "build my app",
    "contract developer",
    "prototype",
    "poc",
    "fixed bid",
    "seeking agency",
]

DEFAULT_CANDIDATE_KEYWORDS = [
    "for hire",
    "open to work",
    "available for freelance",
    "available for contract",
    "seeking projects",
    "consultant available",
]

REDDIT_SUBS_DEFAULT = [
    "forhire",              # [Hiring] (client) / [For Hire] (candidate)
    "remotejobs",           # job posts (look for contract/agency-friendly)
    "slavelabour",          # small budget tasks (sometimes MVPs)
    "hiring",               # general hiring
    "freelance",            # freelancers & requests
    "EntrepreneurRideAlong",# solo founders seeking help
    "startups",             # founders asking for help
]

RSS_FEEDS = [
    # Funding signals (budget likely)
    "https://techcrunch.com/tag/funding/feed/",
    # Remote/contract dev jobs (potential outsource)
    "https://remoteok.com/remote-dev+contract-jobs.rss",
]

REQUEST_HEADERS = {"User-Agent": "LeadRadarPro/1.0 (+https://example.com)"}

CLIENT_HINTS = [
    "need developer",
    "looking for developer",
    "hiring contractor",
    "outsourcing",
    "agency",
    "mvp",
    "prototype",
    "poc",
    "[hiring]",
    "contract",
    "consultant needed",
]

CANDIDATE_HINTS = [
    "for hire",
    "open to work",
    "available for freelance",
    "available for contract",
    "[for hire]",
    "hire me",
    "seeking projects",
]

TRIGGER_KEYWORDS = {
    "funding": ["seed", "pre-seed", "series a", "series b", "venture funding", "raised", "funding", "round"],
    "launch": ["launched", "launching", "product hunt", "beta", "v1", "public launch", "go live"],
    "hiring_freeze": ["hiring freeze", "budget freeze", "cost cutting", "contractors only", "backfill with contractors"],
    "scale_up": ["scale", "scaling", "increasing demand", "rapid growth", "high growth"],
    "deadline": ["deadline", "urgent", "immediately", "asap", "deliver by", "time sensitive"],
}

SKILL_LIB = [
    "python","django","flask","fastapi","pandas",
    "javascript","typescript","react","node","next.js","vue","angular","svelte",
    "java","spring","kotlin","swift","objective-c",
    "ios","android","react native","flutter",
    "php","laravel","symfony","ruby","rails","go","rust","c#",".net","c++",
    "aws","gcp","azure","devops","docker","kubernetes","terraform",
    "postgres","mysql","mariadb","mongodb","redis","elasticsearch","graphql",
    "ai","ml","llm","nlp","computer vision","pytorch","tensorflow",
]

LOCK = threading.Lock()

# Optional paid enrichment (disabled by default; leave keys blank)
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
LUSHA_API_KEY = os.getenv("LUSHA_API_KEY", "")

# ---------------------------
# Helpers
# ---------------------------

def within_30_days(dt):
    return dt and dt >= EARLIEST_TS

def parse_any_dt(s):
    if not s:
        return None
    try:
        return dateparser.parse(s).astimezone(timezone.utc)
    except Exception:
        return None

def parse_unix_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except Exception:
        return None

def score_recency(dt):
    if not dt:
        return 0.0
    days = (NOW - dt).total_seconds() / 86400
    return max(0.0, min(1.0, 1.0 - (days / MAX_DAYS)))

def score_trigger(text):
    if not text:
        return 0.0
    t = text.lower()
    score = 0.0
    for bucket, kws in TRIGGER_KEYWORDS.items():
        hits = sum(1 for k in kws if k in t)
        if hits:
            weight = {"funding": 1.0, "launch": 0.8, "hiring_freeze": 0.7, "scale_up": 0.6, "deadline": 0.5}.get(bucket, 0.4)
            score += weight * min(hits, 3)
    return min(1.0, score / 3.0)

def score_engagement(points=None, comments=None):
    p = (points or 0)
    c = (comments or 0)
    v = math.log1p(p + 0.6 * c)
    return min(1.0, v / 5.0)

def score_accessibility(has_email, has_phone):
    base = 0.3 if has_email else 0.0
    if has_phone:
        base += 0.4
    return min(1.0, base)

def classify_post(title, text, subreddit=None):
    s = " ".join([title or "", text or ""]).lower()
    if subreddit and subreddit.lower() == "forhire":
        if "[for hire]" in s:
            return "Developer Candidate"
        if "[hiring]" in s:
            return "Potential Client"
    client_hits = sum(1 for w in CLIENT_HINTS if w in s)
    cand_hits = sum(1 for w in CANDIDATE_HINTS if w in s)
    if cand_hits > client_hits and cand_hits > 0:
        return "Developer Candidate"
    if client_hits > 0:
        return "Potential Client"
    if "hire" in s and "developer" in s:
        return "Potential Client"
    if any(w in s for w in ["available", "for hire"]) and "developer" in s:
        return "Developer Candidate"
    return None

def extract_urls(text):
    if not text: return []
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
    if not s: return ""
    return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)

def guess_industry(text):
    t = (text or "").lower()
    patterns = {
        "Fintech": ["fintech","payments","banking","trading","ledger","crypto","defi"],
        "Healthtech": ["health","med","clinic","ehr","wellness","fitness"],
        "E-commerce": ["shopify","ecommerce","storefront","marketplace","checkout"],
        "SaaS": ["saas","b2b","multi-tenant","subscription"],
        "Edtech": ["education","learning","edtech","course","lms"],
        "AI/ML": ["ai","ml","model","llm","computer vision","nlp"],
        "Logistics": ["logistics","fleet","delivery","supply chain"],
        "Real Estate": ["real estate","property","proptech"],
        "Travel": ["travel","booking","itinerary"],
        "Social": ["social","community","messaging","feed"],
    }
    for k, kws in patterns.items():
        if any(w in t for w in kws):
            return k
    return None

def detect_trigger(text):
    t = (text or "").lower()
    hits = []
    for label, kws in TRIGGER_KEYWORDS.items():
        if any(k in t for k in kws):
            hits.append(label.replace("_"," "))
    order = ["funding","launch","hiring freeze","scale up","deadline"]
    if hits:
        hits_sorted = sorted(hits, key=lambda x: order.index(x) if x in order else 99)
        return hits_sorted[0]
    return None

def find_emails(text):
    if not text: return []
    pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    return list({e.lower() for e in re.findall(pattern, text)})[:5]

def find_phones(text):
    if not text: return []
    # Use phonenumbers to parse; also catch tel: links
    candidates = set()
    for match in re.findall(r"(tel:\+?[0-9()\-\s]{7,20})", text, flags=re.I):
        candidates.add(match.split(":",1)[1])
    # Try parsing any digits-rich strings
    for m in re.findall(r"(\+?[0-9][0-9()\-\s]{6,20}[0-9])", text):
        try:
            for country in ["US","IN","GB","CA","AU","SG","DE","NL","FR","ES","SE","NO","DK","IE","AE"]:
                num = phonenumbers.parse(m, country)
                if phonenumbers.is_possible_number(num) and phonenumbers.is_valid_number(num):
                    candidates.add(phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
                    break
        except Exception:
            continue
    return list(candidates)[:5]

def company_from_urls(urls):
    for u in urls or []:
        d = extract_domain(u)
        if d and not any(x in d for x in ["reddit.com","news.ycombinator.com","github.com","medium.com","twitter.com","linkedin.com","remoteok.com","techcrunch.com"]):
            base = d.split(".")[0]
            return base.title(), f"https://{d}", d
    return None, None, None

def fetch_url(url, timeout=15):
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None

def safe_soup(url, timeout=15):
    r = fetch_url(url, timeout=timeout)
    if not r: return None
    try:
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return BeautifulSoup(r.text, "html.parser")

def text_from_page(url):
    soup = safe_soup(url)
    if not soup: return ""
    for t in soup(["script","style","noscript"]): t.extract()
    return soup.get_text(" ", strip=True)

def guess_contact_pages(base_url):
    if not base_url: return []
    return list(dict.fromkeys([
        base_url,
        base_url.rstrip("/") + "/contact",
        base_url.rstrip("/") + "/contact-us",
        base_url.rstrip("/") + "/about",
        base_url.rstrip("/") + "/team",
        base_url.rstrip("/") + "/careers",
        base_url.rstrip("/") + "/company",
    ]))

def scrape_emails_phones_from_site(base_url):
    found_emails, found_phones = set(), set()
    for url in guess_contact_pages(base_url):
        soup = safe_soup(url, timeout=12)
        if not soup: continue
        text = soup.get_text(" ", strip=True)
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("mailto:"):
                found_emails.add(a["href"].split(":",1)[1])
            if a["href"].lower().startswith("tel:"):
                found_phones.add(a["href"].split(":",1)[1])
        found_emails.update(find_emails(text))
        found_phones.update(find_phones(text))
        if len(found_emails) >= 5 and len(found_phones) >= 5:
            break
    return list(found_emails)[:5], list(found_phones)[:5]

def shorten(s, n=220):
    if not s: return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n-1].rstrip() + "…"

def dedupe_by_key(items, key_func):
    seen, out = set(), []
    for it in items:
        k = key_func(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out

# ---------------------------
# Free-source fetchers
# ---------------------------

def fetch_hn_algolia(keywords, max_pages=3):
    url = "https://hn.algolia.com/api/v1/search_by_date"
    query = " OR ".join(f"\"{k}\"" for k in keywords)
    out = []
    for page in range(max_pages):
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>{int(EARLIEST_TS.timestamp())}",
            "hitsPerPage": 100,
            "page": page
        }
        try:
            r = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=20)
            if r.status_code != 200: break
            data = r.json()
            for h in data.get("hits", []):
                created = parse_any_dt(h.get("created_at"))
                if not within_30_days(created): continue
                out.append({
                    "source": "Hacker News",
                    "title": h.get("title") or "",
                    "text": "",
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    "author": h.get("author"),
                    "created_at": created,
                    "points": h.get("points") or 0,
                    "comments": h.get("num_comments") or 0,
                    "subreddit": None,
                })
            if not data.get("hits"): break
            time.sleep(0.25)
        except Exception:
            break
    return out

def fetch_reddit_subreddit(subreddit, limit=120):
    url = f"https://www.reddit.com/r/{subreddit}/new.json"
    try:
        r = requests.get(url, params={"limit": str(limit)}, headers=REQUEST_HEADERS, timeout=20)
        if r.status_code != 200: return []
        data = r.json()
    except Exception:
        return []
    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = html.unescape(d.get("title", "")) or ""
        selftext = html.unescape(d.get("selftext", "")) or ""
        created = parse_unix_ts(d.get("created_utc"))
        if not within_30_days(created): continue
        items.append({
            "source": f"Reddit r/{subreddit}",
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

def fetch_rss(url):
    try:
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries:
            dt = parse_any_dt(getattr(e, "published", None) or getattr(e, "updated", None))
            if not within_30_days(dt): continue
            title = html_to_text(getattr(e, "title", "") or "")
            summary = html_to_text(getattr(e, "summary", "") or "")
            link = getattr(e, "link", "")
            out.append({
                "source": f"RSS {extract_domain(url) or 'feed'}",
                "title": title,
                "text": summary,
                "url": link,
                "author": getattr(e, "author", None),
                "created_at": dt,
                "points": None,
                "comments": None,
                "subreddit": None
            })
        return out
    except Exception:
        return []

# ---------------------------
# Build pipeline
# ---------------------------

def build_from_sources(client_kws, candidate_kws, subreddits, rss_feeds, max_workers=12):
    all_items = []

    # Parallel fetch
    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        tasks.append(ex.submit(fetch_hn_algolia, client_kws + candidate_kws, 3))
        for sub in set(subreddits or []):
            tasks.append(ex.submit(fetch_reddit_subreddit, sub, 120))
        for rss in set(rss_feeds or []):
            tasks.append(ex.submit(fetch_rss, rss))

        for fut in as_completed(tasks):
            try:
                all_items.extend(fut.result() or [])
            except Exception:
                continue

    # Classify
    potential_clients, developer_candidates = [], []
    for it in all_items:
        c = classify_post(it["title"], it["text"], subreddit=it.get("subreddit"))
        if not c:
            # Keyword filter pass
            blob = f"{it['title']} {it['text']}".lower()
            if any(k.lower() in blob for k in client_kws):
                c = "Potential Client"
            elif any(k.lower() in blob for k in candidate_kws):
                c = "Developer Candidate"
            else:
                continue
        # Extracts
        urls = extract_urls(f"{it['title']} {it['text']} {it.get('url','')}")
        comp_name, comp_site, comp_domain = company_from_urls(urls)
        emails_inline = find_emails(it["text"])
        phones_inline = find_phones(it["text"])
        trigger = detect_trigger(f"{it['title']} {it['text']}")
        industry = guess_industry(f"{it['title']} {it['text']}")
        # Score
        rec_score = score_recency(it["created_at"])
        trig_score = score_trigger(f"{it['title']} {it['text']}")
        eng_score = score_engagement(it.get("points"), it.get("comments"))
        access_score = score_accessibility(bool(emails_inline), bool(phones_inline))
        score = round(0.38*trig_score + 0.30*rec_score + 0.18*eng_score + 0.14*access_score, 4)

        record = {
            **it,
            "urls": urls,
            "company_name_guess": comp_name,
            "company_website_guess": comp_site,
            "company_domain_guess": comp_domain,
            "emails_inline": emails_inline,
            "phones_inline": phones_inline,
            "trigger": trigger,
            "industry_guess": industry,
            "score": score
        }

        if c == "Potential Client":
            potential_clients.append(record)
        else:
            developer_candidates.append(record)

    # Enrich clients: scrape site contact info (free)
    def enrich_client(doc):
        site = doc.get("company_website_guess")
        emails, phones = [], []
        site_title, site_desc = None, None
        if site:
            soup = safe_soup(site)
            if soup:
                site_title = soup.title.get_text(strip=True) if soup.title else None
                meta = soup.find("meta", attrs={"name": "description"})
                site_desc = meta["content"].strip() if meta and meta.get("content") else None
            e2, p2 = scrape_emails_phones_from_site(site)
            emails = list(dict.fromkeys(doc["emails_inline"] + e2))[:5]
            phones = list(dict.fromkeys(doc["phones_inline"] + p2))[:5]
        else:
            emails, phones = doc["emails_inline"], doc["phones_inline"]

        # Update access score with site finds
        access_score = score_accessibility(bool(emails), bool(phones))
        rec_score = score_recency(doc["created_at"])
        trig_score = score_trigger(f"{doc['title']} {doc['text']}")
        eng_score = score_engagement(doc.get("points"), doc.get("comments"))
        new_score = round(0.36*trig_score + 0.28*rec_score + 0.16*eng_score + 0.20*access_score, 4)

        doc.update({
            "site_title": site_title,
            "site_desc": site_desc,
            "emails": emails,
            "phones": phones,
            "score": new_score
        })
        return doc

    with ThreadPoolExecutor(max_workers=10) as ex:
        potential_clients = list(ex.map(enrich_client, potential_clients))

    # Enrich candidates: skills, availability, YoE, location
    for d in developer_candidates:
        t = (d["title"] + " " + d["text"]).lower()
        skills = sorted({s for s in SKILL_LIB if s in t})
        avail = "Immediate" if any(k in t for k in ["immediate","asap","available now"]) else "Notice Period"
        m_yoe = re.search(r"(\d{1,2})\+?\s*(?:years|yrs|y)", t)
        yoe = int(m_yoe.group(1)) if m_yoe else None
        m_loc = re.search(r"(remote|india|usa|europe|uk|canada|australia|singapore|germany|netherlands|uae|dubai)", t)
        loc = m_loc.group(1).title() if m_loc else "Remote/Unspecified"
        # Try portfolios
        urls = d.get("urls", [])
        portfolios = [u for u in urls if any(x in u for x in ["github.com","gitlab.com","behance.net","dribbble.com","codepen.io","linkedin.com","portfolio","notion.site"])]
        d.update({
            "skills": skills[:15],
            "availability": avail,
            "yoe": yoe,
            "location_guess": loc,
            "portfolios": portfolios[:5]
        })

    # Dedupe by company domain for clients and by author+title for candidates
    potential_clients = dedupe_by_key(potential_clients, lambda x: x.get("company_domain_guess") or extract_domain(x.get("url") or "") or x.get("url"))
    developer_candidates = dedupe_by_key(developer_candidates, lambda x: (x.get("author"), x.get("title")))

    # Rank
    potential_clients.sort(key=lambda x: x["score"], reverse=True)
    developer_candidates.sort(key=lambda x: x["score"], reverse=True)

    return potential_clients, developer_candidates

# ---------------------------
# Rendering
# ---------------------------

def render_markdown(clients, candidates, top_n_clients=30, top_n_candidates=30):
    lines = []
    lines.append("## Potential Clients")
    if not clients:
        lines.append("- No results found in the last 30 days.")
    else:
        for c in clients[:top_n_clients]:
            cname = c.get("company_name_guess") or "(Company TBD)"
            website = c.get("company_website_guess") or c.get("url")
            industry = c.get("industry_guess") or "Unknown"
            location = "Unknown"
            snippet = shorten(c.get("text") or c.get("site_desc") or c.get("title"), 280)
            contact_bits = []
            if c.get("emails"):
                contact_bits.append(c["emails"][0])
            if c.get("phones"):
                contact_bits.append(c["phones"][0])
            contact_line = " | ".join(contact_bits) if contact_bits else website
            trig = c.get("trigger") or "Not specified"
            lines.append(f"- **{cname}:** {website} | {industry} | Score {c['score']}")
            lines.append(f"  - **{cname} – Opportunity Summary:** {snippet} (Source: {c['source']})")
            lines.append(f"  - **Contact:** {contact_line}")
            lines.append(f"  - **Trigger Event:** {trig}")
            lines.append(f"  - **Post:** {c['title']} | {c['url']}")
            lines.append("")
    lines.append("## Developer Candidates")
    if not candidates:
        lines.append("- No results found in the last 30 days.")
    else:
        for d in candidates[:top_n_candidates]:
            name = (d.get("author") or "Developer") + " (" + (d.get("source") or "") + ")"
            skills = ", ".join(d.get("skills", [])[:10]) or "Skills not specified"
            portfolios = d.get("portfolios") or [d.get("url")]
            availability = d.get("availability") or "Notice Period"
            yoe = f"{d.get('yoe')} years" if d.get("yoe") else "N/A"
            loc = d.get("location_guess") or "Remote/Unspecified"
            lines.append(f"- **{name} – Skills Summary:** {skills}")
            lines.append(f"  - **Portfolio:** " + " | ".join(portfolios[:3]))
            lines.append(f"  - **Availability:** {availability} | **Experience:** {yoe} | **Location:** {loc}")
            lines.append(f"  - **Post:** {d['title']} | {d['url']}")
            lines.append("")
    return "\n".join(lines)

def to_clients_df(clients):
    rows = []
    for c in clients:
        rows.append({
            "Company": c.get("company_name_guess"),
            "Website": c.get("company_website_guess") or c.get("url"),
            "Industry": c.get("industry_guess"),
            "Trigger": c.get("trigger"),
            "Emails": ", ".join(c.get("emails", [])),
            "Phones": ", ".join(c.get("phones", [])),
            "Score": c.get("score"),
            "Source": c.get("source"),
            "Post Title": c.get("title"),
            "Post URL": c.get("url"),
            "Created": c.get("created_at").isoformat() if c.get("created_at") else None,
        })
    return pd.DataFrame(rows)

def to_candidates_df(cands):
    rows = []
    for d in cands:
        rows.append({
            "Handle": d.get("author"),
            "Skills": ", ".join(d.get("skills", [])),
            "Availability": d.get("availability"),
            "YoE": d.get("yoe"),
            "Location": d.get("location_guess"),
            "Portfolios": ", ".join(d.get("portfolios", [])),
            "Score": d.get("score"),
            "Source": d.get("source"),
            "Post Title": d.get("title"),
            "Post URL": d.get("url"),
            "Created": d.get("created_at").isoformat() if d.get("created_at") else None,
        })
    return pd.DataFrame(rows)

# ---------------------------
# Streamlit UI
# ---------------------------

st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide")
st.title(APP_NAME)
st.caption("Free-source lead discovery with phones + emails. Last 30 days. Ranked for maximum sales leverage.")

with st.sidebar:
    st.header("Discovery inputs")
    client_kw = st.text_area("Client intent keywords (comma-separated)", value=", ".join(DEFAULT_CLIENT_KEYWORDS))
    cand_kw = st.text_area("Candidate intent keywords (comma-separated)", value=", ".join(DEFAULT_CANDIDATE_KEYWORDS))
    subs_default = ", ".join(REDDIT_SUBS_DEFAULT)
    subreddits = st.text_input("Reddit subreddits (comma-separated)", value=subs_default)
    rss_default = ", ".join(RSS_FEEDS)
    rss_feeds_str = st.text_input("RSS feeds (comma-separated)", value=rss_default)
    st.markdown("---")
    st.header("Filters")
    min_score = st.slider("Minimum lead score", 0, 100, 50, step=5)
    industry_filter = st.multiselect("Industries", ["Fintech","Healthtech","E-commerce","SaaS","Edtech","AI/ML","Logistics","Real Estate","Travel","Social"])
    require_contact = st.checkbox("Require email or phone", value=False)
    st.markdown("---")
    st.header("Optional enrichment (paid; leave blank to skip)")
    APOLLO_API_KEY = st.text_input("APOLLO_API_KEY", value=os.getenv("APOLLO_API_KEY",""), type="password")
    LUSHA_API_KEY = st.text_input("LUSHA_API_KEY", value=os.getenv("LUSHA_API_KEY",""), type="password")
    st.markdown("---")
    run = st.button("Run discovery")

if run:
    with st.spinner("Harvesting sources, extracting phones/emails, and ranking leads..."):
        client_kws = [x.strip() for x in client_kw.split(",") if x.strip()]
        cand_kws = [x.strip() for x in cand_kw.split(",") if x.strip()]
        subs = [x.strip() for x in subreddits.split(",") if x.strip()]
        rss_list = [x.strip() for x in rss_feeds_str.split(",") if x.strip()]
        clients, candidates = build_from_sources(client_kws, cand_kws, subs, rss_list)

        # Apply filters
        def pass_filters_client(c):
            if c["score"]*100 < min_score: return False
            if industry_filter and (c.get("industry_guess") not in industry_filter): return False
            if require_contact and not ((c.get("emails") and len(c["emails"])>0) or (c.get("phones") and len(c["phones"])>0)):
                return False
            return True

        clients_f = [c for c in clients if pass_filters_client(c)]
        candidates_f = [d for d in candidates if d["score"]*100 >= min_score]

        md = render_markdown(clients_f, candidates_f, top_n_clients=50, top_n_candidates=50)

    st.success(f"Found {len(clients_f)} potential clients and {len(candidates_f)} developer candidates (filtered).")

    # Downloads
    st.download_button("Download markdown", data=md.encode("utf-8"), file_name="lead_radar_pro.md", mime="text/markdown")

    cdf = to_clients_df(clients_f)
    ddf = to_candidates_df(candidates_f)
    st.download_button("Download clients (CSV)", data=cdf.to_csv(index=False).encode("utf-8"), file_name="clients.csv", mime="text/csv")
    st.download_button("Download candidates (CSV)", data=ddf.to_csv(index=False).encode("utf-8"), file_name="candidates.csv", mime="text/csv")

    # Display
    st.markdown(md)

    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Clients table")
        st.dataframe(cdf, use_container_width=True)
    with col2:
        st.subheader("Candidates table")
        st.dataframe(ddf, use_container_width=True)

    # Call-ready view
    with st.expander("Call-ready sheets (top 20)"):
        for c in clients_f[:20]:
            cname = c.get("company_name_guess") or "Company"
            website = c.get("company_website_guess") or c.get("url")
            phones = " | ".join(c.get("phones", [])[:2]) or "Phone: N/A"
            emails = " | ".join(c.get("emails", [])[:2]) or "Email: N/A"
            st.markdown(f"**{cname}** — {website}  \n"
                        f"Trigger: {c.get('trigger') or 'None'} | Industry: {c.get('industry_guess') or 'Unknown'} | Score: {int(c['score']*100)}  \n"
                        f"{phones}  \n"
                        f"{emails}  \n"
                        f"Post: {c['title']}  \n"
                        f"Link: {c['url']}")
else:
    st.info("Set keywords/subreddits/feeds on the left, then click Run discovery. Results are limited to the past 30 days.")


