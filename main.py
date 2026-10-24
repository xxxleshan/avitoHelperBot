"""
Telegram-бот: присылает новые объявления с Авито по сохранённым поискам.
 
Пользователь настраивает фильтры прямо на avito.ru (город, запрос, цена и т.д.),
копирует URL выдачи и отправляет боту: /add <url>.
Фоновый цикл периодически запрашивает выдачу (отсортированную по дате),
находит новые ID и отправляет их в чат с фото и ссылкой.
 
Запуск:
    pip install -r requirements.txt
    cp .env.example .env    # и впишите туда BOT_TOKEN (и при желании PROXY)
    python bot.py
"""
 
import asyncio
import html
import logging
import os
import random
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
 
import aiosqlite
import httpx
from typing import Any, Awaitable, Callable
 
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message, TelegramObject
from aiogram.utils.media_group import MediaGroupBuilder
from bs4 import BeautifulSoup
from dotenv import load_dotenv
 
load_dotenv()  # подхватывает переменные из .env рядом с ботом
 
BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY = os.getenv("PROXY")                       # None — без прокси
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "90"))  # пауза между кругами, сек
DB_PATH = os.getenv("DB_PATH", "avito_bot.db")
MAX_SUBS_PER_USER = 5
# Кому разрешено пользоваться ботом: один ID или несколько через запятую
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_ID", "").replace(" ", "").split(",") if x
}
if not ALLOWED_USER_IDS:
    raise SystemExit("Укажите ALLOWED_USER_ID в .env (свой ID можно узнать у @userinfobot)")
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito-bot")
 
class AccessMiddleware(BaseMiddleware):
    """Пропускает к хендлерам только разрешённых пользователей, остальных молча игнорирует."""
 
    def __init__(self, allowed: set[int]) -> None:
        self.allowed = allowed
 
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            if user is not None:
                log.info("Отклонён доступ: %s (@%s)", user.id, user.username)
            return None
        return await handler(event, data)
 
 
dp = Dispatcher()
dp.update.outer_middleware(AccessMiddleware(ALLOWED_USER_IDS))
db: aiosqlite.Connection
 
 
# ---------- БД ----------
 
async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS subs (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            url     TEXT    NOT NULL,
            primed  INTEGER NOT NULL DEFAULT 0   -- 0: первый проход, ничего не шлём
        );
        CREATE TABLE IF NOT EXISTS seen (
            sub_id  INTEGER NOT NULL,
            item_id TEXT    NOT NULL,
            price   INTEGER,
            PRIMARY KEY (sub_id, item_id)
        );
        """
    )
    # миграция для старой БД без колонки price
    async with db.execute("PRAGMA table_info(seen)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if "price" not in columns:
        await db.execute("ALTER TABLE seen ADD COLUMN price INTEGER")
    await db.commit()
 
 
# ---------- Авито ----------
 
def normalize_url(raw: str) -> str | None:
    """Проверяет, что это выдача Авито, и включает сортировку по дате (s=104)."""
    try:
        u = urlparse(raw.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc.endswith("avito.ru"):
        return None
    qs = parse_qs(u.query)
    qs["s"] = ["104"]          # сортировка «по дате»
    qs.pop("p", None)          # всегда первая страница
    return urlunparse(u._replace(scheme="https", query=urlencode(qs, doseq=True)))
 
 
def parse_listing(html: str) -> list[dict]:
    """Разбирает карточки выдачи. Селекторы data-marker могут меняться — проверяйте."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    for card in soup.select('[data-marker="item"]'):
        item_id = card.get("data-item-id")
        link = card.select_one('[data-marker="item-title"]')
        if not item_id or not link or not link.get("href"):
            continue
        price_tag = card.select_one('[itemprop="price"]')
        price = price_tag.get("content") if price_tag else None
        photos = []
        for img in card.select('[data-marker="item-photo"] img'):
            srcset = img.get("srcset")
            src = srcset.split(",")[-1].split()[0] if srcset else img.get("src")
            if src and src.startswith("http"):
                photos.append(src)
        desc_tag = card.select_one('meta[itemprop="description"]')
        items.append(
            {
                "id": item_id,
                "title": link.get("title") or link.get_text(strip=True),
                "url": f"https://www.avito.ru/{item_id}",
                "price_value": int(price) if price and price.isdigit() else None,
                "price": format_price(int(price)) if price and price.isdigit() else "цена не указана",
                "photos": photos[:3],
                "address": text_of(card, '[data-marker="item-address"]'),
                "description": desc_tag.get("content", "").strip() if desc_tag else "",
                "date": text_of(card, '[data-marker="item-date"]'),  # «2 часа назад», уточним со страницы
                "rating": None,
                "reviews": None,
                "completed": None,
            }
        )
    return items
 
 
