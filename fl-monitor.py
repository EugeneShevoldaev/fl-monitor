#!/usr/bin/env python3
"""Монитор заказов FL.ru по RSS — законно, без парсинга.

Читает https://www.fl.ru/rss/all.xml, фильтрует по ключевым словам,
новые заказы отправляет в Telegram.

Запуск: python3 fl-monitor.py
Токен и chat_id — в config.json рядом со скриптом (права 600).
"""
import fcntl
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
STATE = BASE / "state.json"
LOG = BASE / "fl-monitor.log"
LOCK = BASE / "fl-monitor.lock"

RSS_URL = "https://www.fl.ru/rss/all.xml"
FR_URL = "https://freelance.ru/task"   # публичная лента заданий (без RSS)
MAX_PER_RUN = 8          # не спамить: максимум сообщений за проход
MAX_STATE = 800          # сколько id помнить
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/156 Safari/537.36"

# Пробный прогон: ничего не отправляем и не запоминаем — только пишем в лог.
DRY_RUN = "--dry-run" in sys.argv
# --recheck: перепроверить ленту новыми правилами и прислать то, что раньше отсеяли
RECHECK = "--recheck" in sys.argv

# Ключевые слова: (слово в нижнем регистре, вес)
KEYWORDS = {
    # === ТОЧНЫЕ ТЕМЫ (сильные) ===
    "парсинг": 4, "парсер": 4, "спарсить": 4,
    "scrapy": 4, "selenium": 4, "playwright": 4,
    "python": 3, "c#": 3, "csharp": 3,
    "linux": 3, "nginx": 3, "ssl": 3, "сертификат": 2, "vps": 3,
    "docker": 3, "devops": 3, "bash": 3, "powershell": 2,
    "скрипт": 3, "автоматизац": 3, "автоматизир": 3,
    "бота": 5, "бот ": 4, "telegram-бот": 6, "телеграм-бот": 6, "телеграм бот": 6, "aiogram": 6, "tg-бот": 6,
    "excel": 6, "гугл-таблиц": 6, "google sheets": 6, "эксель": 6, "таблица": 3,
    "api": 1, "интеграц": 1, "вебхук": 2, "webhook": 2,
    "база данных": 2, "postgres": 2, "mysql": 2, "sqlite": 2,
    "postfix": 4, "exim": 4, "roundcube": 4, "imap": 3, "smtp": 3,
    "ubuntu": 2, "дебиан": 2, "debian": 2, "сеть": 1,

    # === ТУННЕЛИ И VPN (31.10.2026, по просьбе Жеки) ===
    # Причина: заказ 5527474 (Xray + Hysteria2) прошёл фильтр случайно, по слову
    # «сервер». Ни одного слова о самих протоколах в списке не было.
    "xray": 8, "vless": 8, "hysteria": 8, "hysteria2": 8, "reality": 6,
    "wireguard": 7, "openvpn": 6, "shadowsocks": 7, "sing-box": 7, "singbox": 7,
    "3x-ui": 6, "marzban": 6, "v2ray": 7, "trojan": 5, "socks5": 5,
    "vpn": 7, "туннел": 7, "прокси": 5, "proxy": 4, "iptables": 4,
    "nftables": 5, "kill switch": 6, "статический ip": 5, "выделенный ip": 4,

    # === ПРОСТЫЕ ЗАКАЗЫ ДЛЯ РЕЙТИНГА (тоже берём!) ===
    # 30.10.2026: отзывы убраны из «простых заказов». 500 ₽ за отзыв — не работа,
    # а шум: они забивали ленту и стояли выше всего остального.
    # 30.10.2026: контент для маркетплейсов — НЕ наша линия. Не запрет, а низкий
    # вес: сам по себе такой заказ порог 6 не пройдёт, но «заполнить карточки
    # скриптом/через Excel» пройдёт за счёт автоматизации. Решение Жеки:
    # первый уровень — автоматизация, карточки и отзывы не выпячиваем.
    "отзыв": -10, "отзывы": -10, "написать отзыв": -12,
    "карточк": 1, "карточки товар": 1, "маркетплейс": 1,
    "wildberries": 1, "вайлдберриз": 1, "ozon": 1, "озон": 1,
    "заполнить": 1, "наполнить": 1, "контент карточк": 1,
    "нейминг": 0, "инфографик": -8, "обработка фото": 3,
    "слайд": -8, "3d модел": -8, "архикад": -8, "дизайн": -6,
    "рерайт": 1, "текст": 1, "описание товар": 1,
    "набор текста": 1, "перенос данных": 3, "копировать": 2,
    "таблиц": 3, "графики": 2,

    # === НАСТРОЙКА / АГЕНТЫ (наше, если не "уровень бога") ===
    "настроить сервер": 5, "настройка сервер": 5, "наладить сервер": 5,
    "настроить vps": 5, "установка сервер": 4, "развернуть": 2,
    "домен": 2, "dns": 2, "нужно настроить": 1,
    "агент": 3, "ассистент": 2, "автоответ": 3, "автоматизировать": 3,
    "ии-агент": 3, "ai-агент": 3, "ai агент": 3,
    "нейросет": 2, "gpt": 2, "openai": 2, "deepseek": 2,

    # === СКРИПТЫ И НЕБОЛЬШИЕ ПРИЛОЖЕНИЯ ===
    "создание скрипт": 5, "написать скрипт": 5, "скрипт на": 4,
    "небольшое приложение": 5, "простое приложение": 5,
    "небольшая программ": 5, "программа для": 2, "утилит": 6, "утилита": 6,
    "консольн": 3, "автозаполн": 6, "макрос": 6, "vba": 6,
    "автоматическое заполнение": 6, "заполнение формы": 6, "форма ввода": 5,
    "заполнение документ": 6, "шаблон документ": 5, "накладн": 4,
    "автоформа": 4, "форма с полями": 8, "анкета": 3, "форма с автозаполн": 10,
    "обработка данных": 3, "выгрузка": 3, "отчет": 2, "отчёт": 2,
    "скопировать данные": 5, "перенести данные": 5, "перенос данных": 5,
    # 30.10.2026: «собрать базу/данные» руками — это не парсинг, а ручной труд.
    "собрать данные": -6, "собрать базу": -6, "спарсить": 6,
    "перенести сайт": 6, "перенос сайта": 6, "переехать на": 4,
    "парсинг": 6, "автоматизация отчёт": 6, "автоматизация отчет": 6,

    # === МУСОР (вычитается) ===
    # чужие аккаунты и обходы
    "обход античит": -20, "античит": -20, "накрутк": -20, "лайкер": -15, "лайк": -8, "комментариев на": -10,
    "авторегистрац": -20, "авторегер": -20, "читы": -20,
    "восстановление доступа": -20, "восстановить доступ": -20,
    "восстановить почт": -20, "взлом": -20, "пароль от": -15,
    "rpa": -10, "куки": -10, "cookies": -10, "сессионные": -10,
    "накруток": -20, "отзывы за деньги": -20,
    "с разных аккаунтов": -20, "разных аккаунт": -20, "аккаунтов": -8,
    # не наша специализация
    "1с": -8, "битрикс": -5, "bitrix": -5, "tilda": -5, "тильда": -5,
    "логотип": -8, "презентац": -6, "дизайн интерьер": -8,
    "seo": -8, "продвижен": -8, "реклама": -6, "копирайт": -8,
    "статьи": -2, "контент-план": -3, "монтаж видео": -8,
    # тяжелые форматы
    "собеседован": -6, "резюме": -6, "mvp": -3, "месяцев": -2,
    # студенческие задания — самый частый мусор в ленте
    "дисциплин": -15, "учебн": -15, "зачёт": -15, "зачет": -15,
    "контрольн": -12, "реферат": -15, "курсов": -12, "курсовая": -12,
    "jira": -4, "claude cli": -4, "zoom": -3, "голосов": -3,
    "микросервис": -3, "highload": -4, "мл модель": -3,
    "в команду": -6, "в штат": -6, "постоянную работу": -8,
    # фантазии заказчика ("уровень бога")
    "как chatgpt": -12, "как у chatgpt": -12, "множество агентов": -10,
    "мультиагент": -10, "полностью самостоятельно": -8,
    "должен сам": -6, "автономн": -4, "команда агентов": -10,
    "замен": -3, "всё сам": -10, "целиком сам": -10,
    "волонтер": -20, "бесплатно": -20,

    # === ТЕХНИЧЕСКИЙ ПЕРЕВОД (03.11.2026, разговор с Жекой) ===
    # Берём прикладное: инструкции, документация, чертежи. Литературные
    # тексты и «для сайта» сюда не идут — там филологи и цена за лист.
    "технический перевод": 6, "перевод документац": 6, "перевод инструкц": 6,
    "перевод паспорт": 6, "перевести документац": 6, "перевести инструкц": 6,
    "локализац": 5, "паспорт оборудован": 5, "руководство по эксплуатац": 5,
    "документац": 3, "инструкц": 3, "техническ": 2, "эксплуатац": 2,
    "перевод": 1, "перевести": 1,

    # === ТЕХНИЧЕСКИЙ SEO (03.11.2026, по слову Жеки) ===
    # Берём только аудит и техническую часть — там результат проверяемый.
    # «Продвижение в топ» и «ссылочный» по-прежнему в минусе: обещать
    # позиции нельзя, а конкуренты обещают.
    "seo-аудит": 9, "аудит seo": 9, "seo аудит": 9, "аудит сайта": 6,
    "технический аудит": 6, "аудит индексац": 8, "индексац": 4,
    "sitemap": 4, "robots.txt": 4, "canonical": 4, "микроразметк": 4,
    "schema.org": 4, "метатег": 4, "битые ссылки": 5, "дубли страниц": 5,
    "pagespeed": 4, "скорость сайта": 4, "поисковый аудит": 6,

    # === ПОД УРОВЕНЬ ЖЕКИ (08.11.2026) ===
    # «Жирные» заказы не значит «наши»: сеньорские и юридические роли отсекаем.
    # Продавать надо то, что он сам проверит и защитит перед заказчиком.
    "сеньор": -8, "senior": -8, "архитектор": -9, "тимлид": -9, "teamlead": -9,
    "team lead": -9, "ведущий разработчик": -8, "head of": -8,
    "юрист": -12, "юридическ": -9, "апелляц": -12, "взыскан": -10,
    "процент от": -10, "% от": -10, "партнерств": -6,
    "monday.com": -8, "salesforce": -8, "sap ": -8, "1с-": -8,
    "собеседован": -10, "тестовое задание на 40": -6,
    # Наше, наоборот, поднимаем: мелкое и проверяемое
    "настроить": 2, "починить": 3, "исправить": 2, "не работает": 3,
    "перенести": 2, "миграц": 2, "выгрузить": 3, "собрать таблицу": 5,
    "обработать": 2, "почист": 3, "разобрать": 2,
}

