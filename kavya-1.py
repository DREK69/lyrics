import re
import io
import os
import asyncio
import urllib.parse
from datetime import datetime
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest


BOT_TOKEN = "8939462900:AAHZbSeAQEnw4KYoRe-7eEoLW6K1YFgbDbs"

DL_API_URL = "https://archtubedl-3942279d8b2a.herokuapp.com/download/video"
DL_API_KEY = "874VBAMkVtSoZfKHy4YnTK7GnCgqU48GwhbXwRmIGrQ"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.5",
}

YT_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+")

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


@dataclass
class LyricsResult:
    title: str
    artist: str
    lyrics: str
    source: str


def clean_lyrics(text: str) -> str:
    for p in JUNK_PATTERNS:
        text = re.sub(p, "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(l for l in text.splitlines() if l.strip()).strip()


def safe_filename(title: str, artist: str) -> str:
    raw = re.sub(r"[^\w\s-]", "", f"{artist} - {title}").strip()
    return re.sub(r"\s+", "_", raw) + ".txt"


def txt_content(r: LyricsResult) -> str:
    bar = "═" * 40
    return (
        f"{bar}\n"
        f"  🎵 {r.title}\n"
        f"  🎤 {r.artist}\n"
        f"  📅 {datetime.now().strftime('%Y-%m-%d')}\n"
        f"{bar}\n\n"
        f"{r.lyrics}\n\n"
        f"{bar}\n"
        f"  @KavyaBot\n"
        f"{bar}"
    )


async def ddg(session: aiohttp.ClientSession, q: str) -> BeautifulSoup:
    async with session.post(
        "https://html.duckduckgo.com/html/",
        data={"q": q},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        return BeautifulSoup(await resp.text(), "html.parser")


def ddg_first(soup: BeautifulSoup, domain: str) -> str | None:
    for a in soup.find_all("a", class_="result__url"):
        h = a.get("href", "")
        if domain in h:
            return h if h.startswith("http") else "https://" + h
    for a in soup.find_all("a", class_="result__a"):
        h = a.get("href", "")
        if domain in h:
            p = urllib.parse.parse_qs(urllib.parse.urlparse(h).query)
            return p.get("uddg", [h])[0]
    return None


async def search_megalobiz(s, q): return ddg_first(await ddg(s, f"{q} lyrics megalobiz"), "megalobiz.com")
async def search_azlyrics(s, q): return ddg_first(await ddg(s, f"{q} lyrics site:azlyrics.com"), "azlyrics.com/lyrics")
async def search_genius(s, q):
    url = ddg_first(await ddg(s, f"{q} lyrics site:genius.com"), "genius.com")
    return None if url and "romanized" in url.lower() else url


def extract_megalobiz(soup: BeautifulSoup) -> str | None:
    if (span := soup.find("span", id="lrc_text")):
        return span.get_text(separator="\n").strip()

    for ol in soup.find_all("ol"):
        items = ol.find_all("li")
        if len(items) > 5:
            lines = []
            for li in items:
                for s in li.find_all("span"):
                    s.decompose()
                if t := li.get_text().strip():
                    lines.append(t)
            if lines:
                return "\n".join(lines)

    best, best_len = None, 0
    for div in soup.find_all("div"):
        cls = " ".join(div.get("class", []))
        did = div.get("id", "")
        if "lyric" in cls.lower() or "lyric" in did.lower():
            if len(t := div.get_text()) > best_len:
                best_len, best = len(t), div
    if best:
        return best.get_text(separator="\n").strip()

    if pre := soup.find("pre"):
        return pre.get_text(separator="\n").strip()
    return None


def extract_azlyrics(soup: BeautifulSoup) -> str | None:
    for div in soup.find_all("div"):
        if div.get("class") is None and div.get("id") is None:
            if len(text := div.get_text(separator="\n").strip()) > 200:
                return text
    return None


def extract_genius(soup: BeautifulSoup) -> str | None:
    containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
    if not containers:
        return None
    lines = []
    for c in containers:
        for br in c.find_all("br"):
            br.replace_with("\n")
        lines.append(c.get_text(separator="\n"))
    return "\n".join(lines).strip()


SOURCES = [
    (search_megalobiz, extract_megalobiz, "Megalobiz"),
    (search_azlyrics, extract_azlyrics, "AZLyrics"),
    (search_genius, extract_genius, "Genius"),
]


async def try_source(session, query, search_fn, extract_fn, name) -> LyricsResult | None:
    try:
        if not (link := await search_fn(session, query)):
            return None
        async with session.get(link, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            soup = BeautifulSoup(await r.text(), "html.parser")

        if not (raw := extract_fn(soup)) or len(raw.strip()) < 50:
            return None

        h1 = soup.find("h1")
        title = re.sub(r"(?i)^lrc\s*", "", h1.text.strip()) if h1 else query
        atag = soup.find("a", class_="entity_name") or soup.find("a", class_="artist") or soup.find("span", class_="artist")
        artist = atag.text.strip() if atag else "Unknown"
        return LyricsResult(title, artist, raw, name)
    except Exception:
        return None


async def fetch_lyrics(query: str) -> LyricsResult | None:
    key = query.lower().strip()
    if key in lyrics_cache:
        return lyrics_cache[key]

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        results = await asyncio.gather(*[
            try_source(session, query, sf, ef, name)
            for sf, ef, name in SOURCES
        ])

    for result in results:
        if result:
            result.lyrics = clean_lyrics(result.lyrics)
            if len(result.lyrics) > 100:
                lyrics_cache[key] = result
                return result
    return None


async def download_yt(url: str) -> bytes | None:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            DL_API_URL,
            params={"url": url},
            headers={"ArchFreeYt": DL_API_KEY},
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            return await resp.read() if resp.status == 200 else None


def lyrics_keyboard(query: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Download .txt", callback_data=f"dl:{query[:50]}"),
        InlineKeyboardButton("🔄 Retry", callback_data=f"retry:{query[:50]}"),
    ]])


def notfound_keyboard(query: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Try Again", callback_data=f"retry:{query[:50]}"),
    ]])


