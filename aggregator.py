# -*- coding: utf-8 -*-
"""News aggregator MVP v2 — RSS -> pre-filter -> GigaChat -> Telegram.

Changes vs v1:
  + pre-filter discards 60-80% of non-market noise BEFORE LLM
  + stricter prompt: requires concrete tickers / percentages, refuses vague impact
  + fallback for politically-sensitive topics (raw repost, no AI analysis)
  + HTML formatting (no emojis), bold/italic, clean source citation

Usage:
    python3 aggregator.py            # один прогон
    python3 aggregator.py --dry-run  # без публикации
    python3 aggregator.py --limit 30 # обработать только N новостей
"""
import os, sys, json, time, hashlib, urllib.request, urllib.parse, ssl, uuid, argparse, re
from datetime import datetime, timedelta, timezone
from html import unescape, escape as html_escape

import feedparser
import yaml

HOME = os.path.expanduser("~")
# In GitHub Actions we run from repo root; locally — from ~/Documents/news_aggregator
DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HOME, "Documents", "ai_consulting")


def _load_secret(env_name, file_name, required=True):
    val = os.environ.get(env_name)
    if val:
        return val.strip()
    path = os.path.join(SECRETS, file_name)
    if os.path.exists(path):
        return open(path).read().strip()
    if required:
        raise RuntimeError(f"Missing secret: env {env_name} or file {path}")
    return None


GIGACHAT_AUTH = _load_secret("GIGACHAT_AUTH", "gigachat_auth.txt")
TG_TOKEN = _load_secret("TG_BOT_TOKEN", "tg_bot_token.txt")
TG_CHAT_ID = _load_secret("TG_NEWS_CHAT_ID", "tg_news_chat_id.txt", required=False)

SOURCES_PATH = os.path.join(DIR, "sources.yml")
SEEN_PATH = os.path.join(DIR, "seen.txt")

GIGACHAT_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ----------------------- PRE-FILTER -----------------------

# If title or summary contains these — automatically skip (low signal for market)
NEGATIVE_KEYWORDS = [
    # спорт
    "матч", "забил", "гол", "чемпион", "плей-офф", "плей офф", "кубок", "гран-при",
    "пенальти", "тайм", "тренер сборной", "клуб", "ufc", "mma", "хоккей", "футбол",
    "тенниc", "теннисист", "леброн", "карпин", "роналдо", "месси",
    # происшествия / криминал
    "погиб", "пострадал", "дтп", "столкнул", "столкновение машин",
    "школьник", "девятиклассник", "подросток открыл", "стрельба в школе",
    "ограбил", "украл", "ножом", "пьяный", "наркотик", "задержали", "подозреваемого",
    "найден мертв", "нашли мертвым", "осужден", "приговор",
    # развлечения
    "сериал", "фильм", "актер", "актриса", "певец", "певица", "концерт", "звезда",
    "блогер", "ютубер", "тиктокер", "звёзд", "холостяк",
    # бытовое
    "погода", "циклон", "шторм", "метель", "снегопад", "ливень",
    "праздник", "парад", "фестиваль", "карнавал",
    # политические заявления без рыночного эффекта
    "учительниц", "родственник",
    # local-news
    "московского", "петербурга", "оренбург", "чебоксары",  # — пока опасно, но эти географические как маркер бытовой
]