# --- Что умею я (08.11.2026, по слову Жеки: «большую часть делаешь ты») ---
# Фильтр должен отбирать не «жирное» и не «сеньорское», а посильное мне:
# агент делает работу, заказчик проверяет и сдаёт. См. раздел «Фильтры» в README.
ABILITY_OK = (
    # данные и документы
    "excel", "эксель", "таблиц", "csv", "google sheets", "гугл-таблиц",
    "pdf", "docx", "word документ", "отчёт", "отчет", "сводн", "выгрузк",
    "конверт", "распознать", "ocr", "регулярк", "обработка данных",
    # код и автоматизация
    "парсинг", "парсер", "спарсить", "scrapy", "selenium", "playwright",
    "скрипт", "python", "bash", "powershell", "батник", "макрос", "vba",
    "автоматизац", "автоматизир", "утилит", "консольн", "бота", "бот ",
    "telegram-бот", "телеграм-бот", "aiogram", "tg-бот", "вебхук", "webhook",
    # серверы и сайты
    "nginx", "vps", "сервер", "linux", "docker", "ssl", "сертификат",
    "dns", "домен", "перенести сайт", "перенос сайт", "миграц", "хостинг",
    "настроить", "починить", "исправить", "не работает", "ошибк",
    # тексты и разбор
    "перевод документ", "технический перевод", "документац", "инструкц",
    "аудит сайта", "seo-аудит", "seo аудит",
)

