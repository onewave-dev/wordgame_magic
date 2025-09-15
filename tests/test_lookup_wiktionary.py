import io
import json
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

import llm_utils


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

    with patch("llm_utils.request.urlopen", fake_urlopen):
        exists, is_noun, definition = llm_utils.lookup_wiktionary("инь")

    assert exists is True
    assert is_noun is True
    assert definition == "женское начало в китайской философии"


def test_lookup_wiktionary_missing():
    data = {"query": {"pages": {"-1": {"ns": 0, "title": "foo", "missing": ""}}}}
    payload = json.dumps(data).encode("utf-8")

    def fake_urlopen(url):
        return DummyResponse(payload)

    with patch("llm_utils.request.urlopen", fake_urlopen):
        result = llm_utils.lookup_wiktionary("foo")

    assert result == (False, False, "")


def test_lookup_wiktionary_first_line():
    payload = make_json(
        "кровельный материал из картона, пропитанного дегтем или битумом\nВторое"
    )

    def fake_urlopen(url):
        return DummyResponse(payload)

    with patch("llm_utils.request.urlopen", fake_urlopen):
        exists, is_noun, definition = llm_utils.lookup_wiktionary("толь")

    assert exists is True
    assert is_noun is True
    assert (
        definition
        == "кровельный материал из картона, пропитанного дегтем или битумом"
    )

