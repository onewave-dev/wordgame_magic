"""Utilities for querying ru.wiktionary.org."""

import json
import logging
from typing import Optional, Tuple

from urllib import parse, request
from urllib.error import HTTPError, URLError

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def lookup_wiktionary(word: str) -> Optional[Tuple[bool, bool, str]]:
    """Return word info from ru.wiktionary.org using the MediaWiki API.

    The function requests the API with ``action=query&prop=extracts`` and
    attempts to obtain the first line of the ``extract`` field.  The network
    call is performed with ``urllib`` so it can be easily mocked in tests.
    """

    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": word,
        "format": "json",
    }
    query = parse.urlencode(params)
    url = f"https://ru.wiktionary.org/w/api.php?{query}"
    req = request.Request(url, headers={"User-Agent": "wordgame-bot/1.0"})
    try:
        with request.urlopen(req) as resp:  # pragma: no cover - network
            data = json.loads(resp.read())
    except HTTPError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary HTTP error: %s", e)
        return None
    except URLError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary URL error: %s", e.reason)
        return None
    except json.JSONDecodeError as e:  # pragma: no cover - malformed JSON
        logger.exception("Wiktionary JSON error: %s", e)
        return None

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        logger.info("No pages field in response for word '%s'", word)
        return None
    page = next(iter(pages.values()))
    if "missing" in page:
        return (False, False, "")

    extract = page.get("extract")
    if not extract:
        logger.info("No extract found for word '%s'", word)
        return None

    definition = extract.splitlines()[0].strip()
    return (True, True, definition)


def lookup_wiktionary_meaning(word: str) -> Optional[str]:
    """Return the first definition paragraph from the Wiktionary article.

    The function downloads the page ``/wiki/{word}``, locates the section with
    ``id="Значение"`` and returns the text of the first ``<li>`` or ``<p>``
    that appears after the heading.  Text after the ``◆`` symbol is trimmed as
    it usually contains additional usage notes that are not part of the
    definition shown in chat.  ``None`` is returned when the section is not
    present or no matching elements are found.
    """

    url = f"https://ru.wiktionary.org/wiki/{parse.quote(word)}"
    req = request.Request(url, headers={"User-Agent": "wordgame-bot/1.0"})
    try:
        with request.urlopen(req) as resp:  # pragma: no cover - network
            html = resp.read()
    except HTTPError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary HTTP error: %s", e)
        return None
    except URLError as e:  # pragma: no cover - network errors
        logger.exception("Wiktionary URL error: %s", e.reason)
        return None

    soup = BeautifulSoup(html, "html.parser")
    headline = soup.find(id="Значение")
    if headline is None:
        return None

    heading = headline.find_parent(["h2", "h3", "h4"]) or headline
    text: Optional[str] = None
    for sibling in heading.next_siblings:
        if getattr(sibling, "name", None) is None:
            continue
        if sibling.name == "p":
            text = sibling.get_text(" ", strip=True)
            break
        if sibling.name in {"ol", "ul"}:
            first_item = sibling.find("li")
            if first_item is not None:
                text = first_item.get_text(" ", strip=True)
                break
        if sibling.name and sibling.name.startswith("h"):
            # Stop once a new section begins.
            break

    if not text:
        return None

    cleaned = " ".join(text.split())
    if "◆" in cleaned:
        cleaned = cleaned.split("◆", 1)[0].rstrip()

    return cleaned or None
