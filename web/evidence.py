"""Conservative URL deduplication and bounded public-page link evidence."""
import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

NAME = re.compile(r"[\w][\w.\-]{0,63}", re.UNICODE)
REVIEW_STATES = {"pending": "待核实", "confirmed": "已人工核实", "rejected": "已排除"}


def valid_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def variants(username):
    candidates = [username, username.lower()]
    for separator in ("_", "-", ".", ""):
        candidates.append(re.sub(r"[_.-]+", lambda _: separator, username))
    return list(dict.fromkeys(v for v in candidates if valid_name(v)))[:6]


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return ""
    try:
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            return ""
        if p.port not in (None, 80, 443):
            return ""
        return value
    except ValueError:
        return ""


def canonical_url(value):
    if not safe_url(value):
        return ""
    p = urlsplit(value)
    host = p.hostname.lower()
    # GitHub redirects www and treats account names case-insensitively.
    github = host in ("github.com", "www.github.com")
    host = "github.com" if github else host
    netloc = f"[{host}]" if ":" in host else host
    if p.port and not (p.scheme == "https" and p.port == 443 or p.scheme == "http" and p.port == 80):
        netloc += ":" + str(p.port)
    path = p.path or "/"
    if github:
        path = path.rstrip("/").lower() or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")))
    return urlunsplit((p.scheme, netloc, path, query, ""))


def accounts(job):
    merged = {}
    for row in job["rows"]:
        if row["status"] != "Claimed":
            continue
        key = canonical_url(row.get("url", ""))
        if not key:
            continue
        entry = merged.setdefault(key, {"key": key, "site": row["site"], "url": key,
            "status": "Claimed", "review": job.get("reviews", {}).get(key, "pending"),
            "sources": [], "evidence": "Sherlock 站点规则检测；尚未验证账号归属"})
        source = {"username": row.get("username", job["username"]), "site": row["site"],
                  "url": row["url"], "checked_at": row.get("checked_at", "")}
        if source not in entry["sources"]:
            entry["sources"].append(source)
    return list(merged.values())


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.navigation = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("nav", "header", "footer"):
            self.navigation += 1
        if tag.lower() != "a" or len(self.links) >= 500:
            return
        attrs = dict(attrs)
        href = attrs.get("href", "") or ""
        if len(href) <= 2048:
            declaration = "rel=me" if "me" in (attrs.get("rel", "") or "").lower().split() else ""
            if not declaration and "url" in (attrs.get("itemprop", "") or "").lower().split():
                declaration = "itemprop=url"
            if declaration or not self.navigation:
                self.links.append((href, declaration))

    def handle_endtag(self, tag):
        if tag.lower() in ("nav", "header", "footer"):
            self.navigation = max(0, self.navigation - 1)


def public_links(source, html, definitions, username):
    """Only declared profile links or exact supported-profile URLs; no fetching."""
    if not safe_url(source) or not html:
        return []
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    if "<a" not in html[:1_000_000].lower():
        return []
    parser = Anchors()
    parser.feed(html[:1_000_000])
    patterns = []
    for site, definition in definitions.items():
        template = definition.get("url", "")
        if template.count("{}") == 1 and safe_url(template.replace("{}", "fixture")):
            normalized = canonical_url(template.replace("{}", "profileplaceholder"))
            if normalized.count("profileplaceholder") != 1:
                continue
            before, after = normalized.split("profileplaceholder")
            patterns.append((site, re.compile(re.escape(before) + r"([\w.\-]{1,64})" + re.escape(after) + r"/?$")))
    reserved = {"login", "signup", "about", "explore", "features", "settings", "pricing", "help", "search", "support", "terms", "privacy", "contact", "blog", "platform", "marketplace", "trending", "enterprise", "team", "why-github", "customer-stories", "trust-center", "resources", "partners", "security"}
    result, seen = [], set()
    for href, declaration in sorted(parser.links, key=lambda pair: not bool(pair[1])):
        target = safe_url(urljoin(source, href))
        if not target or canonical_url(source) == canonical_url(target):
            continue
        target_site = "公开主页"
        matched = False
        for site, pattern in patterns:
            match = pattern.fullmatch(canonical_url(target))
            if match and match[1].lower() not in reserved:
                matched, target_site = True, site
                break
        if not declaration and not matched:
            continue
        key = hashlib.sha256((canonical_url(source) + "\n" + canonical_url(target)).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": key, "from_url": source, "to_url": target, "site": target_site,
            "username": username, "review": "pending", "observed_at": timestamp(),
            "evidence": f"公开资料外链（{declaration} 声明，未经归属核实）" if declaration else "公开页面包含平台主页链接（可能是导航或他人账号）"})
        if len(result) >= 20:
            break
    return result