# Always pass through (override negative if any of these in title)
POSITIVE_KEYWORDS = [
    # макро и регуляторы
    "ставк", "ключевая ставка", "ставку", "цб", "банк россии", "фрс", "ецб", "цб рф",
    "инфляц", "вв п", "ввп", "минфин", "минэк",
    # курс
    "курс рубл", "доллар", "евро", "юан", "юань", "usd", "eur", "cny",
    # биржа/инструменты
    "акци", "облигац", "moex", "imoex", "rts", "индекс",
    "ipo", "spo", "buyback", "делистинг", "дивиденд", "выплат", "выручк",
    "отчет", "отчёт", "прибыл", "убыт",
    # эмитенты РФ
    "газпром", "сбер", "сберб", "лукойл", "роснефт", "норникель", "норильск",
    "новатэк", "татнефт", "втб", "магнит", "x5", "озон", "ozon", "яндекс",
    "северстал", "пик", "лср", "мечел", "русал", "алроса", "фосагро", "полюс",
    "тинькоф", "т-банк", "тбанк", "мтс", "ростелеком", "транснефт", "аэрофлот",
    # эмитенты глобальные
    "apple", "tesla", "nvidia", "microsoft", "amazon", "google", "meta",
    # сырьё
    "нефть", "brent", "wti", "opec", "опек", "газ ttf", "цена нефти", "цены на нефть",
    "уголь", "медь", "никель", "золото", "палладий", "пшениц",
    # инфраструктура / удары по объектам (важно для нефтегаза и обороны)
    "нпз", "нефтебаз", "газопровод", "трубопровод", "хранилище нефт",
    "удар по нпз", "атака на нпз", "обстрел нпз", "взрыв на нпз",
    # санкции / геополитика с рыночным импактом
    "санкци", "эмбарго", "ограничен", "трамп", "путин и трамп",
    # M&A
    "поглощен", "слияние", "приобрел", "купил долю",
]


def _word_match(kw, txt):
    """Match keyword with word boundaries (avoids 'ставк' catching 'поставках')."""
    # If keyword has space — match as substring; else require word boundary
    if " " in kw:
        return kw in txt
    pat = r"\b" + re.escape(kw)
    return re.search(pat, txt) is not None


def precheck(item):
    """Return (passes_prefilter: bool, reason: str)."""
    txt = (item["title"] + " " + item["summary"]).lower()
    # positive override
    for kw in POSITIVE_KEYWORDS:
        if _word_match(kw, txt):
            return True, f"positive:{kw}"
    # negative reject
    for kw in NEGATIVE_KEYWORDS:
        if _word_match(kw, txt):
            return False, f"negative:{kw}"
    return True, "neutral"


# ----------------------- HTTP / GigaChat -----------------------

def http_post(url, headers, data=None, json_body=None):
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode("utf-8", errors="replace")}


def get_gigachat_token():
    return http_post(
        GIGACHAT_OAUTH,
        headers={
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_AUTH}",
        },
        data={"scope": "GIGACHAT_API_PERS"},
    )["access_token"]


def ask_gigachat(token, system, user, max_tokens=500, temp=0.2):
    return http_post(
        GIGACHAT_CHAT,
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "model": "GigaChat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temp,
            "max_tokens": max_tokens,
        },
    )


FILTER_SYSTEM = """Ты строгий финансовый редактор канала. Решаешь, важна ли новость для российского инвестора.

Отвечай СТРОГО в JSON (без markdown), без пояснений:
{"important": true|false, "reason_skip": "если false — одна фраза почему", "category": "макро|геополитика|компании|сектор|сырьё", "translation": "если en — короткий перевод заголовка и сути, иначе пустая строка", "summary": "2 строки сути по-русски без вводных слов", "impact": "1-2 предложения: НАЗВАНИЕ СЕКТОРА/АКТИВА (например 'российский нефтегаз', 'рубль', 'банковский сектор РФ', 'нефть Brent', 'индекс MOEX', 'американский техсектор') И НАПРАВЛЕНИЕ (рост/снижение/давление/поддержка/нейтрально-позитивно) С КРАТКОЙ ПРИЧИНОЙ. Без процентов и цифр прогнозов.", "assets": ["до 4 названий по-русски: 'российский нефтегаз', 'рубль', 'индекс MOEX', 'банковский сектор РФ', 'нефть Brent' и т.п."]}

КРИТЕРИИ important=true (нужны ВСЕ):
1. Есть новостной повод (не аналитика, не прогноз без события)
2. Можно назвать конкретный сектор/актив И направление движения (рост/снижение/давление)
3. Есть понятная причинная связь между событием и активом
4. ФОКУС НА РФ: либо российский актив, либо иностранный с прямой связью на РФ-рынок (нефть, газ, доллар, юань, санкции, ФРС). Новости про корейский / японский / индийский / бразильский рынок БЕЗ связи с РФ → important=false. Новости про экзотические сырьевые товары (сера, кофе, какао) → important=false.
5. ПОЛИТИЧЕСКИЕ ЗАЯВЛЕНИЯ без события (Песков сказал, Дмитриев заявил, эксперт прокомментировал) → important=false. Только реальные события.

ВАЖНЫЕ ТЕМЫ:
- Ставка ЦБ/ФРС/ЕЦБ
- Корпоративные отчёты, дивиденды, buyback, M&A эмитентов
- Санкции, эмбарго, удары по инфраструктуре (НПЗ, трубопроводы)
- Решения ОПЕК+, добыча нефти/газа
- IPO, SPO, делистинг
- Регуляторные решения с эффектом на сектор
- Курсовые движения с поводом (резкое укрепление/ослабление рубля)

ЗАПРЕЩЕНО (это паттерны плохого impact):
- "Может повлиять на настроения инвесторов"
- "Х%" с буквой вместо числа
- "USDJPY: вверх" если новость про юань/рубль
- "Влияние на волатильность активов"
- Названия тикеров вроде "SNGCN", если не уверен в реальности тикера

ЕСЛИ НЕ МОЖЕШЬ НАЗВАТЬ КОНКРЕТНЫЙ СЕКТОР И НАПРАВЛЕНИЕ — ставь important=false."""


