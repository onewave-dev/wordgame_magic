Code to import  validation dictionary  (colab version)

Word Validator — критерии отбора

Инструмент проверяет, является ли слово валидным русским нарицательным существительным. Слово считается валидным ✅, если выполняются все три условия:

Морфология (pymorphy2)

есть разбор как NOUN (нарицательное существительное, не собственное, не архаизм);

и хотя бы один из разборов соответствует:

именительный падеж, единственное число (nomn + sing), или

pluralia tantum — существительное, которое употребляется только во множественном числе в именительном (nomn + plur, без формы единственного числа);

конкурирующие разборы (ADJF, NUMR и др.) с равной или большей вероятностью (score) отклоняют слово.

Частотность (wordfreq)

используется Zipf-частота формы слова (не леммы);

слово проходит, если Zipf ≥ 2.5.

это отсеивает архаизмы и редкие формы.

Wiktionary (ru.wiktionary.org)

страница существует и имеет раздел для русского языка (== Русский == или {{-ru-}}).
Примеры

✅ валидны:

«стол» (nomn+sing, Zipf=4.78, есть в Wiktionary)

«сота» (nomn+sing, Zipf=2.53, есть в Wiktionary)

«наст» (nomn+sing, Zipf=3.51, есть в Wiktionary)

❌ невалидны:

«сто» (лучший разбор NUMR → отклонено)

«красный» (лучший разбор ADJF → отклонено)

«зренье» (устаревшее, Zipf формы = 1.94 < 2.5)

«нство» (редкое, Zipf формы = 1.05 < 2.5)

«вейс» (морфология и частота проходят, но нет страницы на Wiktionary)

A. КОД ДЛЯ ТЕСТА НА ВЫБОРКЕ СЛОВ

#cell 1

!python -V
!pip -q install pymorphy2==0.9.1 pymorphy2-dicts-ru==2.4.417127.4579844 wordfreq==3.1.1

#cell 2

# =======================
# Импорты и совместимость
# =======================
import re
import inspect
from collections import namedtuple
import requests

# Совместимость с Python 3.11+: pymorphy2 иногда ожидает inspect.getargspec
if not hasattr(inspect, 'getargspec'):
    def _getargspec(func):
        fs = inspect.getfullargspec(func)
        ArgSpec = namedtuple('ArgSpec', 'args varargs keywords defaults')
        return ArgSpec(fs.args, fs.varargs, fs.varkw, fs.defaults)
    inspect.getargspec = _getargspec

from pymorphy2 import MorphAnalyzer
from wordfreq import zipf_frequency

morph = MorphAnalyzer()

ZIPF_FREQ_THRESHOLD = 2.5
PROPER_GRAMMEMES = {'Name', 'Surn', 'Patr', 'Geox', 'Orgn', 'Trad'}
BLOCK_GRAMMEMES  = {'Arch'}     # исключаем явные архаизмы по метке
SHOW_DEBUG = True              # True — печать всех разборов проблемных слов
AMBIGUITY_DELTA = 0.0           # «строго не ниже» — конкурирующий разбор блокирует

def clean_token(s: str) -> str:
    return re.sub(r"[^а-яё-]", "", s.strip().lower()).strip("-")

def is_common_noun(p) -> bool:
    if p.tag.POS != 'NOUN':
        return False
    if any(g in p.tag for g in PROPER_GRAMMEMES):
        return False
    if any(g in p.tag for g in BLOCK_GRAMMEMES):
        return False
    return True

def has_singular_in_lexeme(p) -> bool:
    try:
        for f in p.lexeme:
            if 'sing' in f.tag:
                return True
    except Exception:
        return True
    return False

def is_pluralia_tantum_nom_plur(p) -> bool:
    t = p.tag
    if 'nomn' in t and 'plur' in t:
        if 'Pltm' in t:
            return True
        return not has_singular_in_lexeme(p)
    return False

def is_nomn_sing(p) -> bool:
    t = p.tag
    return ('nomn' in t) and ('sing' in t)