ABILITY_RISKY = (
    "vue", "react", "angular", "админк", "личный кабинет", "дашборд",
    "микросервис", "highload", "мобильное приложение", "android", "apk",
    "интеграц без api", "реверс", "sdk", "маркетплейс api",
    "информационная система", "веб-система", "конструктор", "crm", "erp",
    "кей-арт", "маскот", "иллюстрац", "рендер", "интерьер", "kicad",
    "печатн плат", "металлокаркас", "чертеж детал",
    "крипт", "блокчейн", "смарт-контракт", "laravel", "сопровожд",
)

# Роли, которые нам не подходят по определению: «жирно» != «наше»
ROLE_NO = (
    "сеньор", "senior", "архитектор", "тимлид", "teamlead", "team lead",
    "ведущий разработчик", "head of", "юрист", "юридическ", "апелляц",
    "взыскан", "процент от", "% от", "клиентской поддержк",
    "оператор чата", "менеджер по",
)

ABILITY_NO = (
    "flutter", "kotlin", "swift", "ios", "1с", "битрикс", "bitrix",
    "unity", "3d", "геймдев", "взлом", "накрутк", "обход", "читы",
    "античит", "обход блокировок", "продажа аккаунт", "казино", "ставк",
    "трейдинг", "ninjatrader", "форекс", "бинарн",
    # гос-площадки: нужен доступ/ЭП и опыт (10.11.2026)
    "еис", "44-фз", "223-фз", "госзакуп",

    # целые продукты и сервисы под ключ — не наш размер (09.11.2026)
    "коммерческий сервис", "интеллектуальный сервис", "платных пользовател",
    "10000 платных", "10 000 платных", "витрина данных", "гис ",

    # приманки: «собираю базу исполнителей», «оцениваю стоимость» — это не заказ
    "формирую базу", "собираю базу исполнител", "база исполнителей",
    "оцениваю стоимость задачи", "сбор прайс",
)