def analyze(token, item):
    user = f"Источник: {item['source']}\nЯзык: {item['lang']}\nЗаголовок: {item['title']}\nКраткое содержание: {item['summary']}"
    resp = ask_gigachat(token, FILTER_SYSTEM, user)
    if "_error" in resp:
        return {"_error": resp}
    content = resp["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE)
    # Detect censorship refusal
    if "не обладают собственным мнением" in content or "чувствительные темы" in content.lower():
        return {"_censored": True, "_raw": content[:150]}
    try:
        return json.loads(content)
    except Exception as e:
        return {"_parse_error": str(e), "_raw": content[:300]}


# ----------------------- Sources / fetch -----------------------

def fetch_sources():
    with open(SOURCES_PATH) as f:
        cfg = yaml.safe_load(f)
    items = []
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        print(f"[fetch] {src['name']} ... ", end="", flush=True)
        try:
            d = feedparser.parse(src["url"])
            cnt = 0
            for e in d.entries[:30]:
                title = unescape(getattr(e, "title", "")).strip()
                link = getattr(e, "link", "")
                summary = unescape(re.sub(r"<[^>]+>", " ", getattr(e, "summary", ""))).strip()
                summary = re.sub(r"\s+", " ", summary)[:500]
                if not title or not link:
                    continue
                items.append({
                    "source": src["name"],
                    "lang": src["lang"],
                    "title": title,
                    "link": link,
                    "summary": summary,
                })
                cnt += 1
            print(f"{cnt} items")
        except Exception as ex:
            print(f"FAIL: {ex}")
    return items


def hash_item(item):
    return hashlib.md5(f"{item['source']}|{item['title']}".encode("utf-8")).hexdigest()


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return set()
    return set(line.strip() for line in open(SEEN_PATH) if line.strip())


def save_seen(seen):
    with open(SEEN_PATH, "w") as f:
        for h in sorted(seen):
            f.write(h + "\n")


# ----------------------- Formatting / publish -----------------------

def format_post(item, analysis):
    """HTML format for Telegram, no emojis."""
    lines = []

    # Title
    title = item["title"]
    if item["lang"] == "en" and analysis.get("translation"):
        title = analysis["translation"]
    lines.append(f"<b>{html_escape(title)}</b>")
    lines.append("")

    # Summary
    summary = analysis.get("summary", "").strip()
    if summary:
        lines.append(html_escape(summary))
        lines.append("")

    # Impact
    impact = analysis.get("impact", "").strip()
    if impact:
        # Decode possible HTML entities returned by LLM, then re-escape properly
        impact = unescape(impact)
        lines.append(f"<b>Влияние:</b> {html_escape(impact)}")
        lines.append("")

    # Assets / sectors
    assets = analysis.get("assets") or analysis.get("tickers") or []
    if assets:
        # Filter out fake-looking 4-6 letter all-caps tickers (likely hallucinations)
        clean = [a for a in assets if not (re.fullmatch(r"[A-Z]{3,7}", a) and len(a) >= 3)]
        if clean:
            lines.append(f"<b>Активы:</b> {html_escape(', '.join(clean))}")
            lines.append("")

    # Source + link
    lines.append(f"<i>{html_escape(item['source'])}</i> · <a href=\"{html_escape(item['link'])}\">оригинал</a>")

    return "\n".join(lines)


def format_raw_repost(item):
    """For censored/political topics — pass without analysis."""
    title = item["title"]
    lines = [
        f"<b>{html_escape(title)}</b>",
        "",
    ]
    if item["summary"]:
        lines.append(html_escape(item["summary"][:400]))
        lines.append("")
    lines.append(f"<i>{html_escape(item['source'])}</i> · <a href=\"{html_escape(item['link'])}\">оригинал</a>")
    return "\n".join(lines)


def send_telegram(text):
    if not TG_CHAT_ID:
        print("[!] TG_CHAT_ID not set — skipping send")
        return None
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode("utf-8", errors="replace")}