def format_price(value: int) -> str:
    return f"{value:,} ₽".replace(",", " ")
 
 
def text_of(root, *selectors: str) -> str:
    """Текст первого найденного элемента из списка селекторов."""
    for sel in selectors:
        tag = root.select_one(sel)
        if tag:
            text = tag.get_text(" ", strip=True)
            if text:
                return text
    return ""
 
 
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
 
 
def parse_avito_date(raw: str) -> str:
    """«· 1 октября в 19:51» / «сегодня в 19:51» → «01.10.2026 19:51»."""
    text = raw.lower().replace("\xa0", " ")
    m = re.search(r"(сегодня|вчера|(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?)\s+в\s+(\d{1,2}):(\d{2})", text)
    if not m:
        return raw.strip(" ·")
    today = datetime.now().date()
    if m.group(1) == "сегодня":
        d = today
    elif m.group(1) == "вчера":
        d = today - timedelta(days=1)
    else:
        month = MONTHS.get(m.group(3))
        if not month:
            return raw.strip(" ·")
        d = date(int(m.group(4) or today.year), month, int(m.group(2)))
    return f"{d:%d.%m.%Y} {int(m.group(5)):02d}:{m.group(6)}"
 
 
async def fetch_details(client: httpx.AsyncClient, item: dict) -> None:
    """Догружает со страницы объявления полное описание, точную дату, адрес и рейтинг продавца.
    Селекторы data-marker взяты по вёрстке Авито — при изменении сайта их нужно поправить."""
    html_page = await fetch(client, item["url"])
    if html_page is None:
        return
    soup = BeautifulSoup(html_page, "lxml")
 
    desc = soup.select_one('[data-marker="item-view/item-description"]') or soup.select_one('[itemprop="description"]')
    if desc:
        item["description"] = desc.get_text("\n", strip=True)
 
    raw_date = text_of(soup, '[data-marker="item-view/item-date"]')
    if raw_date:
        item["date"] = parse_avito_date(raw_date)
 
    address = text_of(soup, '[data-marker="item-view/item-address"]', '[itemprop="address"]')
    if address:
        item["address"] = address
 
    seller = (
        soup.select_one('[data-marker="item-view/seller-info"]')
        or soup.select_one('[data-marker="seller-info"]')
        or soup.select_one('[data-marker^="seller-info"]')
    )
    if seller:
        seller_text = seller.get_text(" ", strip=True).replace("\xa0", " ")
        score = text_of(seller, '[data-marker="seller-info/score"]', '[data-marker="seller-rating/score"]')
        m = re.search(r"\b([0-5][,.]\d)\b", score or seller_text)
        item["rating"] = m.group(1).replace(",", ".") if m else None
        m = re.search(r"\d+\s+отзыв\w*", seller_text)
        item["reviews"] = m.group(0) if m else None
        m = re.search(r"\d+\s+заверш\w*\s+объявлени\w*", seller_text)
        item["completed"] = m.group(0) if m else None
 
 
class RateLimited(Exception):
    """Авито ограничил частоту запросов (429/439)."""
 
 
async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    r = await client.get(url)
    if r.status_code in (429, 439):
        raise RateLimited(r.status_code)
    if r.status_code != 200:
        log.warning("Авито вернул %s для %s", r.status_code, url)
        return None
    return r.text
 
 
# ---------- Отправка ----------
 
