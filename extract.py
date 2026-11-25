"""Turn church web pages into published contacts. Standard library only.

Rules: only emails that appear on the page (mailto links, visible text, or
Cloudflare-protected addresses that a visitor sees). Nothing is ever guessed
or built from a name.
"""
import html
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-']{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}")
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]{1,64})\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9\-]{1,63}(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[A-Za-z0-9\-]{1,63})+)",
    re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)")

ROLE_PATTERNS = [
    r"youth\s*(?:&|and|/)?\s*(?:family\s+)?(?:pastor|director|minister|ministries\s+director|coordinator|leader|ministry\s+(?:director|leader|pastor|coordinator))",
    r"(?:pastor|director|minister|coordinator)\s+(?:of|for)\s+(?:youth\s*(?:&|and)\s*famil(?:y|ies)|youth|students?|next\s*gen(?:eration)?|famil(?:y|ies))(?:\s+(?:&|and)\s+families)?(?:\s+ministr(?:y|ies))?",
    r"students?\s+(?:pastor|director|minister|ministries\s+(?:director|pastor|coordinator)|ministry\s+(?:director|pastor|coordinator))",
    r"student\s+ministries", r"student\s+ministry",
    r"(?:middle|high)\s+school\s+(?:pastor|director|minister|ministry\s+(?:director|pastor))",
    r"next\s*-?\s*gen(?:eration)?\s+(?:pastor|director|minister|ministries|ministry)",
    r"famil(?:y|ies)\s+(?:pastor|minister|ministries\s+(?:pastor|director))",
    r"creative\s+(?:arts\s+)?pastor",
    r"youth\s+pastor", r"youth\s+director", r"youth\s+minister",
]
ROLE_RE = re.compile(r"\b(?:" + "|".join(ROLE_PATTERNS) + r")\b", re.I)
# "Associate Pastor of Youth", "Lead Student Pastor" etc. – grab a short prefix too
ROLE_PREFIX_RE = re.compile(r"\b((?:(?:lead|senior|associate|assistant|executive|interim|co-?)\s+){0,2})$", re.I)

YOUTH_SIGNAL_RE = re.compile(
    r"\b(youth|student\s+ministr|students|next\s*-?\s*gen|middle\s+school|high\s+school|teens?|jr\.?\s+high|sr\.?\s+high|grades?\s+6|6th\s*-\s*12th)\b",
    re.I)

ROLE_INBOX_PREFIXES = ("youth", "students", "student", "studentministr", "nextgen", "next-gen", "next.gen",
                       "ym", "hsm", "msm", "youthgroup", "youthministr", "teens", "wired", "family", "families",
                       "fam", "creative")
GENERAL_INBOX = ("info", "office", "church", "contact", "hello", "admin", "secretary", "welcome", "connect",
                 "mail", "frontdesk", "reception", "communications", "churchoffice", "parishoffice", "parish")

PLACEHOLDER_BITS = ("example.", "sample", "test@", "@test", "yourname", "your-name", "your.name", "youremail",
                    "your-email", "noreply", "no-reply", "donotreply", "do-not-reply", "name@", "email@domain",
                    "user@", "username@", "someone@", "johndoe", "john.doe", "janedoe", "jane.doe", "first.last",
                    "firstname", "lastname", "@domain.", "@email.", "@mail.com", "@yourdomain", "@yourchurch",
                    "@company", "sentry", "wixpress", "squarespace", "@sentry", "godaddy", "@wix.", "webmaster@wix",
                    "@2x", "@3x", "placeholder", "changeme", "privacy@", "abuse@", "postmaster@", "hostmaster@",
                    "dmca@", "support@wix", "subscriptions@", "unsubscribe", "@example", "website@", "webmaster@",
                    "@sentry.io", "@ingest.", "@cloudflare", "@mailchimp", "@constantcontact", "@planningcenter",
                    "@churchcenter", "@subsplash", "@tithe.ly", "@pushpay", "@faithlife", "@ministrybrands")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tif", ".tiff", ".avif",
             ".js", ".css", ".pdf", ".mp4", ".mov", ".woff", ".woff2", ".ttf")

