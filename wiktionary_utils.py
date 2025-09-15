"""Utilities for querying ru.wiktionary.org."""

import json
import logging
from typing import Optional, Tuple

from urllib import parse, request
from urllib.error import HTTPError, URLError

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