async def download_photos(client: httpx.AsyncClient, item: dict) -> list[BufferedInputFile]:
    """Качаем фото сами: Telegram не может забрать их с CDN Авито по ссылке."""
    files = []
    for n, url in enumerate(item["photos"]):
        try:
            r = await client.get(url, headers={"Referer": "https://www.avito.ru/"})
            if r.status_code == 200 and r.content:
                files.append(BufferedInputFile(r.content, filename=f"{item['id']}_{n}.jpg"))
        except httpx.HTTPError as e:
            log.warning("Не скачалось фото %s: %s", url, e)
    return files
 
 
CAPTION_LIMIT = 1024  # лимит подписи к фото в Telegram
 
 
def plain_len(s: str) -> int:
    """Длина текста так, как её считает Telegram (без HTML-тегов)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))
 
 
def format_caption(item: dict) -> str:
    e = html.escape
    head = [f"<b>{e(item['title'])}</b>", f"💵 {e(item['price'])}"]
    if item.get("address"):
        head.append(f"🌍 {e(item['address'])}")
    head.append(f"🔍 {item['url']}")
 
    seller = []
    if item.get("rating"):
        seller.append(f"{item['rating']} ⭐")
    for key in ("reviews", "completed"):
        if item.get(key):
            seller.append(e(item[key]))
 
    blocks_after = []
    if seller:
        blocks_after.append("\n".join(seller))
    if item.get("date"):
        blocks_after.append(f"📅 {e(item['date'])}")
 
    head_text = "\n".join(head)
    tail_text = "\n\n".join(blocks_after)
    desc = (item.get("description") or "").strip()
    if desc:
        budget = CAPTION_LIMIT - plain_len(head_text) - plain_len(tail_text) - 10
        if budget > 20:
            if len(desc) > budget:
                desc = desc[: budget - 1].rstrip() + "…"
            head_text += f"\n\n<blockquote expandable>{e(desc)}</blockquote>"
    return "\n\n".join(x for x in (head_text, tail_text) if x)
 
 
async def send_item(bot: Bot, client: httpx.AsyncClient, chat_id: int, item: dict) -> None:
    caption = format_caption(item)
    try:
        photos = await download_photos(client, item)
        if photos:
            mg = MediaGroupBuilder(caption=caption)
            for p in photos:
                mg.add_photo(media=p, parse_mode="HTML")
            await bot.send_media_group(chat_id, media=mg.build())
        else:
            await bot.send_message(chat_id, caption, parse_mode="HTML")
    except Exception as e:  # например, Telegram не смог скачать фото
        log.warning("Фото не отправились (%s), шлю текстом", e)
        await bot.send_message(chat_id, caption, parse_mode="HTML")
 
 
# ---------- Фоновый опрос ----------
 
async def check_sub(bot: Bot, client: httpx.AsyncClient, sub_id: int, chat_id: int, url: str, primed: int) -> None:
    html = await fetch(client, url)
    if html is None:
        return
    items = parse_listing(html)
    new_items = []
    price_changes = []  # (item, старая цена)
    for item in items:
        async with db.execute(
            "SELECT price FROM seen WHERE sub_id = ? AND item_id = ?", (sub_id, item["id"])
        ) as cur:
            row = await cur.fetchone()
        new_price = item["price_value"]
        if row is None:
            await db.execute(
                "INSERT INTO seen (sub_id, item_id, price) VALUES (?, ?, ?)", (sub_id, item["id"], new_price)
            )
            new_items.append(item)
        elif new_price is not None and row[0] != new_price:
            await db.execute(
                "UPDATE seen SET price = ? WHERE sub_id = ? AND item_id = ?", (new_price, sub_id, item["id"])
            )
            if row[0] is not None:  # у старых записей цены могло не быть — просто запоминаем
                price_changes.append((item, row[0]))
    if not primed:
        await db.execute("UPDATE subs SET primed = 1 WHERE id = ?", (sub_id,))
        await db.commit()
        log.info("Подписка %s: запомнено %s текущих объявлений", sub_id, len(items))
        return
    await db.commit()
    details_allowed = True
    for item in reversed(new_items):  # от старых к новым
        if details_allowed:
            await asyncio.sleep(random.uniform(2, 5))
            try:
                await fetch_details(client, item)
            except RateLimited:
                details_allowed = False  # остальные отправим с данными из выдачи
                log.warning("Лимит при загрузке карточки %s, шлю без деталей", item["id"])
            except httpx.HTTPError as e:
                log.warning("Не удалось загрузить карточку %s: %s", item["id"], e)
        await send_item(bot, client, chat_id, item)
        await asyncio.sleep(0.5)
    for item, old_price in price_changes:
        arrow = "📉" if item["price_value"] < old_price else "📈"
        text = (
            f"{arrow} <b>Новая цена: {format_price(item['price_value'])}</b> (было {format_price(old_price)})\n"
            f"{html.escape(item['title'])}\n{item['url']}"
        )
        await bot.send_message(chat_id, text, parse_mode="HTML")
        await asyncio.sleep(0.5)
 
 
async def poll_loop(bot: Bot) -> None:
    async with httpx.AsyncClient(headers=HEADERS, proxy=PROXY, timeout=20, follow_redirects=True) as client:
        cooldown = 0  # растёт при блокировках, сбрасывается после успешного круга
        while True:
            async with db.execute("SELECT id, chat_id, url, primed FROM subs") as cur:
                subs = await cur.fetchall()
            limited = False
            for sub in subs:
                try:
                    await check_sub(bot, client, *sub)
                except RateLimited as e:
                    limited = True
                    log.warning("Авито ограничил запросы (%s), прерываю круг", e)
                    break
                except httpx.TimeoutException:
                    log.warning("Таймаут при проверке подписки %s", sub[0])
                except Exception:
                    log.exception("Ошибка при проверке подписки %s", sub[0])
                await asyncio.sleep(random.uniform(3, 8))  # не долбим Авито подряд
            if limited:
                cooldown = min(cooldown * 2 or 300, 1800)  # 5 → 10 → 20 → 30 мин
                log.info("Пауза %s сек из-за ограничения", cooldown)
                await asyncio.sleep(cooldown)
            else:
                cooldown = 0
                await asyncio.sleep(POLL_INTERVAL + random.uniform(0, 30))
 
 
# ---------- Команды ----------
 
HELP = (
    "Настройте поиск на avito.ru (город, запрос, цена), скопируйте ссылку из адресной строки и пришлите:\n"
    "/add <ссылка> — следить за поиском\n"
    "/list — мои поиски\n"
    "/del <номер> — удалить поиск"
)
 
 
@dp.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)
 
 
@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    url = normalize_url(command.args or "")
    if not url:
        await message.answer("Нужна ссылка на выдачу Авито, например:\n/add https://www.avito.ru/saratov?q=iphone")
        return
    async with db.execute("SELECT COUNT(*) FROM subs WHERE chat_id = ?", (message.chat.id,)) as cur:
        (count,) = await cur.fetchone()
    if count >= MAX_SUBS_PER_USER:
        await message.answer(f"Максимум {MAX_SUBS_PER_USER} поисков. Удалите лишний через /del.")
        return
    cur = await db.execute("INSERT INTO subs (chat_id, url) VALUES (?, ?)", (message.chat.id, url))
    await db.commit()
    await message.answer(f"Поиск №{cur.lastrowid} добавлен. Буду присылать новые объявления.")
 
 
@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with db.execute("SELECT id, url FROM subs WHERE chat_id = ?", (message.chat.id,)) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Поисков пока нет. " + HELP)
        return
    await message.answer("\n".join(f"№{i}: {u}" for i, u in rows), disable_web_page_preview=True)
 
 
@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Укажите номер: /del 3")
        return
    sub_id = int(command.args.strip())
    cur = await db.execute("DELETE FROM subs WHERE id = ? AND chat_id = ?", (sub_id, message.chat.id))
    if cur.rowcount:
        await db.execute("DELETE FROM seen WHERE sub_id = ?", (sub_id,))
        await db.commit()
        await message.answer(f"Поиск №{sub_id} удалён.")
    else:
        await message.answer("Такого поиска нет.")
 
 
# ---------- Запуск ----------
 
async def main() -> None:
    await init_db()
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(poll_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
 
 
if __name__ == "__main__":
    asyncio.run(main())
 