NOT_YOUTH_RE = re.compile(r"preschool|pre-school|children|childrens|kids|nursery|elementary|early\s+childhood", re.I)
SENTENCE_RE = re.compile(r"^(email|e-mail|from|please|contact|reach|call|ask|for|our|meet|about)\b|\binformation\b|\bour\b", re.I)
TITLE_WORD_RE = re.compile(r"\b(director|pastor|minister|coordinator|manager|assistant|administrator|admin|leader|secretary|bookkeeper|treasurer|elder|deacon|intern|accountant|custodian|facilities|chair|receptionist|clerk|organist|pianist|music|worship|technical|communications|finance|operations|children|kids|preschool|nursery|missions|care|women|men)\b", re.I)
NAME_STOPWORDS = {
    "youth", "student", "students", "ministry", "ministries", "church", "contact", "email", "e-mail", "staff",
    "office", "our", "the", "lead", "senior", "pastor", "pastors", "director", "about", "team", "worship",
    "children", "childrens", "kids", "family", "families", "next", "gen", "nextgen", "middle", "high", "school",
    "us", "me", "click", "here", "send", "message", "phone", "call", "meet", "leadership", "elders", "deacons",
    "minister", "associate", "assistant", "executive", "admin", "administrator", "coordinator", "creative",
    "music", "media", "welcome", "home", "events", "give", "giving", "sermons", "watch", "live", "online",
    "visit", "new", "here", "join", "group", "groups", "small", "life", "bible", "study", "sunday", "wednesday",
    "service", "services", "times", "location", "directions", "map", "facebook", "instagram", "youtube",
    "more", "read", "learn", "info", "information", "fellowship", "baptist", "methodist", "lutheran",
    "presbyterian", "catholic", "community", "chapel", "first", "united", "christian", "assembly", "god",
    "jesus", "christ", "lord", "holy", "spirit", "saint", "st", "parish", "mission", "outreach", "care",
    "operations", "business", "finance", "communications", "of", "and", "for", "to", "with", "at", "in",
    "interim", "co", "escape", "rooms", "room", "gang", "camp", "night", "crew", "club", "retreat", "trip", "registration", "calendar", "update", "updates", "intern", "volunteer", "volunteers", "teacher", "coach", "grades", "grade", "jr", "sr",
    "rev", "dr", "mr", "mrs", "ms", "miss", "fr", "father", "deacon", "elder", "bishop", "minister", "vacant",
    "open", "position", "hiring", "tbd", "tba", "coming", "soon", "west", "east", "north", "south", "campus",
}
NAME_TITLES_RE = re.compile(r"^(?:(?:pastor|pastors|rev\.?|reverend|dr\.?|mr\.?|mrs\.?|ms\.?|miss|fr\.?|father|deacon|elder|minister)\s+)+", re.I)
NAME_TOKEN_RE = re.compile(r"^(?:[A-Z][a-zA-Z'’\-]{1,20}|[A-Z]\.|Mc[A-Z][a-z]+|O'[A-Z][a-z]+)$")
NAME_SUFFIXES = {"Jr.", "Jr", "Sr.", "Sr", "II", "III", "IV"}

BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "br", "td", "th", "tr",
              "table", "section", "article", "header", "footer", "aside", "figure", "figcaption", "dd", "dt",
              "dl", "blockquote", "hr", "main", "nav", "form", "label", "address", "button"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head", "iframe", "select", "option"}


