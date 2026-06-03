import re
import io
import asyncio
import urllib.parse
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

BOT_TOKEN = "8939462900:AAHZbSeAQEnw4KYoRe-7eEoLW6K1YFgbDbs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.5",
}

JUNK_PATTERNS = [
    r'\[(?:ar|al|ti|au|length|by|re|ve|offset):[^\]]*\]',
    r'\[\d+:\d+[\.:]\d+\]',
    r'\*[^*\n]*\*',
    r'[-=~]{3,}',
    r'(?i)(keep untagged|show lyrics only|edit time|lrc time|cross media|views x|download\s*x|copied|lrc maker)[^\n]*',
    r'\d+\s*-\s*[^\n]+\n?\s*\d+ years? ago[^\n]*',
    r'(?i)by\s+guest[^\n]*',
    r'(?i)lrc tag[^\n]*',
    r'\bx\s*\d+\b',
    r'(?i)offset[^\n]*',
]

lyrics_cache: dict[str, tuple] = {}


def clean_lyrics(text: str) -> str:
    for pattern in JUNK_PATTERNS:
        text = re.sub(pattern, '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return '\n'.join(line for line in text.splitlines() if line.strip()).strip()


def make_filename(title: str, artist: str) -> str:
    name = re.sub(r'[^\w\s-]', '', f"{artist} - {title}").strip()
    return re.sub(r'\s+', '_', name) + ".txt"


def build_txt_content(title: str, artist: str, lyrics: str) -> str:
    border = "═" * 40
    return (
        f"{border}\n"
        f"  🎵 {title}\n"
        f"  🎤 {artist}\n"
        f"  📅 {datetime.now().strftime('%Y-%m-%d')}\n"
        f"{border}\n\n"
        f"{lyrics}\n\n"
        f"{border}\n"
        f"  Downloaded via @YourBot\n"
        f"{border}"
    )


async def search_megalobiz(session: aiohttp.ClientSession, query: str) -> str | None:
    async with session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": f"{query} lyrics megalobiz"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        soup = BeautifulSoup(await r.text(), "html.parser")

    for a in soup.find_all("a", class_="result__url"):
        href = a.get("href", "")
        if "megalobiz.com" in href:
            return href if href.startswith("http") else "https://" + href

    for a in soup.find_all("a", class_="result__a"):
        href = a.get("href", "")
        if "megalobiz" in href:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return params.get("uddg", [href])[0]

    return None


async def search_azlyrics(session: aiohttp.ClientSession, query: str) -> str | None:
    async with session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": f"{query} lyrics site:azlyrics.com"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        soup = BeautifulSoup(await r.text(), "html.parser")

    for a in soup.find_all("a", class_="result__a"):
        href = a.get("href", "")
        if "azlyrics.com/lyrics" in href:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return params.get("uddg", [href])[0]

    return None


async def search_genius(session: aiohttp.ClientSession, query: str) -> str | None:
    async with session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": f"{query} lyrics site:genius.com"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        soup = BeautifulSoup(await r.text(), "html.parser")

    for a in soup.find_all("a", class_="result__a"):
        href = a.get("href", "")
        if "genius.com" in href and "/lyrics" not in href.split("genius.com")[0]:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            link = params.get("uddg", [href])[0]
            if "genius.com" in link:
                return link

    return None


def extract_megalobiz(soup: BeautifulSoup) -> str | None:
    span = soup.find("span", id="lrc_text")
    if span:
        return span.get_text(separator="\n").strip()

    for ol in soup.find_all("ol"):
        items = ol.find_all("li")
        if len(items) > 5:
            lines = []
            for li in items:
                for s in li.find_all("span"):
                    s.decompose()
                t = li.get_text().strip()
                if t:
                    lines.append(t)
            if lines:
                return "\n".join(lines)

    best, best_len = None, 0
    for div in soup.find_all("div"):
        cls = " ".join(div.get("class", []))
        did = div.get("id", "")
        if "lyric" in cls.lower() or "lyric" in did.lower():
            t = div.get_text()
            if len(t) > best_len:
                best_len, best = len(t), div
    if best:
        return best.get_text(separator="\n").strip()

    pre = soup.find("pre")
    if pre:
        return pre.get_text(separator="\n").strip()

    return None


def extract_azlyrics(soup: BeautifulSoup) -> str | None:
    divs = soup.find_all("div", class_=lambda c: not c, id_=lambda i: not i)
    for div in soup.find_all("div"):
        if div.get("class") is None and div.get("id") is None:
            text = div.get_text(separator="\n").strip()
            if len(text) > 200:
                return text
    return None


def extract_genius(soup: BeautifulSoup) -> str | None:
    containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
    if containers:
        lines = []
        for container in containers:
            for br in container.find_all("br"):
                br.replace_with("\n")
            lines.append(container.get_text(separator="\n"))
        return "\n".join(lines).strip()
    return None


async def try_source(
    session: aiohttp.ClientSession,
    query: str,
    search_fn,
    extract_fn,
    source_name: str
) -> tuple[str, str, str, str] | None:
    try:
        link = await search_fn(session, query)
        if not link:
            return None

        async with session.get(link, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            soup = BeautifulSoup(await r.text(), "html.parser")

        lyrics = extract_fn(soup)
        if not lyrics or len(lyrics.strip()) < 50:
            return None

        h1 = soup.find("h1")
        title = re.sub(r'(?i)^lrc\s*', '', h1.text.strip()) if h1 else query

        artist_tag = (
            soup.find("a", class_="entity_name") or
            soup.find("a", class_="artist") or
            soup.find("span", class_="artist")
        )
        artist = artist_tag.text.strip() if artist_tag else "Unknown"

        return title, artist, lyrics, source_name

    except Exception:
        return None


async def fetch_lyrics(query: str) -> tuple[str, str, str, str] | tuple[None, None, None, None]:
    cache_key = query.lower().strip()
    if cache_key in lyrics_cache:
        return lyrics_cache[cache_key]

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        sources = [
            (search_megalobiz, extract_megalobiz, "Megalobiz"),
            (search_azlyrics, extract_azlyrics, "AZLyrics"),
            (search_genius, extract_genius, "Genius"),
        ]

        results = await asyncio.gather(*[
            try_source(session, query, sf, ef, name)
            for sf, ef, name in sources
        ])

    for result in results:
        if result:
            title, artist, lyrics, source = result
            lyrics = clean_lyrics(lyrics)
            if len(lyrics) > 100:
                final = (title, artist, lyrics, source)
                lyrics_cache[cache_key] = final
                return final

    return None, None, None, None


def build_keyboard(query: str, has_lyrics: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_lyrics:
        buttons.append([
            InlineKeyboardButton("📥 Download .txt", callback_data=f"dl:{query[:50]}"),
            InlineKeyboardButton("🔄 Retry", callback_data=f"retry:{query[:50]}"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🔄 Try Again", callback_data=f"retry:{query[:50]}"),
        ])
    return InlineKeyboardMarkup(buttons)


async def send_lyrics_message(
    update: Update,
    query: str,
    title: str,
    artist: str,
    lyrics: str,
    source: str,
    edit_msg=None
):
    header = f"🎵 <b>{title}</b>\n🎤 <i>{artist}</i>\n🌐 <code>{source}</code>\n\n"
    full = header + lyrics
    keyboard = build_keyboard(query, True)

    if len(full) <= 4096:
        if edit_msg:
            await edit_msg.edit_text(full, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(full, parse_mode="HTML", reply_markup=keyboard)
    else:
        if edit_msg:
            await edit_msg.edit_text(
                header + lyrics[:4096 - len(header)],
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(
                header + lyrics[:4096 - len(header)],
                parse_mode="HTML",
                reply_markup=keyboard
            )
        remaining = lyrics[4096 - len(header):]
        while remaining:
            chunk, remaining = remaining[:4096], remaining[4096:]
            await update.message.reply_text(chunk)


async def lyrics_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "🎵 <b>Lyrics Bot</b>\n\n"
            "❍ <b>Usage:</b>\n"
            "  /lyrics <code>song name</code>\n"
            "  /lyrics <code>song - artist</code>\n\n"
            "❍ <b>Alias:</b> /ly",
            parse_mode="HTML"
        )
        return

    query = " ".join(ctx.args)
    msg = await update.message.reply_text("🔍 Searching across sources...")

    title, artist, lyrics, source = await fetch_lyrics(query)

    if not lyrics:
        await msg.edit_text(
            "❌ <b>Lyrics not found</b>\n\n"
            "Try being more specific:\n"
            "<code>/lyrics Blinding Lights - The Weeknd</code>",
            parse_mode="HTML",
            reply_markup=build_keyboard(query, False)
        )
        return

    await send_lyrics_message(update, query, title, artist, lyrics, source, edit_msg=msg)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, search_query = query.data.split(":", 1)

    if action == "dl":
        cached = lyrics_cache.get(search_query.lower().strip())
        if not cached:
            await query.answer("❌ Cache expired. Search again.", show_alert=True)
            return

        title, artist, lyrics, source = cached
        content = build_txt_content(title, artist, lyrics)
        filename = make_filename(title, artist)

        file_obj = io.BytesIO(content.encode("utf-8"))
        file_obj.name = filename

        await query.message.reply_document(
            document=InputFile(file_obj, filename=filename),
            caption=f"🎵 <b>{title}</b> — <i>{artist}</i>",
            parse_mode="HTML"
        )

    elif action == "retry":
        msg = await query.message.reply_text("🔍 Retrying...")
        cache_key = search_query.lower().strip()
        if cache_key in lyrics_cache:
            del lyrics_cache[cache_key]

        title, artist, lyrics, source = await fetch_lyrics(search_query)

        if not lyrics:
            await msg.edit_text(
                "❌ Still not found. Try a different query.",
                reply_markup=build_keyboard(search_query, False)
            )
            return

        await send_lyrics_message(
            update.callback_query,
            search_query, title, artist, lyrics, source,
            edit_msg=msg
        )


async def inline_lyrics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return
    query = " ".join(ctx.args)
    await lyrics_cmd(update, ctx)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["lyrics", "ly"], lyrics_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("✅ Bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
