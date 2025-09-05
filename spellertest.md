import requests

YANDEX_SPELLER_URL = "https://speller.yandex.net/services/spellservice.json/checkText"

def check_word(word: str):
    """Проверка слова на орфографию через Яндекс.Спеллер"""
    response = requests.get(YANDEX_SPELLER_URL, params={"text": word, "lang": "ru"})
    result = response.json()

    if not result:
        return f"✅ '{word}' — ошибок не найдено."
    else:
        # Берём первую подсказку
        suggestion = result[0].get("s", ["нет вариантов"])[0]
        return f"❌ '{word}' — ошибка. Возможно, имелось в виду: '{suggestion}'"

def main():
    print("Введите слово для проверки (или 'exit' для выхода):")
    while True:
        word = input("👉 Слово: ").strip()
        if word.lower() == "exit":
            print("Программа завершена.")
            break
        if not word:
            continue

        print(check_word(word))

if __name__ == "__main__":
    main()
