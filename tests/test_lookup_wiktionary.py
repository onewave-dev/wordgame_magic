import io
import json
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

import wiktionary_utils


class DummyResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def make_json(extract: str) -> bytes:
    data = {"query": {"pages": {"1": {"extract": extract}}}}
    return json.dumps(data).encode("utf-8")


def test_lookup_wiktionary_iny():
    payload = make_json("женское начало в китайской философии\nдругое")

    def fake_urlopen(url):
        return DummyResponse(payload)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        exists, is_noun, definition = wiktionary_utils.lookup_wiktionary("инь")

    assert exists is True
    assert is_noun is True
    assert definition == "женское начало в китайской философии"


def test_lookup_wiktionary_missing():
    data = {"query": {"pages": {"-1": {"ns": 0, "title": "foo", "missing": ""}}}}
    payload = json.dumps(data).encode("utf-8")

    def fake_urlopen(url):
        return DummyResponse(payload)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        result = wiktionary_utils.lookup_wiktionary("foo")

    assert result == (False, False, "")


def test_lookup_wiktionary_first_line():
    payload = make_json(
        "кровельный материал из картона, пропитанного дегтем или битумом\nВторое"
    )

    def fake_urlopen(url):
        return DummyResponse(payload)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        exists, is_noun, definition = wiktionary_utils.lookup_wiktionary("толь")

    assert exists is True
    assert is_noun is True
    assert (
        definition
        == "кровельный материал из картона, пропитанного дегтем или битумом"
    )


def test_lookup_wiktionary_meaning_trim():
    html = """
    <html>
      <body>
        <h2><span id=\"Русский\">Русский</span></h2>
        <h3><span id=\"Значение\">Значение</span></h3>
        <p>Первое значение ◆ дополнительная помета</p>
      </body>
    </html>
    """.encode("utf-8")

    def fake_urlopen(url):
        return DummyResponse(html)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        result = wiktionary_utils.lookup_wiktionary_meaning("пример")

    assert result == "Первое значение"


H5_HEADING_HTML = """
<html>
  <body>
    <h2><span id=\"Русский\">Русский</span></h2>
    <h4>Другой раздел</h4>
    <h5><span id=\"Значение\">Значение</span></h5>
    <ol>
      <li>Толкование из углубленного заголовка</li>
    </ol>
  </body>
</html>
""".encode("utf-8")


def test_lookup_wiktionary_meaning_deep_heading():
    def fake_urlopen(url):
        return DummyResponse(H5_HEADING_HTML)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        result = wiktionary_utils.lookup_wiktionary_meaning("трен")

    assert result == "Толкование из углубленного заголовка"


RUSSIAN_SECTION_NESTED_HTML = """
<html>
  <body>
    <h2><span id=\"Русский\">Русский</span></h2>
    <div class=\"mw-parser-output\">
      <div>
        <h3><span id=\"Значение_2\">Значение</span></h3>
        <div class=\"t-section\">
          <div>
            <p>Определение внутри вложенного контейнера ◆ с пометой</p>
          </div>
        </div>
      </div>
    </div>
    <h2><span id=\"Английский\">Английский</span></h2>
  </body>
</html>
""".encode("utf-8")


def test_lookup_wiktionary_meaning_russian_section_nested():
    def fake_urlopen(url):
        return DummyResponse(RUSSIAN_SECTION_NESTED_HTML)

    with patch("wiktionary_utils.request.urlopen", fake_urlopen):
        result = wiktionary_utils.lookup_wiktionary_meaning("пример")

    assert result == "Определение внутри вложенного контейнера"