# Wiktionary: прежний критерий (есть русская секция)
def wiktionary_ru_has_russian_entry(word: str, timeout=8.0) -> bool:
    API = "https://ru.wiktionary.org/w/api.php"
    headers = {"User-Agent": "WordValidator/1.0 (contact: example@example.com)"}
    q = {
        "action": "query", "format": "json", "redirects": 1, "titles": word,
        "prop": "revisions", "rvprop": "content", "rvslots": "main", "formatversion": 2
    }
    try:
        r = requests.get(API, params=q, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return False
        content = pages[0].get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("content", "")
        if not isinstance(content, str):
            return False
        return bool(
            re.search(r"==\s*Русский\s*==", content, re.IGNORECASE) or
            re.search(r"\{\{\s*-ru-\s*\}\}", content, re.IGNORECASE)
        )
    except Exception:
        return False

def validate_word(word: str, zipf_threshold: float = ZIPF_FREQ_THRESHOLD):
    original = word
    w = clean_token(word)
    details = {
        'input': original,
        'normalized': w,
        'pymorphy_check': 'не пройдена',
        'freq_check': 'не пройдена',
        'wiktionary_check': 'не пройдена',
        'zipf_form': 0.0,
        'zipf_lemma': 0.0,
        'final': 'невалидно ❌'
    }
    if not w:
        return details

    parses = morph.parse(w)
    if not parses:
        return details

    # 1) Все нарицательные существительные (любой падеж/число)
    noun_parses = [p for p in parses if is_common_noun(p)]
    if not noun_parses:
        return details

    # 2) Подходящие по нашим правилам кандидаты:
    #    - nomn + sing
    #    - pluralia tantum: nomn + plur и нет sing в лексеме
    valid_noun_candidates = []
    for p in noun_parses:
        if is_nomn_sing(p):
            valid_noun_candidates.append(('nomn + sing', p))
        elif is_pluralia_tantum_nom_plur(p):
            valid_noun_candidates.append(('pluralia tantum: nomn + plur', p))

    if not valid_noun_candidates:
        return details

    # Берём лучшего из подходящих по score
    accepted_by, noun_best = max(valid_noun_candidates, key=lambda t: t[1].score)

    # 3) Анти-омонимный фильтр:
    #    если есть конкурент ADJF/NUMR/PRTF/PRTS с score >= score выбранного NOUN — отклоняем
    competitor_pos = {'ADJF', 'NUMR', 'PRTF', 'PRTS'}
    comp_score = max([p.score for p in parses if p.tag.POS in competitor_pos] or [0.0])
    if comp_score >= noun_best.score:
        return details

    details['pymorphy_check'] = f'пройдена ({accepted_by})'

    # 4) Частотность по форме (лемму показываем для информации)
    form_zipf  = zipf_frequency(w, 'ru', wordlist='best') or 0.0
    lemma_zipf = zipf_frequency(noun_best.normal_form, 'ru', wordlist='best') or 0.0
    details['zipf_form']  = round(form_zipf, 3)
    details['zipf_lemma'] = round(lemma_zipf, 3)

    if form_zipf >= zipf_threshold:
        details['freq_check'] = f'пройдена (Zipf формы = {form_zipf:.3f} ≥ {zipf_threshold})'
    else:
        details['freq_check'] = f'не пройдена (Zipf формы = {form_zipf:.3f} < {zipf_threshold})'
        return details

    # 5) Wiktionary (как раньше: наличие русской секции)
    if wiktionary_ru_has_russian_entry(w):
        details['wiktionary_check'] = 'пройдена ✅'
        details['final'] = 'валидно ✅'
    else:
        details['wiktionary_check'] = 'не пройдена ❌'

    return details

# =======================
# Автотест на заданном списке
# =======================
test_words = ["стол", "столмв", "чат", "наст", "сота", "вейс", "ство", "нство", "зренье", "сто", "красный", "тысяча", "мненье"]
print(f"Порог Zipf (по форме) = {ZIPF_FREQ_THRESHOLD}\n")
for w in test_words:
    info = validate_word(w, ZIPF_FREQ_THRESHOLD)
    print(f"ИТОГ: {info['final']}")
    print(f"  ▸ Ввод: {info['input']}")
    print(f"  ▸ Нормализовано: {info['normalized']}")
    print(f"  ▸ Проверка pymorphy2: {info['pymorphy_check']}")
    print(f"  ▸ Проверка частотности: {info['freq_check']}")
    print(f"  ▸ Проверка Wiktionary (Русский): {info['wiktionary_check']}")
    print(f"  ▸ Zipf(форма): {info['zipf_form']},  Zipf(лемма): {info['zipf_lemma']}\n")


B. КОД ДЛЯ ВЫГРУЗКИ СЛОВАРЯ ВАЛИДАЦИИ

#cell 3 (работает вместе с ячейками выше)


# =======================
# ИТЕРАЦИЯ 1 (ФИНАЛЬНАЯ): кандидаты (pymorphy2 + Zipf) с жёстким тестом позитивов
# =======================
import os, json, re
from tqdm import tqdm
from google.colab import drive

# --- 0) Окружение: ожидаем, что предыдущие ячейки уже объявили эти объекты/константы
try:
    morph
    ZIPF_FREQ_THRESHOLD
    PROPER_GRAMMEMES
    BLOCK_GRAMMEMES
except NameError as e:
    raise RuntimeError(f"Не найдена переменная/объект: {e}. Запустите ячейки с инициализацией morph/настроек раньше.")

# --- 1) Монтаж Google Drive
drive.mount('/content/drive', force_remount=True)

# --- 2) Пути
SAVE_DIR = "/content/drive/MyDrive/word_validation"
os.makedirs(SAVE_DIR, exist_ok=True)
out_stage1 = os.path.join(SAVE_DIR, "valid_stage1.json")

# --- 3) Нормализация (строго как в validate_word)
def clean_token(s: str) -> str:
    return re.sub(r"[^а-яё-]", "", s.strip().lower()).strip("-")

# --- 4) Вспомогательные функции (как в «успешном коде»)
def is_common_noun(p) -> bool:
    return (
        p.tag.POS == 'NOUN'
        and not any(g in p.tag for g in PROPER_GRAMMEMES)
        and not any(g in p.tag for g in BLOCK_GRAMMEMES)
    )

def has_singular_in_lexeme(p) -> bool:
    try:
        for f in p.lexeme:
            if 'sing' in f.tag:
                return True
    except Exception:
        return True
    return False

def is_nomn_sing(p) -> bool:
    t = p.tag
    return ('nomn' in t) and ('sing' in t)

def is_pluralia_tantum_nom_plur(p) -> bool:
    t = p.tag
    if 'nomn' in t and 'plur' in t:
        if 'Pltm' in t:
            return True
        return not has_singular_in_lexeme(p)
    return False

from wordfreq import zipf_frequency

def validate_stage1_only(word: str, zipf_threshold: float = ZIPF_FREQ_THRESHOLD):
    """
    Stage-1 = ровно шаги 1..4 из «успешного кода», но БЕЗ Wiktionary:
      1) среди разборов берём нарицательные NOUN
      2) допускаем nomn+sing ИЛИ pluralia-tantum (nomn+plur без sing / c Pltm)
      3) анти-омонимный фильтр: конкуренты ADJF/NUMR/PRTF/PRTS с score >= NOUN — отклоняем
      4) Zipf(форма) >= порога
    """
    w = clean_token(word)
    details = {
        'input': word,
        'normalized': w,
        'pymorphy_check': 'не пройдена',
        'freq_check': 'не пройдена',
        'zipf_form': 0.0,
        'zipf_lemma': 0.0,
        'stage1_final': False,
        'debug_parses': None,
    }
    if not w:
        return details

    parses = morph.parse(w)
    details['debug_parses'] = [(str(p.tag), p.normal_form, round(p.score, 3)) for p in parses]

    noun_parses = [p for p in parses if is_common_noun(p)]
    if not noun_parses:
        return details

    valid_noun_candidates = []
    for p in noun_parses:
        if is_nomn_sing(p):
            valid_noun_candidates.append(('nomn + sing', p))
        elif is_pluralia_tantum_nom_plur(p):
            valid_noun_candidates.append(('pluralia tantum: nomn + plur', p))
    if not valid_noun_candidates:
        return details

    accepted_by, noun_best = max(valid_noun_candidates, key=lambda t: t[1].score)

    # анти-омонимный фильтр
    competitor_pos = {'ADJF', 'NUMR', 'PRTF', 'PRTS'}
    comp_score = max([p.score for p in parses if p.tag.POS in competitor_pos] or [0.0])
    if comp_score >= noun_best.score:
        return details

    details['pymorphy_check'] = f'пройдена ({accepted_by})'

    # частотность по форме
    form_zipf  = zipf_frequency(w, 'ru', wordlist='best') or 0.0
    lemma_zipf = zipf_frequency(noun_best.normal_form, 'ru', wordlist='best') or 0.0
    details['zipf_form']  = round(form_zipf, 3)
    details['zipf_lemma'] = round(lemma_zipf, 3)

    if form_zipf >= zipf_threshold:
        details['freq_check'] = f'пройдена (Zipf формы = {form_zipf:.3f} ≥ {zipf_threshold})'
        details['stage1_final'] = True

    return details

# --- 5) Итератор словоформ словаря (совместим с разными версиями pymorphy2)
def iter_all_forms():
    for item in morph.dictionary.iter_known_words():
        form = item[0] if isinstance(item, (tuple, list)) else item
        yield str(form)

# --- 6) «Посев» позитивных слов для гарантированного попадания в пул
SEED_WORDS = ["стол", "чат", "наст", "сота"]

# --- 7) Готовим набор all_words: нормализация + дедупликация + SEED
all_words = set()
for form in iter_all_forms():
    w = clean_token(form)          # критично: чистим УЖЕ на этом этапе
    if w:
        all_words.add(w)

seed_added = 0
for sw in SEED_WORDS:
    nw = clean_token(sw)
    if nw and nw not in all_words:
        all_words.add(nw)
        seed_added += 1

print(f"Всего словоформ (после нормализации и дедупликации): {len(all_words)}")
print(f"Добавлено seed-слов, отсутствовавших в словаре: {seed_added}")

# --- 8) ЖЁСТКИЙ ТЕСТ: «позитивы» обязаны попасть в результат Stage-1
print("Проверка позитивов на этапе Stage-1 (должны пройти и попасть в выгрузку):")
positive_ok = True
for w in SEED_WORDS:
    info = validate_stage1_only(w, ZIPF_FREQ_THRESHOLD)
    will_be_in_output = info["stage1_final"] and (clean_token(w) in all_words)
    flag = "OK" if will_be_in_output else "FAIL"
    print(f"  • {w}: {flag} | morph={info['pymorphy_check']} | freq={info['freq_check']} | zipf_form={info['zipf_form']}")
    if not will_be_in_output:
        print("    └─ DBG parses:", info["debug_parses"])
        positive_ok = False

if not positive_ok:
    raise SystemExit("❌ Тест позитивов Stage-1 НЕ пройден — выгрузка остановлена.")

print("✅ Позитивы пройдут и попадут в результат Stage-1. Запускаем полную выгрузку...")

# --- 9) Полный прогон Stage-1
stage1_valid = []
for w in tqdm(sorted(all_words), desc="Stage1: pymorphy2+Zipf"):
    info = validate_stage1_only(w, ZIPF_FREQ_THRESHOLD)
    if info["stage1_final"]:
        stage1_valid.append(info)

# --- 10) Контроль: убедимся, что позитивы действительно в результатах
normalized_out = {rec["normalized"] for rec in stage1_valid}
missing_from_output = [w for w in SEED_WORDS if clean_token(w) not in normalized_out]
if missing_from_output:
    print("❌ Аномалия: после прогона позитивы не оказались в выходном файле:", missing_from_output)
    raise SystemExit("Остановка для диагностики.")

print(f"Этап 1 (без Wiktionary): кандидатов = {len(stage1_valid)}")

# --- 11) Сохранение
with open(out_stage1, "w", encoding="utf-8") as f:
    json.dump(stage1_valid, f, ensure_ascii=False, indent=2)
print("Промежуточный результат сохранён:", out_stage1)


#cell 4 (выгрузска финальных файлов (также "normalized" заменить на "word" для целей кода игры))

# =======================
# ЯЧЕЙКА 2 (устойчивая к рекурсии):
# Тест -> полный прогон (pymorphy2 + Zipf + Wiktionary) -> сохранение на Drive
# Кэш строится поверх локальной _wiktionary_ru_has_russian_entry_raw (без глобалов)
# =======================
import os, json, re, requests
from tqdm import tqdm

SAVE_DIR    = "/content/drive/MyDrive/word_validation"
STAGE1_JSON = os.path.join(SAVE_DIR, "valid_stage1.json")
FINAL_JSONL = os.path.join(SAVE_DIR, "valid_words_full.jsonl")
FINAL_JSON  = os.path.join(SAVE_DIR, "valid_words_full.json")
FINAL_LIST  = os.path.join(SAVE_DIR, "valid_words_only.json")
CACHE_PATH  = os.path.join(SAVE_DIR, "wiktionary_cache.json")

if not os.path.exists(STAGE1_JSON):
    raise FileNotFoundError(f"Не найден {STAGE1_JSON}. Сначала выполните Ячейку 1.")

# validate_word и ZIPF_FREQ_THRESHOLD должны быть объявлены ранее (из ваших ячеек)
try:
    validate_word
    ZIPF_FREQ_THRESHOLD
except NameError as e:
    raise RuntimeError(f"Нет функции/переменной: {e}. Запустите ячейки с валидацией раньше.")

def _norm(s: str) -> str:
    return re.sub(r"[^а-яё-]", "", s.strip().lower()).strip("-")

# --- Загружаем Stage-1
stage1_data = json.load(open(STAGE1_JSON, "r", encoding="utf-8"))
stage1_set  = { rec.get("normalized", _norm(rec.get("input",""))) for rec in stage1_data }
print(f"Кандидатов из Stage-1: {len(stage1_data)}")

# --- ЛОКАЛЬНАЯ «сырая» функция запроса к Wiktionary (без зависимости от глобалов)
def _wiktionary_ru_has_russian_entry_raw(word: str, timeout=8.0) -> bool:
    API = "https://ru.wiktionary.org/w/api.php"
    headers = {"User-Agent": "WordValidator/1.0 (contact: example@example.com)"}
    q = {
        "action": "query", "format": "json", "redirects": 1, "titles": word,
        "prop": "revisions", "rvprop": "content", "rvslots": "main", "formatversion": 2
    }
    try:
        r = requests.get(API, params=q, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return False
        content = pages[0].get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("content", "")
        if not isinstance(content, str):
            return False
        return bool(
            re.search(r"==\s*Русский\s*==", content, re.IGNORECASE) or
            re.search(r"\{\{\s*-ru-\s*\}\}", content, re.IGNORECASE)
        )
    except Exception:
        return False

# --- КЭШ поверх «сырой» функции (никаких глобалов внутри)
try:
    _wikt_cache = json.load(open(CACHE_PATH, "r", encoding="utf-8"))
except Exception:
    _wikt_cache = {}

def _wiktionary_cached(word: str, timeout=8.0) -> bool:
    key = _norm(word)
    if key in _wikt_cache:
        if _wikt_cache[key] is False:
            # мягкий recheck одним реальным запросом
            ok = _wiktionary_ru_has_russian_entry_raw(key, timeout=timeout)
            _wikt_cache[key] = bool(ok)
            return _wikt_cache[key]
        return _wikt_cache[key]
    ok = _wiktionary_ru_has_russian_entry_raw(key, timeout=timeout)
    _wikt_cache[key] = bool(ok)
    return _wikt_cache[key]

# --- ВКЛЮЧАЕМ наш кэш в validate_word, подменив глобальную ссылку ровно на нашу обёртку:
globals()["wiktionary_ru_has_russian_entry"] = _wiktionary_cached

# --- Тестовые наборы
positive_test = ["стол","чат","наст","сота"]
negative_test = ["столмв","сто","вейс","ство","нство","красный","зренье"]

# 1) Жёстко проверяем присутствие позитивов в Stage-1
missing = [w for w in positive_test if _norm(w) not in stage1_set]
if missing:
    print("❌ Тест НЕ пройден. Позитивные слова отсутствуют в Stage-1:")
    for w in missing: print("   •", w)
    raise SystemExit
print("✓ Позитивы присутствуют в Stage-1.")

# 2) Проверяем финальные исходы ДО прогона (validate_word уже использует наш кэш)
def is_valid_final(w: str) -> bool:
    return validate_word(w, ZIPF_FREQ_THRESHOLD)["final"] == "валидно ✅"

errors = False
for w in positive_test:
    if not is_valid_final(w):
        print(f"❌ (должно быть ВАЛИДНО в финале): {w}")
        errors = True
for w in negative_test:
    if is_valid_final(w):
        print(f"❌ (должно быть НЕвалидно в финале): {w}")
        errors = True

if errors:
    print("❌ Тест НЕ пройден. Финальная прогрузка остановлена.")
    raise SystemExit
print("✅ Тест пройден. Запускаем финальную прогрузку...")

# --- Финальный прогон Stage-1 кандидатов
final_records = []
already = set()
if os.path.exists(FINAL_JSON):
    try:
        prev = json.load(open(FINAL_JSON, "r", encoding="utf-8"))
        if isinstance(prev, list):
            final_records = prev
            already = {r["normalized"] for r in final_records}
            print(f"Обнаружен прогресс: {len(final_records)} записей — продолжим.")
    except Exception:
        pass

for rec in tqdm(stage1_data, desc="Stage-2: validate with Wiktionary"):
    w = rec["normalized"]
    if w in already:
        continue
    info = validate_word(w, ZIPF_FREQ_THRESHOLD)  # внутри вызовется _wiktionary_cached
    if info["final"] == "валидно ✅":
        final_records.append(info)
        already.add(w)

# --- Сохранение результата и кэша
with open(FINAL_JSONL, "w", encoding="utf-8") as f:
    for obj in final_records:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
with open(FINAL_JSON, "w", encoding="utf-8") as f:
    json.dump(final_records, f, ensure_ascii=False, indent=2)
with open(FINAL_LIST, "w", encoding="utf-8") as f:
    json.dump(sorted(already), f, ensure_ascii=False, indent=2)
with open(CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump(_wikt_cache, f, ensure_ascii=False, indent=2)

print("Готово. Финальные файлы сохранены:")
print("  • JSONL:", FINAL_JSONL)
print("  • JSON :", FINAL_JSON)
print("  • LIST :", FINAL_LIST)
print("  • CACHE:", CACHE_PATH)

#cell 5 (тестовая проверка индивидуальных слов из json)

# =======================
# Проверочная ячейка: интерактивная валидация по сохранённым JSON на Google Drive
# =======================
import os, json, re
from google.colab import drive

# --- 0) Монтаж Google Drive (без принудительного ремонта)
try:
    drive.mount('/content/drive')
except:
    pass

# --- 1) Пути к файлам на диске (проверьте, при необходимости поправьте папку)
BASE_DIR   = "/content/drive/MyDrive/word_validation"
FINAL_JSON = os.path.join(BASE_DIR, "valid_words_full.json")   # финальный полный JSON
STAGE1_JSON= os.path.join(BASE_DIR, "valid_stage1.json")       # кандидаты этапа 1
LIST_JSON  = os.path.join(BASE_DIR, "valid_words_only.json")   # просто список слов (из финала)

# --- 2) Быстрый загрузчик и проверка наличия
def _must_exist(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Файл не найден: {path}")
    return path

final_data  = json.load(open(_must_exist(FINAL_JSON),  "r", encoding="utf-8"))
stage1_data = json.load(open(_must_exist(STAGE1_JSON), "r", encoding="utf-8"))
final_words = set(json.load(open(_must_exist(LIST_JSON),  "r", encoding="utf-8")))

print(f"Загружено из Google Drive:")
print(f"  • финал: {len(final_data)} записей")
print(f"  • stage1: {len(stage1_data)} записей")
print(f"  • список слов (финал): {len(final_words)} слов")

# --- 3) Индексы по нормализованной форме для быстрых lookup
def _normalize(s: str) -> str:
    # если у вас уже есть clean_token(...) из предыдущих ячеек — можете использовать её.
    return re.sub(r"[^а-яё-]", "", s.strip().lower()).strip("-")

idx_stage1 = { rec.get("normalized", _normalize(rec.get("input",""))): rec for rec in stage1_data }
idx_final  = { rec.get("normalized", _normalize(rec.get("input",""))): rec for rec in final_data  }

# --- 4) Помощник красивого вывода
def _print_block(title: str, d: dict | None):
    print(f"\n— {title} —")
    if not d:
        print("  нет данных")
        return
    fields = [
        ("ИТОГ", d.get("final") or d.get("stage1_final")),
        ("Ввод", d.get("input")),
        ("Нормализовано", d.get("normalized")),
        ("pymorphy2", d.get("pymorphy_check")),
        ("Частотность", d.get("freq_check")),
        ("Wiktionary", d.get("wiktionary_check", "—")),
        ("Zipf(форма)", d.get("zipf_form")),
        ("Zipf(лемма)", d.get("zipf_lemma")),
    ]
    for k, v in fields:
        print(f"  ▸ {k}: {v}")

def _live_validate(word: str):
    """Живой прогон через текущую validate_word(...), если она объявлена."""
    try:
        res = validate_word(word, ZIPF_FREQ_THRESHOLD)
        return res
    except NameError:
        # Если validate_word нет в памяти, выдаём пустой отчёт
        return {
            "input": word,
            "normalized": _normalize(word),
            "final": "—",
            "pymorphy_check": "—",
            "freq_check": "—",
            "wiktionary_check": "—",
            "zipf_form": None,
            "zipf_lemma": None,
        }

print("\nГотово. Введите слово (пустая строка — выход).")

while True:
    q = input("\nСлово: ").strip()
    if not q:
        print("Выход.")
        break

    norm = _normalize(q)
    s1   = idx_stage1.get(norm)
    fin  = idx_final.get(norm)
    live = _live_validate(q)

    print("\n====================== ОТЧЁТ ======================")
    print(f"Слово: «{q}»   (нормализовано: «{norm}»)")
    print(f"В финальном списке JSON: {'ДА' if norm in final_words else 'НЕТ'}")
    _print_block("Stage 1 (pymorphy2 + Zipf) — сохранённые данные", s1)
    _print_block("Финал (pymorphy2 + Zipf + Wiktionary) — сохранённые данные", fin)
    _print_block("Живой прогон validate_word(...) сейчас", live)
    print("===================================================\n")


_______________________________________

5 Sep 2025
Финально валидных слов: 13025
Сохранено финальное:
  • JSONL: /content/drive/MyDrive/word_validation/valid_words_full.jsonl
  • JSON : /content/drive/MyDrive/word_validation/valid_words_full.json
  • LIST : /content/drive/MyDrive/word_validation/valid_words_only.json