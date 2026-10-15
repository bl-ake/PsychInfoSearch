"""APA PsycInfo through the EBSCOhost website, in a Firefox window you have logged in to.

Each search loads a results page by address, the way the site's own links do, and reads the
count and the first page of records. No account details are handled here: you log in by hand.

EBSCO's terms of use prohibit automated access to the website, so this is deliberately slow and
small: one visible window, a pause between searches, a cap on searches per run, results pages
only (no record pages, no full text), and on a block or CAPTCHA it pauses with an alert in the window until you clear it by hand (it never works around one).

Settings (environment or .env):
    EBSCOHOST_URL            any address from your EBSCO search session (default: UConn's PsycInfo,
                             https://research.ebsco.com/c/q67rbq/search)
    EBSCOHOST_DB             database short name (default psyh = APA PsycInfo)
    EBSCOHOST_DELAY          seconds between searches (default 5, minimum 3)
    EBSCOHOST_MAX_SEARCHES   searches allowed per run (default 120)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import quote, urlsplit

from .. import browser
from ..browser import BiDi, BrowserError
from ..query import Node, to_ebsco
from .base import Backend, BackendError, Record, SearchOptions, SearchResult, chunks, split_known

PAGE_SIZE = 10  # records on the first results page
LOGIN_WAIT = 15 * 60  # longest wait for you to do something in the window
REMIND_EVERY = 60
LOAD_WAIT = 45
MIN_DELAY = 3.0
DEFAULT_URL = "https://research.ebsco.com/c/q67rbq/search"
_BASE = re.compile(r"^(https://[^/]+/c/[^/?#]+)")

# EBSCO's (Osano) cookie banner is showing, i.e. no cookie choice is stored yet. Until it is answered,
# results pages can stay blank. The choice is the user's, so the tool waits for it rather than clicking.
CONSENT_PENDING_JS = (
    "(() => { const d = document.querySelector('.osano-cm-dialog'); "
    "return !!d && !d.classList.contains('osano-cm-dialog--hidden') && d.getBoundingClientRect().height > 0; })()"
)

# Runs in the results page. Expands the collapsed author/subject lists, then reads what is shown.
EXTRACT_JS = r"""
const clean = s => (s || '').replace(/⁠/g, '').replace(/\s+/g, ' ').trim();
const one = (root, auto) => root.querySelector(`[data-auto="${auto}"]`);
const listed = root => root ? [...root.querySelectorAll('[data-auto=delimited-description-link-list-item]')].map(e => {
  const copy = e.cloneNode(true);
  copy.querySelectorAll('[data-auto=delimiter]').forEach(d => d.remove());
  return clean(copy.textContent);
}).filter(Boolean) : [];
const count = one(document, 'result-count');
const state = {
  count: count ? clean(count.querySelector('span') ? count.querySelector('span').textContent : count.textContent) : null,
  noResults: !!one(document, 'no-results'),
  hasSearchForm: !!one(document, 'search-form'),
  databases: clean((one(document, 'selected-databases') || {}).textContent).replace(/^\|?\s*Searching:\s*/, ''),
  filters: [...document.querySelectorAll('[data-auto=pill-label]')].map(e => clean(e.textContent)).filter(Boolean),
  title: document.title, url: location.href, items: [],
  consentPending: CONSENT_PENDING,
};
if (!count) {
  // Leave out the cookie-consent banner, which shows over any page, so the text says what the page itself holds.
  const banner = document.querySelector('.osano-cm-window');
  let text = clean(document.body.innerText);
  if (banner && banner.innerText) text = clean(text.replace(clean(banner.innerText), ''));
  state.text = text.slice(0, 300);
  state.blank = !document.title.trim() && !text;
  return state;
}
const toggles = [...document.querySelectorAll('[data-auto=search-result-item] [data-auto=delimited-list-toggle][aria-expanded=false]')];
toggles.forEach(b => b.click());
if (toggles.length) await new Promise(r => setTimeout(r, 500));
for (const item of document.querySelectorAll('[data-auto=search-result-item]')) {
  const meta = one(item, 'result-item-metadata');
  const authors = [];
  if (meta) for (const child of meta.children) {
    if (child.tagName === 'DT' && child.getAttribute('data-auto') !== 'result-item-metadata-heading--contributors') break;
    if (child.tagName === 'DD') authors.push(...listed(child));
  }
  const published = one(item, 'result-item-metadata-content--published');
  const link = one(item, 'result-item-title__link');
  const types = [...item.querySelectorAll('[data-auto=article-type] li:not([data-auto=peer-reviewed])')].map(e => clean(e.textContent));
  state.items.push({
    id: item.getAttribute('aria-labelledby') || '',
    title: clean((one(item, 'result-item-title') || {}).textContent),
    href: link ? link.href : '',
    authors,
    published: published ? [...published.querySelectorAll('span')].map(e => clean(e.textContent)) : [],
    publishedText: clean((published || {}).textContent),
    database: clean((one(item, 'result-item-metadata-content--database') || {}).textContent),
    abstract: clean((one(item, 'abstract-content') || {}).textContent),
    subjects: listed(one(item, 'subjects-content')),
    types,
    peerReviewed: !!one(item, 'peer-reviewed'),
    citedBy: clean((one(item, 'citation-insights-cited-by') || {}).textContent),
  });
}
return state;
""".replace("CONSENT_PENDING", CONSENT_PENDING_JS)


# A banner drawn over whatever the window shows when a search lands somewhere unexpected. The user
# clears the problem by hand (log in, answer a challenge, ...) and clicks Continue; nothing is clicked for them.
ALERT_JS = r"""
const text = ALERT_TEXT;
let bar = document.getElementById('psychsearch-alert');
if (!bar) {
  bar = document.createElement('div');
  bar.id = 'psychsearch-alert';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#b45309;color:#fff;' +
    'font:15px/1.4 system-ui,sans-serif;padding:12px 16px;display:flex;gap:16px;align-items:center;box-shadow:0 2px 8px #0006';
  const msg = document.createElement('div');
  msg.style.flex = '1';
  const btn = document.createElement('button');
  btn.textContent = 'Fixed it, continue';
  btn.style.cssText = 'font:inherit;padding:6px 12px;cursor:pointer';
  btn.onclick = () => { window.__psychsearchContinue = true; bar.remove(); };
  bar.append(msg, btn);
  document.documentElement.append(bar);
}
bar.firstChild.textContent = text;
const clicked = !!window.__psychsearchContinue;
window.__psychsearchContinue = false;
return clicked;
"""

ALERT_TRIES = 2  # alerts per search before giving up for the run

WINDOW = "automation Firefox window"
CONSENT_NEED = "Answer EBSCO's cookie banner, any option (results pages can stay blank until it is answered)"


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _is_ebsco(url: str) -> bool:
    return "ebsco" in (urlsplit(url).hostname or "") and bool(_BASE.match(url))


class EbscoHost(Backend):
    name = "ebscohost"
    label = "PsycInfo (EBSCOhost)"
    coverage = "APA PsycInfo itself, read from the EBSCOhost website in a Firefox window you log in to (see README)"

    def __init__(self, http):
        super().__init__(http)
        env = os.environ
        self.start_url = env.get("EBSCOHOST_URL") or DEFAULT_URL
        self.db = env.get("EBSCOHOST_DB", "psyh")
        self.delay = max(MIN_DELAY, float(env.get("EBSCOHOST_DELAY", "5")))
        self.max_searches = int(env.get("EBSCOHOST_MAX_SEARCHES", "120"))
        self.searches = 0
        self._page: BiDi | None = None
        self._context = ""
        self._base = ""
        self._last = 0.0
        self._halted = ""

    # ------------------------------------------------------------ window

    def check(self) -> None:
        """Make sure the window is open and logged in; this is where the user is asked to log in."""
        try:
            if not browser.is_running():
                _say("Opening a separate Firefox window...")
                browser.launch(self._start())
                deadline = time.time() + 60
                while not browser.is_running():
                    if time.time() > deadline:
                        raise BackendError("Firefox did not open its control port within a minute")
                    time.sleep(1)
                time.sleep(3)
            self._page = BiDi()
            self._wait_for_login()
        except BrowserError as exc:
            raise BackendError(f"EBSCOhost: {exc}") from None

    def _start(self) -> str:
        base = _BASE.match(self.start_url)
        if not base:
            raise BackendError(
                "the ebscohost backend needs EBSCOHOST_URL (environment or .env) to open the window: "
                "paste any address from your EBSCO search session, e.g. https://research.ebsco.com/c/abc123/search"
            )
        return f"{base.group(1)}/search?db={self.db}"

    def _wait_for_user(self, needs, poll: float) -> str:
        """Wait while `needs()` names something you have to do in the window, saying so when it starts,
        reminding every REMIND_EVERY seconds, and saying when it is over. Returns "" once nothing is
        needed, or what was still needed when LOGIN_WAIT ran out."""
        deadline = time.monotonic() + LOGIN_WAIT
        told, reminded = "", 0.0
        while need := needs():
            now = time.monotonic()
            if now > deadline:
                return need
            if need != told:
                _say(f"\a>> Waiting for you: {need} in the {WINDOW}.")
                self._bring_to_front()
                told, reminded = need, now
            elif now - reminded >= REMIND_EVERY:
                _say(f">> Still waiting for you to {need[0].lower()}{need[1:]} in the {WINDOW} "
                     f"({_minutes(deadline - now)} left).")
                reminded = now
            time.sleep(poll)
        if told:
            _say("   Done; continuing.")
        return ""

    def _bring_to_front(self) -> None:
        try:
            self._page.bring_to_front(self._context or None)
        except BrowserError:
            pass  # only a convenience

    def _wait_for_login(self) -> None:
        """Wait for a logged-in EBSCO search page with no unanswered cookie banner."""
        ready_js = f"return {{form: !!document.querySelector('[data-auto=search-form]'), consent: {CONSENT_PENDING_JS}}}"

        def needs() -> str:
            need = "Log in to EBSCO (the searches start once its search page shows)"
            for tab in reversed(self._page.tabs()):
                if not _is_ebsco(tab["url"]):
                    continue
                seen = self._page.evaluate(tab["context"], ready_js) or {}
                if seen.get("form") and not seen.get("consent"):
                    self._context = tab["context"]
                    self._base = _BASE.match(tab["url"]).group(1)
                    return ""
                if seen.get("form"):
                    need = CONSENT_NEED
            return need

        if missing := self._wait_for_user(needs, poll=3):
            raise BackendError(f"EBSCOhost: gave up after {_minutes(LOGIN_WAIT)}. Still needed: {missing}.")

    def _wait_for_consent(self) -> None:
        """Mid-run: wait for the cookie banner to be answered; the caller then reloads the page."""
        try:
            self._wait_for_user(lambda: CONSENT_NEED if self._page.evaluate(self._context, f"return {CONSENT_PENDING_JS}") else "", poll=2)
        except BrowserError as exc:
            self._halted = f"EBSCOhost: {exc}"
            raise BackendError(self._halted) from None

    def _wait_for_clearance(self, seen: str) -> bool:
        """A search landed on something that is not a results page. Say so in the window and wait for the
        user to clear it (log in, answer a challenge) and click Continue, or for EBSCO's search page to
        come back on its own. True when it was cleared, False when LOGIN_WAIT ran out."""
        text = f"psychsearch paused: {seen}. Fix it in this window, then click Continue."
        alert = ALERT_JS.replace("ALERT_TEXT", json.dumps(text))
        need = f"Clear the problem shown at the top of the window ({seen})"

        def needs() -> str:
            try:
                clicked = self._page.evaluate(self._context, alert)
                state = self._page.evaluate(self._context, EXTRACT_JS)
            except BrowserError:
                return need  # mid-navigation (logging in, say); look again in a moment
            if clicked or (_is_ebsco(state.get("url", "")) and (state.get("count") or state.get("hasSearchForm"))
                           and not state.get("consentPending")):
                return ""
            return need

        try:
            return not self._wait_for_user(needs, poll=2)
        except BrowserError as exc:
            self._halted = f"EBSCOhost: {exc}"
            raise BackendError(self._halted) from None

    def close(self) -> None:
        if self._page is not None:
            self._page.close()
            self._page = None

    # ------------------------------------------------------------ search

    def render(self, node: Node, opts: SearchOptions) -> str:
        return to_ebsco(node, opts.field)

    def _query_string(self, term: str, opts: SearchOptions) -> str:
        parts = [f"q={quote(term, safe='')}", f"db={self.db}", "searchMode=boolean", "expanders=None", "autocorrect=n"]
        if opts.years:
            parts.append("limiters=" + quote(f"DT1:{opts.years[0]}-01-01/{opts.years[1]}-12-31", safe=""))
        if opts.sort == "date":
            parts.append("sort=" + quote("date:desc", safe=""))
        return "&".join(parts)

    def _cache_file(self, query_string: str):
        if self.http.cache_dir is None:
            return None
        digest = hashlib.sha256(f"{self._base}?{query_string}".encode()).hexdigest()
        return self.http.cache_dir / f"ebscohost-{digest}.json"

    def _load(self, term: str, opts: SearchOptions) -> dict:
        """Load one results page (or reuse a cached read of it) and return what it shows."""
        query_string = self._query_string(term, opts)
        cache = self._cache_file(query_string)
        if cache is not None and cache.exists() and time.time() - cache.stat().st_mtime < self.http.ttl:
            self.http.cache_hits += 1
            return json.loads(cache.read_text(encoding="utf-8"))
        if self._halted:
            raise BackendError(self._halted)
        if self._page is None:
            raise BackendError("EBSCOhost: the browser window is not connected")

        url = f"{self._base}/search/results?{query_string}"
        state = self._visit(url)
        settled = state.get("count") or state.get("noResults")
        if not settled and _is_ebsco(state.get("url", "")) and (state.get("consentPending") or state.get("blank")):
            # An unanswered cookie banner, or an empty page (no title, no text) after a load that never
            # finished, is neither a login nor a block: once the banner is answered, load the page once more,
            # paced and counted like any search.
            if state.get("consentPending"):
                self._wait_for_consent()
            state = self._visit(url)

        alerts = 0
        while not state.get("count") and not state.get("noResults"):
            # Not a results page: logged out, blocked, or challenged. Ask the user to clear it in the window.
            where = urlsplit(state.get("url", "")).hostname or "unknown page"
            if state.get("consentPending"):
                seen = f"the cookie banner in the {WINDOW} is still unanswered"
            elif state.get("blank"):
                seen = f"the results page on {where} stayed blank for {LOAD_WAIT} seconds, twice"
            else:
                seen = f"now on {where}: {state.get('title', '')!r} {state.get('text', '')[:120]!r}"
            if alerts >= ALERT_TRIES or not self._wait_for_clearance(seen):
                self._halted = (
                    f"EBSCOhost: no results page appeared ({seen}). Stopping all further searches; check the {WINDOW}, "
                    "log in again if needed, and rerun (finished searches are cached)."
                )
                raise BackendError(self._halted)
            alerts += 1
            state = self._visit(url)  # paced and counted like any search
        self._base = _BASE.match(state["url"]).group(1)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(state), encoding="utf-8")
        return state

    def _visit(self, url: str) -> dict:
        """Load `url` after the pause between searches and read the page once it settles."""
        if self.searches >= self.max_searches:
            self._halted = (
                f"EBSCOhost: stopped at the limit of {self.max_searches} searches per run (EBSCOHOST_MAX_SEARCHES). "
                "Finished searches are cached; rerun to continue."
            )
            raise BackendError(self._halted)
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.searches += 1
        self.http.requests_made += 1
        try:
            self._page.navigate(self._context, url)
            started, state = time.monotonic(), {}
            while time.monotonic() - started < LOAD_WAIT:
                state = self._page.evaluate(self._context, EXTRACT_JS)
                if state.get("count") or state.get("noResults") or not _is_ebsco(state.get("url", "")):
                    break
                time.sleep(0.7)
        except BrowserError as exc:
            self._halted = f"EBSCOhost: {exc}"
            raise BackendError(self._halted) from None
        finally:
            self._last = time.monotonic()
        return state

    @staticmethod
    def _count(state: dict) -> int:
        if state.get("noResults"):
            return 0
        numbers = re.findall(r"[\d,.   ]*\d", state["count"])  # "1-10 of 7,061 total results"
        return int(re.sub(r"\D", "", numbers[-1]))

    def _search(self, node: Node, opts: SearchOptions, result: SearchResult) -> None:
        result.query_sent = self.render(node, opts)
        state = self._load(result.query_sent, opts)
        result.count = self._count(state)
        result.records = [_parse(item) for item in state["items"][: opts.sample]]
        if state.get("databases"):
            result.interpretation = f"Searching: {state['databases']}" + (f"; filters: {', '.join(state['filters'])}" if state.get("filters") else "")
        if opts.sample > PAGE_SIZE and result.count > PAGE_SIZE:
            result.warnings.append(f"only the first results page is read ({PAGE_SIZE} records)")

    # ------------------------------------------------------- known items

    def resolve_known(self, ids: list[str]) -> dict[str, str]:
        dois, pmids = split_known(ids)
        clauses = [f'DI "{d}"' for d in dois] + [f"PM {p}" for p in pmids]
        found = {}
        for chunk in chunks(clauses, PAGE_SIZE):  # one page shows 10 records
            for item in self._load(" OR ".join(chunk), SearchOptions())["items"]:
                record = _parse(item)
                found[record.id] = record.title
        return found

    def find_known(self, node: Node, opts: SearchOptions, known_ids: list[str]) -> set[str]:
        found = set()
        for chunk in chunks(known_ids, PAGE_SIZE):
            # Quoted: an unquoted accession number is matched loosely and pulls in unrelated records.
            term = f"( {self.render(node, opts)} ) AND ( {' OR '.join(f'AN "{an}"' for an in chunk)} )"
            found.update(_parse(item).id for item in self._load(term, opts)["items"])
        return found & set(known_ids)


def _parse(item: dict) -> Record:
    accession, _, _db = item.get("id", "").rpartition("-")  # "2010-05835-004-psyh"
    published = item.get("published") or []
    year = re.search(r"\b(1[89]|20)\d{2}\b", " ".join(published[1:]) or item.get("publishedText", ""))
    cited = re.search(r"\d+", item.get("citedBy", ""))
    types = [t.strip() for entry in item.get("types", []) for t in entry.split(",") if t.strip()]
    return Record(
        id=accession or item.get("id", ""),
        title=item.get("title", ""),
        authors=item.get("authors", []),
        year=int(year.group()) if year else None,
        journal=published[0] if published else "",
        url=item.get("href", ""),
        abstract=item.get("abstract", ""),
        pub_types=types,
        subjects=item.get("subjects", []),
        cited_by=int(cited.group()) if cited else None,
        extra={"database": item.get("database", ""), "peer_reviewed": item.get("peerReviewed", False),
               "published": item.get("publishedText", "")},
    )