# ----------------------- Main -----------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--show-skipped", action="store_true")
    parser.add_argument("--no-pre-filter", action="store_true")
    args = parser.parse_args()

    items = fetch_sources()
    print(f"\n[total fetched] {len(items)}")

    seen = load_seen()
    new = [it for it in items if hash_item(it) not in seen]
    print(f"[new]           {len(new)}")

    # Pre-filter
    if not args.no_pre_filter:
        before = len(new)
        kept = []
        for it in new:
            ok, reason = precheck(it)
            if ok:
                it["_prefilter"] = reason
                kept.append(it)
        new = kept
        print(f"[after prefilter] {len(new)}  (отсеяно {before - len(new)})")

    if args.limit:
        new = new[: args.limit]
        print(f"[limit applied] {len(new)}")

    if not new:
        print("Nothing new after filter.")
        return

    token = get_gigachat_token()
    print(f"[gigachat token ok]\n")

    posted = 0
    skipped_llm = 0
    political_passes = 0

    for item in new:
        h = hash_item(item)
        print(f"--- [{item.get('_prefilter','?')}] {item['source']}: {item['title'][:80]}")

        a = analyze(token, item)

        # Censorship → political fallback (publish raw if it has positive keyword)
        if a.get("_censored"):
            if item.get("_prefilter", "").startswith("positive:"):
                text = format_raw_repost(item)
                print(f"    [POLITICAL → RAW REPOST]\n{text[:300]}\n")
                if not args.dry_run:
                    r = send_telegram(text)
                    if r and r.get("ok"):
                        posted += 1
                        political_passes += 1
                        seen.add(h)
                        time.sleep(2)
                else:
                    posted += 1
                    political_passes += 1
                    seen.add(h)
            else:
                if args.show_skipped:
                    print(f"    [censored, skip]")
                seen.add(h)
            continue

        if "_error" in a or "_parse_error" in a:
            print(f"    [analyze fail] {a}")
            continue

        if not a.get("important"):
            skipped_llm += 1
            seen.add(h)
            if args.show_skipped:
                print(f"    [skip] {a.get('reason_skip', '')}")
            continue

        text = format_post(item, a)
        print(f"    [IMPORTANT]\n{text}\n")
        if not args.dry_run:
            r = send_telegram(text)
            if r and r.get("ok"):
                posted += 1
                seen.add(h)
                time.sleep(2)
            else:
                print(f"    [tg fail] {r}")
        else:
            posted += 1
            seen.add(h)

    save_seen(seen)
    print(f"\n=== done ===\nposted: {posted} (из них political raw: {political_passes})\nskipped LLM: {skipped_llm}")


if __name__ == "__main__":
    main()