# --- Заказы масштаба «построить систему» (11.11.2026) ---
# Живой пример: fl 5528712 — карточка гостя по QR-карте лояльности, интеграции
# с PremiumBonus и кассой, роли, аналитика. Прошёл все фильтры, а по сути это
# месяц работы и приёмка чужого бизнес-процесса. Отсекаем не по одному слову,
# а по сумме признаков: одно «система» бывает и в мелкой задаче, а «система +
# интеграция + аналитика/роли/мобильность» — уже не вечерняя работа.
MONTH_SCALE = (
    "систем", "интеграц", "аналитик", "личный кабинет", "админк", "дашборд",
    "мобильн", "камер", "qr", "штрихкод", "под ключ", "микросервис",
    "база данных", "админпанел", "маркетплейс", "роли ",
)
MONTH_SCALE_MIN = 3   # три разных признака сразу — это месяц, а не выходные

# --- Чистка дублей в словаре (02.11.2026) ---
# Причина: «таблица» и «таблиц» считались дважды (по 3), и заказ про сбор
# материалов на масло набирал вес 9 только за счёт одного слова. Если ключ
# является подстрокой другого ключа — оставляем только короткий.
KEYWORDS_CLEAN = {}
for k, w in sorted(KEYWORDS.items(), key=lambda kv: (len(kv[0]), -kv[1])):
    # выбрасываем длинную фразу, только если короткая внутри неё не слабее
    weaker = any(k != other and k in other and abs(w) <= abs(KEYWORDS_CLEAN[other])
                 for other in list(KEYWORDS_CLEAN))
    if weaker:
        continue
    KEYWORDS_CLEAN[k] = w

# --- Технический минимум (02.11.2026) ---
# Заказ считается «нашим», только если в нём есть хотя бы одно слово
# из технических тем. Иначе «сбор материалов», «таблица», «поиск поставщиков»
# тянут вес, а работы по нашей части там нет.
TECH_KEYWORDS = (
    "python", "c#", "csharp", "скрипт", "bash", "powershell", "api", "json",
    "парсинг", "парсер", "спарсить", "scrapy", "selenium", "playwright",
    "бот", "telegram-бот", "телеграм-бот", "aiogram", "tg-бот",
    "сервер", "vps", "linux", "nginx", "docker", "devops", "xray", "vless",
    "hysteria", "wireguard", "vpn", "туннел", "прокси", "iptables",
    "html", "css", "вёрстк", "верстк", "страниц", "сайт",
    "excel", "vba", "макрос", "автоматизац", "автоматизир", "интеграц",
    "вебхук", "webhook", "sql", "postgres", "mysql", "sqlite", "база данных",
    "нейросет", "gpt", "openai", "deepseek", "ии-агент", "ai-агент",
    # технический перевод / документация (03.11.2026)
    "технический перевод", "перевод документац", "перевод инструкц",
    "перевод паспорт", "перевести документац", "перевести инструкц",
    "паспорт оборудован", "руководство по эксплуатац", "локализац", "чертеж",
    "документац", "инструкц", "эксплуатац",
    # технический SEO (03.11.2026)
    "seo-аудит", "аудит seo", "seo аудит", "аудит сайта", "технический аудит",
    "индексац", "sitemap", "robots.txt", "canonical", "микроразметк", "pagespeed",
)