def decode_cfemail(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


class PageParser(HTMLParser):
    """Splits a page into visible text blocks, records which emails sit in which block, and collects links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []        # list of [text, set(emails), set(source)]
        self.cur_text = []
        self.cur_emails = set()
        self.skip_depth = 0
        self.links = []         # (href, anchor_text)
        self.title = ""
        self.site_name = ""
        self._in_title = False
        self._a_href = None
        self._a_text = []

    def flush(self):
        text = re.sub(r"\s+", " ", " ".join(self.cur_text)).strip()
        if text or self.cur_emails:
            self.blocks.append([text, set(self.cur_emails)])
        self.cur_text, self.cur_emails = [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("property") or a.get("name") or "").lower() in ("og:site_name", "application-name"):
            self.site_name = self.site_name or (a.get("content") or "").strip()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in BLOCK_TAGS:
            self.flush()
        cf = a.get("data-cfemail")
        if cf:
            e = decode_cfemail(cf)
            if e:
                self.cur_emails.add(e)
                self.cur_text.append(e)
        if tag == "a":
            href = (a.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                addr = unquote(href[7:].split("?")[0]).strip()
                for part in addr.split(","):
                    if part.strip():
                        self.cur_emails.add(part.strip())
            elif "/cdn-cgi/l/email-protection#" in href:
                e = decode_cfemail(href.split("#", 1)[1])
                if e:
                    self.cur_emails.add(e)
            else:
                self._a_href = href
                self._a_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, " ".join(self._a_text).strip()))
            self._a_href = None
        if tag in BLOCK_TAGS:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("a",):
            pass

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.skip_depth:
            return
        self.cur_text.append(data)
        if self._a_href is not None:
            self._a_text.append(data)

    def close(self):
        super().close()
        self.flush()


def parse_page(html_text):
    p = PageParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        p.flush()
    # visible text emails (plain and "name [at] church [dot] org")
    for block in p.blocks:
        for m in EMAIL_RE.finditer(block[0]):
            block[1].add(m.group(0))
        for m in OBFUSCATED_RE.finditer(block[0]):
            dom = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", m.group(2), flags=re.I)
            block[1].add(f"{m.group(1)}@{dom}")
    return p


def clean_email(e):
    e = html.unescape(unquote(e)).strip().strip(".,;:()[]<>\"'").lower()
    e = e.replace("%20", "")
    if e.startswith("mailto:"):
        e = e[7:]
    if not EMAIL_RE.fullmatch(e):
        return None
    if len(e) > 80:
        return None
    if any(e.endswith(ext) for ext in IMAGE_EXT) or re.search(r"\.(png|jpe?g|gif|webp|svg)@", e):
        return None
    if any(bit in e for bit in PLACEHOLDER_BITS):
        return None
    local, dom = e.split("@", 1)
    if re.search(r"\.(com|org|net|church|ca|us)\.(com|org|net)$", dom):  # typos like gmail.com.com
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hashed tracking addresses
        return None
    return e


def is_role_inbox(email):
    local = re.sub(r"[^a-z]", "", email.split("@")[0])
    return any(local.startswith(re.sub(r"[^a-z]", "", p)) for p in ROLE_INBOX_PREFIXES)


def is_general_inbox(email):
    local = re.sub(r"[^a-z]", "", email.split("@")[0])
    return any(local.startswith(p) for p in GENERAL_INBOX)


def find_role(text):
    m = ROLE_RE.search(text)
    if not m:
        return None
    if NOT_YOUTH_RE.search(text[max(0, m.start() - 25):m.end() + 5]) and not re.search(r"youth|student|teen", m.group(0), re.I):
        return None  # "Preschool & Families Minister", "Children's and Family Pastor"
    # short title line ("Student Ministry Assistant", "Name | Next Gen Pastor"): keep the whole title
    for seg in re.split(r"\s*[|•·–—:,]\s*|\s-\s", text):
        seg = seg.strip()
        if ROLE_RE.search(seg) and len(seg) <= 50 and "@" not in seg and len(seg.split()) <= 7 \
                and not SENTENCE_RE.search(seg):
            seg = re.sub(r"\s+", " ", seg)
            return seg.title() if seg.isupper() else seg
    prefix = ROLE_PREFIX_RE.search(text[:m.start()])
    role = ((prefix.group(1) if prefix else "") + m.group(0)).strip()
    role = re.sub(r"\s+", " ", role)
    return role.title().replace(" Of ", " of ").replace(" And ", " and ").replace(" For ", " for ")


def _name_from_segment(seg):
    seg = seg.strip(" -–—|:,•·")
    seg = NAME_TITLES_RE.sub("", seg).strip()
    seg = re.sub(r"\s*\([^)]*\)", "", seg)  # drop "(he/him)" etc
    tokens = seg.split()
    while tokens and tokens[-1].rstrip(",") in NAME_SUFFIXES:
        tokens.pop()
    if not 2 <= len(tokens) <= 4:
        return None
    for t in tokens:
        t2 = t.rstrip(",")
        if re.search(r"['’]s$", t2):
            return None
        if not NAME_TOKEN_RE.match(t2):
            return None
        if t2.lower().strip(".") in NAME_STOPWORDS:
            return None
    if not re.match(r"^[A-Z][a-z]", tokens[0]):  # first name must be a real word, not an initial
        if len(tokens) < 3:
            return None
    return " ".join(t.rstrip(",") for t in tokens)


def find_names(text):
    """Return candidate person names found in a block of text (exact text, never invented)."""
    names = []
    cleaned = EMAIL_RE.sub(" | ", text)
    cleaned = ROLE_RE.sub(" | ", cleaned)
    cleaned = PHONE_RE.sub(" | ", cleaned)
    for seg in re.split(r"[|•·–—:\n,;/]|\s-\s|\s{2,}", cleaned):
        seg = seg.strip()
        if not seg or len(seg) > 45:
            continue
        n = _name_from_segment(seg)
        if n:
            names.append(n)
    return names


def name_matches_email(name, email):
    local = re.sub(r"[^a-z]", "", email.split("@")[0])
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in name.split()]
    parts = [p for p in parts if len(p) > 1]
    if not parts or not local:
        return False
    first, last = parts[0], parts[-1]
    if any(len(p) >= 3 and p in local for p in parts):   # any real name part, e.g. a middle name
        return True
    if len(first) >= 3 and local.startswith(first[:3]):          # maxirona -> Maxine
        return True
    if local.startswith(first[0]) and len(last) >= 4 and last[:4] in local:   # jsmith, jsmit
        return True
    if len(parts) >= 2 and local in (first[0] + last[0], first[0] + parts[1][0] + last[0]):  # initials
        return True
    return False


def contacts_from_page(page_url, parsed):
    """Yield dicts: email, role, full_name, source_url, kind ('A'|'B'|'general'|None)."""
    blocks = parsed.blocks
    out = []
    for i, (text, emails) in enumerate(blocks):
        if not emails:
            continue
        for raw in emails:
            email = clean_email(raw)
            if not email:
                continue
            # context: the email's block, then up to 3 blocks before it, then 1 after.
            # A block holding a *different* email belongs to another person: stop there.
            trimmed = [(i, text)]
            for j in (i - 1, i - 2, i - 3):
                if j < 0:
                    break
                other = {clean_email(x) for x in blocks[j][1]} - {None, email}
                if other:
                    break
                trimmed.append((j, blocks[j][0]))
            if i + 1 < len(blocks):
                other = {clean_email(x) for x in blocks[i + 1][1]} - {None, email}
                if not other:
                    trimmed.append((i + 1, blocks[i + 1][0]))
            # if one block holds several people, keep only the text that belongs to this email
            if len({clean_email(x) for x in emails} - {None}) > 1:
                low = text.lower()
                pos = low.find(email)
                if pos >= 0:
                    prev_end = 0
                    for m in EMAIL_RE.finditer(low[:pos]):
                        prev_end = m.end()
                    trimmed[0] = (i, text[prev_end:pos + len(email)])

            # 1) find the person's name (exact text on the page)
            name, name_j = None, None
            for j, t in sorted((x for x in trimmed if x[0] <= i), key=lambda x: -x[0]):
                for n in find_names(t):
                    if name_matches_email(n, email):
                        name, name_j = n, j
                        break
                if name:
                    break
            if not name and not is_role_inbox(email) and not is_general_inbox(email):
                for j, t in trimmed:
                    if j in (i, i - 1):
                        near = find_names(t)
                        if near:
                            name, name_j = (near[-1] if j == i - 1 else near[0]), j
                            break

            # 2) the role must sit with this person: in the blocks from their name down to their email
            span = [(j, t) for j, t in trimmed if (name_j if name_j is not None else i - 1) <= j <= i]
            role = None
            for j, t in span:
                if len(t) > 90 and j != i:   # prose (bios) don't count, only title lines
                    continue
                if j == i and len(t) > 160:
                    continue
                role = find_role(t)
                if role:
                    break
            if not role and not any(TITLE_WORD_RE.search(t) for _, t in span):
                # no title of their own: accept a short section heading just above (e.g. "Student Ministry")
                top = min(j for j, _ in span)
                for j, t in trimmed:
                    if j in (top - 1, top - 2) and len(t) <= 60:
                        role = find_role(t)
                        if role:
                            break
            out.append({"email": email, "role": role or "", "full_name": name or "", "source_url": page_url})
    return out


def first_phone(text):
    m = PHONE_RE.search(text)
    if not m:
        return ""
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def has_youth_signal(text):
    return bool(YOUTH_SIGNAL_RE.search(text))


PAGE_KEYWORDS = [
    # (regex on url path or anchor text, priority: lower first)
    (re.compile(r"youth|student|next-?gen|nextgen|teen|middle-?school|high-?school|\bms\b|\bhs\b|family-ministr|famil(y|ies)", re.I), 0),
    (re.compile(r"staff|team|leadership|our-?pastors|pastors|meet-?(us|the)|who-?we-?are|elders|ministers|people", re.I), 1),
    (re.compile(r"about", re.I), 2),
    (re.compile(r"contact|connect", re.I), 3),
    (re.compile(r"ministr", re.I), 4),
]
BAD_LINK_RE = re.compile(r"\.(pdf|jpe?g|png|gif|mp3|mp4|zip|docx?|xlsx?|pptx?)(\?|$)|/(wp-admin|wp-login|login|cart|checkout|feed|tag|category|author|give|donate|sermons?/\d|events?/\d)", re.I)


def pick_links(base_url, links, host):
    """Return up to N same-site candidate URLs ranked by how likely they hold youth staff contacts."""
    scored = {}
    for href, text in links:
        if not href or href.startswith(("#", "javascript:", "tel:", "sms:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        pu = urlparse(url)
        if pu.scheme not in ("http", "https"):
            continue
        h = pu.netloc.lower().removeprefix("www.")
        if h != host:
            continue
        if BAD_LINK_RE.search(pu.path):
            continue
        hay = f"{pu.path} {text}"
        best = None
        for rx, prio in PAGE_KEYWORDS:
            if rx.search(hay):
                best = prio if best is None else min(best, prio)
        if best is None:
            continue
        depth = pu.path.strip("/").count("/")
        score = best * 10 + depth
        url = url.rstrip("/") or url
        if url not in scored or score < scored[url]:
            scored[url] = score
    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1])]


CHURCH_WORD_RE = re.compile(r"church|chapel|parish|fellowship|ministr|assembly|cathedral|tabernacle|congregation|community|bible|baptist|methodist|lutheran|presbyterian|catholic|episcopal|life|grace|hope|faith|christ", re.I)
GENERIC_TITLE_RE = re.compile(r"^(home|welcome|homepage|index|untitled|main)$", re.I)


def church_name_from_page(parsed, host):
    """Best guess at the church's own name from its homepage (og:site_name, then <title>), never invented."""
    cands = []
    if parsed.site_name:
        cands.append(parsed.site_name)
    t = re.sub(r"\s+", " ", parsed.title or "").strip()
    if t:
        parts = [p.strip() for p in re.split(r"\s+[|\-–—:•·]\s+", t) if p.strip()]
        parts = [p for p in parts if not GENERIC_TITLE_RE.match(p)]
        cands += sorted(parts, key=lambda p: (0 if CHURCH_WORD_RE.search(p) else 1, len(p)))
    for c in cands:
        c = html.unescape(c).strip()
        if 3 <= len(c) <= 70 and not GENERIC_TITLE_RE.match(c):
            return c
    return ""
