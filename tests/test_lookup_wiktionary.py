import io
import logging
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup

import llm_utils


class DummyResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def load_html(name: str) -> bytes:
    path = Path(__file__).parent / 'data' / f'{name}.html'
    return path.read_bytes()


def test_lookup_wiktionary_iny():
    html = load_html('iny')

    def fake_urlopen(url):
        return DummyResponse(html)

    with patch('llm_utils.request.urlopen', fake_urlopen):
        exists, is_noun, definition = llm_utils.lookup_wiktionary('инь')

    assert exists is True
    assert is_noun is True
    assert definition == 'женское начало в китайской философии'


def test_lookup_wiktionary_fallback_no_span(caplog):
    html = load_html('tol')
    soup = BeautifulSoup(html, 'html.parser')
    for span in soup.select('span.mw-headline[id^="Существительное"]'):
        span.decompose()
    html_no_span = str(soup).encode('utf-8')

    def fake_urlopen(url):
        return DummyResponse(html_no_span)

    with patch('llm_utils.request.urlopen', fake_urlopen):
        with caplog.at_level(logging.INFO):
            exists, is_noun, definition = llm_utils.lookup_wiktionary('толь')

    assert exists is True
    assert is_noun is True
    assert definition == (
        'кровельный материал из картона, пропитанного дегтем или битумом'
    )
    assert any('branch: no_span' in record.message for record in caplog.records)