def has_tech(item) -> bool:
    text = f"{item['title']} {item.get('desc','')}".lower()
    return any(k in text for k in TECH_KEYWORDS)

THRESHOLD = 6     # минимальный вес, чтобы прислать (было 3 — слало мусор)

# --- Второй проход: «мелочь» (30.10.2026, просьба Жеки) ---
# Мелкие понятные заказы разбирают за часы, а основной фильтр их не видит:
# вес низкий, потому что тема узкая, но работы на вечер. Ловим отдельно.
# 08.11.2026, Жека: «ценник не по 15к, а без потолка, но начало где-то от 1к».
# То есть дешёвые тоже берём (от тысячи), а сверху не ограничиваем.
BIG_BUDGET = 50_000      # с этого бюджета помечаем «крупный, не для старта»
SMALL_MIN = 1000          # нижняя граница бюджета, ₽
SMALL_MAX = 0             # 0 = без потолка
SMALL_PER_RUN = 3         # больше трёх «мелочей» за проход не шлём
SMALL_MIN_SCORE = 1       # отсекаем откровенный мусор, но не требуем THRESHOLD
# Признаки того, что заказ уже занят. Проверяются на странице заказа.
# 30.10.2026, просьба Жеки: не показывать то, что уже взято.
# Формулировки сверены на живых страницах 30.10.2026:
#   занятый — «Заказчик выбрал исполнителя»
#   в архиве — «Заказ находится в архиве»
# Берём фразами, а не одним словом «архив»: в описаниях заказов встречается
# «данные в архиве», «архивный файл» — одиночное слово дало бы ложные срабатывания.
TAKEN_MARKERS = ("заказчик выбрал исполнителя", "исполнитель выбран",
                 "заказ находится в архиве", "проект находится в архиве",
                 "заказ в архиве", "проект в архиве",
                 "проект закрыт", "заказ закрыт", "проект удалён")
CHECK_LIMIT = 10          # сколько страниц максимум проверить за проход

# --- Отсев по тегам (08.11.2026, просьба Жеки) ---
# «Недорого» заказчик ставит сам — это честное признание, что ищет подешевле.
# Такие не присылаем совсем. Фильтр срабатывает дважды: по заголовку из ленты
# (быстро, без сети) и по тегам страницы заказа (точно).
TAG_BLOCK = ("недорого", "дёшево", "дешево", "бюджетно", "небольшой бюджет")

# Заказы, доступные только платным аккаунтам (PRO/премиум). Откликнуться всё равно
# нельзя, поэтому и показывать нечего: smысл только тратить внимание (10.11.2026).
PREMIUM_MARKERS = ("только для pro", "только для premium", "для pro-аккаунт",
                   "для pro аккаунт", "только для премиум", "премиум-аккаунт",
                   "только для участников с подпиской", "доступно только pro",
                   "только платным аккаунтам",
                   # freelance.ru (10.11.2026)
                   "доступно для владельцев премиум", "для владельцев премиум-аккаунт",
                   "у вас нет доступа к этому заданию")
CHEAP_PER_RUN = 1         # сколько «недорого для отзыва» шлём за проход

