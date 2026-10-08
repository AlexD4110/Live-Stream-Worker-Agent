"""Web search through Tavily, for background that is not in our data.

Rules built into this module:
  - Results are outside information. They are labeled external and never mixed with the computed numbers.
  - Nothing private goes out: queries containing account ids, emails, phone numbers or long digit strings are refused.
  - Web text is untrusted: results that try to give the AI model orders are dropped, snippets are short, links removed.
  - The query is whatever the caller said, so it is never logged, and the API key never appears in an error.
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

URL = "https://api.tavily.com/search"
MAX_QUERY_CHARS = 150
MAX_SNIPPET_CHARS = 280
MAX_RESULTS = 3
REQUEST_RESULTS = 5            # ask for a few extra, since some may be dropped

_PRIVATE = re.compile(r"\bg\d{5}\b|\bc\d{3}\b|\bd\d{5}\b|\bdx\d\b|@", re.IGNORECASE)
_ORDERS = re.compile(r"ignore (?:all |any |the |your )?(?:previous|prior|above|earlier)|system prompt|you are now|"
                     r"disregard (?:all|the|any|your)|reveal your|new instructions", re.IGNORECASE)
_LINK = re.compile(r"https?://\S+|www\.\S+")


class WebSearchError(Exception):
    """A problem described in words that are safe to say aloud and to log."""


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def sanitize_query(query):
    q = re.sub(r"\s+", " ", query or "").strip()
    if len(q) < 3:
        raise WebSearchError("What would you like me to search for?")
    if _PRIVATE.search(q) or re.search(r"\d{7,}", re.sub(r"[\s\-().]", "", q)):
        raise WebSearchError("I won't search the web with account IDs, emails or phone numbers in the question.")
    if len(q) > MAX_QUERY_CHARS:
        q = q[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return q


def _post(url, api_key, body, timeout=8):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                                          "User-Agent": "gifting-pulse/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HttpError(e.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OSError(type(e).__name__) from None             # the type only: never the text, which could hold the key


def _clean(text):
    return re.sub(r"\s+", " ", _LINK.sub("", text or "")).strip()


def _shorten(text):
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[:MAX_SNIPPET_CHARS].rsplit(" ", 1)[0].rstrip(",;:")


def _domain(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


class Tavily:
    def __init__(self, api_key, post=_post, depth="basic"):
        self._key, self._post, self._depth = api_key, post, depth

    def search(self, query):
        """[{source, title, snippet}], at most three, from different sites. Raises WebSearchError."""
        q = sanitize_query(query)
        body = {"query": q, "search_depth": self._depth, "max_results": REQUEST_RESULTS, "include_answer": False,
                "include_raw_content": False, "include_images": False}
        try:
            data = self._post(URL, self._key, body, 8)
        except HttpError as e:
            raise WebSearchError({401: "Tavily rejected the API key.", 429: "The web search limit was reached.",
                                  432: "The web search limit was reached.", 433: "The web search limit was reached."}
                                 .get(e.status, "Web search isn't available right now.")) from None
        except (OSError, TimeoutError):
            raise WebSearchError("Web search isn't available right now.") from None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise WebSearchError("Web search gave an answer I couldn't read.")
        out, seen = [], set()
        for item in data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            title, content = _clean(str(item.get("title") or "")), _clean(str(item.get("content") or ""))
            source = _domain(item["url"])
            if not source or not content or source in seen or _ORDERS.search(title + " " + content):
                continue
            seen.add(source)
            out.append({"source": source, "title": title[:100], "snippet": _shorten(content)})
            if len(out) == MAX_RESULTS:
                break
        return out
