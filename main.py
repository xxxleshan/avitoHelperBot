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
import logging
import os
import random
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
 
import aiosqlite
import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.media_group import MediaGroupBuilder
from bs4 import BeautifulSoup
from dotenv import load_dotenv
 
load_dotenv()  # подхватывает переменные из .env рядом с ботом
 
BOT_TOKEN = os.environ["BOT_TOKEN"]
PROXY = os.getenv("PROXY")                       # None — без прокси
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "90"))  # пауза между кругами, сек
DB_PATH = os.getenv("DB_PATH", "avito_bot.db")
MAX_SUBS_PER_USER = 5
 
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito-bot")
 
dp = Dispatcher()
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
            PRIMARY KEY (sub_id, item_id)
        );
        """
    )
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
        items.append(
            {
                "id": item_id,
                "title": link.get("title") or link.get_text(strip=True),
                "url": "https://www.avito.ru" + link["href"].split("?")[0],
                "price": f"{int(price):,} ₽".replace(",", " ") if price and price.isdigit() else "цена не указана",
                "photos": photos[:3],
            }
        )
    return items
 
 
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
 
 
async def send_item(bot: Bot, client: httpx.AsyncClient, chat_id: int, item: dict) -> None:
    caption = f"<b>{item['title']}</b>\n{item['price']}\n{item['url']}"
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
    for item in items:
        cur = await db.execute(
            "INSERT OR IGNORE INTO seen (sub_id, item_id) VALUES (?, ?)", (sub_id, item["id"])
        )
        if cur.rowcount:
            new_items.append(item)
    if not primed:
        await db.execute("UPDATE subs SET primed = 1 WHERE id = ?", (sub_id,))
        await db.commit()
        log.info("Подписка %s: запомнено %s текущих объявлений", sub_id, len(items))
        return
    await db.commit()
    for item in reversed(new_items):  # от старых к новым
        await send_item(bot, client, chat_id, item)
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