def format_header(r: LyricsResult) -> str:
    return f"🎵 <b>{r.title}</b>\n🎤 <i>{r.artist}</i>\n🌐 <code>{r.source}</code>\n\n"


async def send_lyrics(update: Update, query: str, result: LyricsResult, edit_msg=None):
    header = format_header(result)
    full = header + result.lyrics
    kb = lyrics_keyboard(query)

    send = edit_msg.edit_text if edit_msg else update.message.reply_text

    if len(full) <= 4096:
        await send(full, parse_mode="HTML", reply_markup=kb)
        return

    truncated = header + result.lyrics[:4096 - len(header)]
    await send(truncated, parse_mode="HTML", reply_markup=kb)

    remaining = result.lyrics[4096 - len(header):]
    while remaining:
        chunk, remaining = remaining[:4096], remaining[4096:]
        await update.message.reply_text(chunk)


async def song_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip() if ctx.args else ""

    if not query:
        await update.message.reply_html(
            "🎵 <b>Usage</b>\n\n"
            "/song <code>song name</code>\n"
            "/song <code>song - artist</code>\n"
            "/song <code>youtube_url</code>\n\n"
            "<b>Alias:</b> /ly"
        )
        return

    if YT_RE.match(query):
        msg = await update.message.reply_text("⬇️ Downloading...")
        data = await download_yt(query)
        if not data:
            await msg.edit_text("❌ Download failed. Try again later.")
            return
        fname = "video.mp4"
        with open(fname, "wb") as f:
            f.write(data)
        await msg.edit_text("📤 Uploading...")
        await update.message.reply_video(fname, caption="✅ Done")
        os.remove(fname)
        await msg.delete()
        return

    msg = await update.message.reply_text("🔍 Searching across sources...")
    result = await fetch_lyrics(query)

    if not result:
        await msg.edit_text(
            "❌ <b>Lyrics not found</b>\n\nTry: <code>/song Blinding Lights - The Weeknd</code>",
            parse_mode="HTML",
            reply_markup=notfound_keyboard(query),
        )
        return

    await send_lyrics(update, query, result, edit_msg=msg)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()

    action, query = cq.data.split(":", 1)
    key = query.lower().strip()

    if action == "dl":
        result = lyrics_cache.get(key)
        if not result:
            await cq.answer("❌ Cache expired. Search again.", show_alert=True)
            return
        content = txt_content(result)
        fname = safe_filename(result.title, result.artist)
        buf = io.BytesIO(content.encode("utf-8"))
        buf.name = fname
        await cq.message.reply_document(
            document=InputFile(buf, filename=fname),
            caption=f"🎵 <b>{result.title}</b> — <i>{result.artist}</i>",
            parse_mode="HTML",
        )

    elif action == "retry":
        lyrics_cache.pop(key, None)
        msg = await cq.message.reply_text("🔍 Retrying...")
        result = await fetch_lyrics(query)
        if not result:
            await msg.edit_text(
                "❌ Still not found. Try a different query.",
                reply_markup=notfound_keyboard(query),
            )
            return
        try:
            await send_lyrics(update.callback_query, query, result, edit_msg=msg)
        except BadRequest:
            await send_lyrics(update.callback_query, query, result)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["song", "ly"], song_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("✅ Kavya Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