SMALL_CATS = ("программирование", "создание скриптов", "ai — искусственный",
              "искусственный интеллект", "автоматизация бизнеса", "мессенджеры")


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 строка, 2 строки, 5 строк."""
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_feed() -> str:
    req = urllib.request.Request(
        RSS_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; FL-monitor/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_items(xml: str):
    out = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def grab(tag):
            m = re.search(
                rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", raw, re.S
            )
            return clean(m.group(1)) if m else ""
        link = grab("link") or grab("guid")
        if not link:
            continue
        out.append({
            "title": grab("title"),
            "link": link,
            "desc": grab("description"),
            "cat": grab("category"),
            "date": grab("pubDate"),
        })
    return out


def fetch_freelance_ru() -> list:
    """freelance.ru не отдаёт RSS — разбираем публичную страницу заданий."""
    req = urllib.request.Request(FR_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    out = []
    seen = set()
    premium_marks = 0
    for m in re.finditer(r'href="(/task/view/\d+)"[^>]*>(.*?)</a>', page, re.S):
        href, raw_title = m.group(1), m.group(2)
        title = clean(raw_title)
        if href in seen or len(title) < 8:
            continue
        seen.add(href)
        # Значок «Только для Премиум» виден уже в списке (10.11.2026):
        # смотрим небольшой кусок разметки вокруг ссылки, страницу не тянем.
        around = page[max(0, m.start() - 800): m.start() + 1500].lower()
        if "только для премиум" in around or "для владельцев премиум" in around:
            premium_marks += 1
            continue
        out.append({
            "title": title,
            "link": "https://freelance.ru" + href,
            "desc": "",
            "cat": "freelance.ru",
            "date": "",
        })
    if premium_marks:
        print(f"freelance.ru: премиумных в ленте — {premium_marks}, берём остальные")
    return out


def score(item) -> int:
    blob = f"{item['title']} {item['cat']} {item['desc']}".lower()
    text = blob
    total = 0
    for word, weight in sorted(KEYWORDS_CLEAN.items(), key=lambda kv: -len(kv[0])):
        if word in text:
            total += weight
            text = text.replace(word, " ")   # чтобы короткие части не считались дважды
    # поправка на мои умения: что умею — плюс, что рискованно — минус
    ok_hits = sum(1 for k in ABILITY_OK if k in blob)
    risky_hits = sum(1 for k in ABILITY_RISKY if k in blob)
    total += min(6, 2 * ok_hits)        # много «умею» — не повод топить вверх
    total -= min(6, 3 * risky_hits)
    return total


def project_info(link: str, timeout: int = 15):
    """Страница заказа: занят ли, теги, описание ТЗ, бюджет.

    Возвращает словарь или None, если страницу не открыть.
    Не смогли проверить — считаем заказ живым: лучше показать лишний раз,
    чем спрятать рабочий заказ.
    """
    try:
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None

    text = html.unescape(re.sub(r"<[^>]+>", " ", raw)).lower()
    taken = any(m in text for m in TAKEN_MARKERS)

    # теги: «Теги: Разработка чат-ботов, недорого, FL.ru»
    tags = []
    m = re.search(r"Теги:\s*([^<\n]+)", raw)
    if m:
        tags = [t.strip().lower() for t in html.unescape(m.group(1)).split(",")]
        tags = [t for t in tags if t and t != "fl.ru"]

    # описание — из JSON-LD, там оно чистым текстом с переводами строк
    desc = ""
    for mm in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S):
        try:
            d = json.loads(mm.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("description"):
            desc = str(d["description"])
            break
    if not desc:
        desc = " ".join(text.split())[:4000]

    lines = len([l for l in desc.split("\n") if l.strip()])
    blob = " ".join(desc.split()).lower()
    premium = any(m in text or m in blob for m in PREMIUM_MARKERS)
    return {"taken": taken, "tags": tags, "desc": desc, "premium": premium,
            "lines": lines, "chars": len(desc)}


def is_taken(link: str, timeout: int = 15) -> bool:
    """Оставлено для совместимости: занят ли заказ."""
    info = project_info(link, timeout)
    return bool(info and info["taken"])


def budget_num(title: str, desc: str = "") -> int:
    """Бюджет числом. 0 — если в заказе он не указан («по договорённости»)."""
    m = re.search(r"Бюджет:\s*([\d\s\u00a0]+)\s*₽", f"{title} {desc}")
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 0


def too_cheap(item) -> bool:
    """Бюджет указан и он ниже нашего порога — за такое не берёмся."""
    b = budget_num(item.get("title", ""), item.get("desc", ""))
    return 0 < b < SMALL_MIN


def is_small(item) -> bool:
    """Заказ по бюджету: наша тема, бюджет от SMALL_MIN (потолка нет), не мусор."""
    b = budget_num(item.get("title", ""), item.get("desc", ""))
    if b == 0:
        return False
    if b < SMALL_MIN:
        return False
    if SMALL_MAX and b > SMALL_MAX:
        return False
    cat = (item.get("cat") or "").lower()
    if not any(c in cat for c in SMALL_CATS):
        return False
    return item.get("score", 0) >= SMALL_MIN_SCORE


def is_month_scale(blob: str) -> bool:
    """Крупная система «под ключ»: месяц работы, не наш размер.

    Считаем не слова, а признаки: см. MONTH_SCALE выше. Один признак
    («система», «аналитика») бывает и в мелкой задаче, поэтому нужно сразу
    несколько — тогда это почти наверняка заказ на месяц.
    """
    return sum(1 for k in MONTH_SCALE if k in blob) >= MONTH_SCALE_MIN


def budget_of(title: str) -> str:
    m = re.search(r"Бюджет:\s*([\d\s\u00a0]+)\s*&#8381;", title)
    if not m:
        m = re.search(r"Бюджет:\s*([\d\s\u00a0]+)", title)
    if m:
        val = re.sub(r"\D", "", m.group(1))
        if val:
            return f"{int(val):,} ₽".replace(",", " ")
    return "по договорённости"


def tg_send(token: str, chat_id, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if not body.get("ok"):
            log(f"telegram error: {body.get('description')}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log(f"telegram exception: {e}")
        return False


def main() -> int:
    cfg = load_json(CONFIG, {})
    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        log("нет токена или chat_id в config.json")
        return 2

    # один экземпляр за раз
    lock_f = LOCK.open("w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    items = []
    try:
        items.extend(parse_items(fetch_feed()))
    except Exception as e:  # noqa: BLE001
        log(f"fl.ru rss недоступен: {e}")

    try:
        items.extend(fetch_freelance_ru())
    except Exception as e:  # noqa: BLE001
        log(f"freelance.ru недоступен: {e}")

    if not items:
        log("оба источника недоступны")
        return 1

    state = load_json(STATE, {"seen": []})
    seen = set(state.get("seen", []))

    sent_before = set(state.get("sent", []))
    fresh = []
    banned = []
    for it in items:
        key = it["link"]
        if key in seen and not RECHECK:
            continue
        if RECHECK and key in sent_before:
            continue
        blob = f"{it['title']} {it.get('desc', '')} {it.get('cat', '')}".lower()
        # то, чего я делать не умею в принципе, не показываем вовсе
        if any(k in blob for k in ROLE_NO) or any(k in blob for k in ABILITY_NO):
            banned.append(it["link"])
            continue
        # системы «под ключ» на месяц — не наш размер (11.11.2026)
        if is_month_scale(blob):
            banned.append(it["link"])
            log(f"отсеяно как крупная система: {it['title'][:70]}")
            continue
        s = score(it)
        it["score"] = s
        fresh.append(it)

    # «Недорого» не выбрасываем, а откладываем отдельно: пока у Жеки нет отзывов,
    # дешёвый простой заказ — это способ их получить. Решение 08.11.2026:
    # слать такие помеченными и не больше одного за проход, только по нашей теме.
    cheap = []
    kept = []
    for it in fresh:
        blob = f"{it['title']} {it.get('desc', '')} {it.get('cat', '')}".lower()
        if any(t in blob for t in TAG_BLOCK):
            cheap.append(it)
            continue
        kept.append(it)
    fresh = kept

    # все новые запоминаем, но присылаем только подходящие
    new_ids = [it["link"] for it in fresh] + [it["link"] for it in cheap] + banned
    sent_links = list(state.get("sent", []))
    good = [it for it in fresh
            if it["score"] >= THRESHOLD and has_tech(it) and not too_cheap(it)]

    # второй проход: мелочь, которую основной фильтр не берёт по весу
    good_links = {it["link"] for it in good}
    small = [it for it in fresh if it["link"] not in good_links and is_small(it)]

    # выбрасываем уже занятые: проверяем страницу только у тех, кто прошёл фильтр
    checked = 0
    taken = []
    premium_skip = []
    infos = {}

    def only_open(cands, limit):
        nonlocal checked
        out = []
        for it in cands:
            if len(out) >= limit:
                break
            if checked >= CHECK_LIMIT:
                out.append(it)          # лимит проверок исчерпан — показываем как есть
                continue
            checked += 1
            info = project_info(it["link"])
            if info is None:
                out.append(it)
                continue
            infos[it["link"]] = info
            if info["taken"]:
                taken.append(it["link"])
                continue
            if info.get("premium"):
                premium_skip.append(it["link"])
                continue
            # тег страницы: заказчик сам сказал «недорого» — откладываем для отзыва
            if any(t in TAG_BLOCK for t in info["tags"]):
                cheap.append(it)
                continue
            out.append(it)
        return out

    small = only_open(small, SMALL_PER_RUN)
    good = only_open(good, MAX_PER_RUN)

    def extra_lines(it) -> str:
        """Приписка к сообщению: сколько ТЗ и какие теги."""
        info = infos.get(it["link"])
        if not info:
            return ""
        out = (f"📄 ТЗ: {plural(info['lines'], 'строка', 'строки', 'строк')}, "
           f"{info['chars']} зн.\n")
        if info["tags"]:
            tags = ", ".join(info["tags"][:4])
            out += f"🏷 {html.escape(tags)}\n"
        return out

    sent = 0
    for it in small[:SMALL_PER_RUN]:
        shown = re.sub(r"\s*\((?:Бюджет[^)]*|для всех)\)\s*$", "", it["title"]).strip()
        text = (
            f"💵 <b>по бюджету: {html.escape(shown)}</b>\n"
            f"💰 {budget_of(it['title'])}\n"
            f"{extra_lines(it)}"
            f"📂 {html.escape(it['cat'])}\n"
            f"{html.escape(it['link'])}"
        )
        if DRY_RUN:
            log(f"[dry] {text}")
            sent += 1
        elif tg_send(token, chat_id, text):
            sent += 1
            log(f"→ отправлено: {it['title'][:70]} | {it['link']}")
            time.sleep(1)

    for it in good[:MAX_PER_RUN]:
        title = it["title"]
        # убрать хвост "(Бюджет: ...)" и "(для всех)"
        shown = re.sub(r"\s*\((?:Бюджет[^)]*|для всех)\)\s*$", "", title).strip()
        bsum = budget_num(title, it.get("desc", ""))
        head = ("🏗 <b>крупный — не для старта</b>\n" + f"<b>{html.escape(shown)}</b>"
                if bsum >= BIG_BUDGET else f"🎯 <b>{html.escape(shown)}</b>")
        text = (
            f"{head}\n"
            f"💰 {budget_of(title)}\n"
            f"{extra_lines(it)}"
            f"📂 {html.escape(it['cat'])}\n"
            f"⚖️ вес {it['score']}\n"
            f"{html.escape(it['link'])}"
        )
        if DRY_RUN:
            log(f"[dry] {text}")
            sent += 1
        elif tg_send(token, chat_id, text):
            sent += 1
            log(f"→ отправлено: {it['title'][:70]} | {it['link']}")
            time.sleep(1)

    # «Недорого» шлём отдельной строкой и редко: пока отзывов нет, дешёвый
    # простой заказ полезен рейтингом. Только наша тема (иначе это карточки,
    # тексты и прочий ручной труд) и не больше CHEAP_PER_RUN за проход.
    cheap = list({it["link"]: it for it in cheap}.values())
    cheap = [it for it in cheap if has_tech(it)]
    for it in cheap[:CHEAP_PER_RUN]:
        if it["link"] not in infos:
            info = project_info(it["link"])
            if info:
                infos[it["link"]] = info
        title = it["title"]
        shown = re.sub(r"\s*\((?:Бюджет[^)]*|для всех)\)\s*$", "", title).strip()
        text = (
            f"🏷 <b>недорого — для отзыва</b>\n"
            f"<b>{html.escape(shown)}</b>\n"
            f"💰 {budget_of(title)}\n"
            f"{extra_lines(it)}"
            f"📂 {html.escape(it['cat'])}\n"
            f"{html.escape(it['link'])}"
        )
        if DRY_RUN:
            log(f"[dry] {text}")
            sent += 1
        elif tg_send(token, chat_id, text):
            sent += 1
            log(f"→ отправлено: {it['title'][:70]} | {it['link']}")
            time.sleep(1)

    merged = new_ids + state.get("seen", [])
    state["seen"] = merged[:MAX_STATE]
    state["updated"] = datetime.now(timezone.utc).isoformat()
    state["sent"] = (sent_links + [it["link"] for it in (small + good + cheap[:CHEAP_PER_RUN])])[:MAX_STATE]
    if not DRY_RUN:
        save_json(STATE, state)

    if fresh or cheap:
        log(f"новых: {len(fresh) + len(cheap)}, подходящих: {len(good)}, "
            f"по бюджету: {len(small)}, занятых: {len(taken)}, "
            f"«недорого для отзыва»: {len(cheap)}, только PRO: {len(premium_skip)}, "
            f"отправлено: {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
