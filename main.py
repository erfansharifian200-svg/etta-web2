import requests
from bs4 import BeautifulSoup
import os
import re
import json
import time
import hashlib

CHANNEL = os.getenv("EITAA_CHANNEL")
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if cid.strip()]
LIMIT = 10
STATE_FILE = "state.json"
MAX_STATE_ITEMS = 200

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


# ---------- استخراج از HTML ----------

def extract_bg_url(style):
    if not style:
        return None
    m = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style)
    return m.group(1) if m else None


def message_id(msg):
    """یه شناسه یکتا برای هر پست می‌سازه تا بشه تشخیص داد قبلا فرستاده شده یا نه"""
    if msg.get("post_url"):
        return msg["post_url"]
    raw = msg["text"] + "".join(msg["photos"] + msg["videos"] + msg["gifs"] + msg["stickers"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_post(post, channel):
    text_div = post.find("div", class_=re.compile(r"message_text"))
    text = text_div.get_text(separator="\n", strip=True) if text_div else ""

    post_url = ""
    date_link = post.find("a", class_=re.compile(r"message_date"))
    if date_link and date_link.get("href"):
        href = date_link["href"]
        post_url = "https://eitaa.com" + href if href.startswith("/") else href
    if not post_url:
        link_tag = post.find("a", href=True)
        if link_tag and channel in link_tag.get("href", ""):
            href = link_tag["href"]
            post_url = "https://eitaa.com" + href if href.startswith("/") else href

    photos, videos, gifs, stickers = [], [], [], []
    documents = []

    # عکس‌ها (تکی و آلبومی) - هر تگی که کلاسش شامل photo_wrap باشه
    for tag in post.find_all(class_=re.compile(r"photo_wrap")):
        bg = extract_bg_url(tag.get("style", ""))
        if bg and bg not in photos:
            photos.append(bg)

    # ویدیو و گیف
    for wrap in post.find_all(class_=re.compile(r"video_player")):
        video_tag = wrap.find("video")
        src = None
        if video_tag:
            src = video_tag.get("src")
            if not src:
                source_tag = video_tag.find("source")
                src = source_tag.get("src") if source_tag else None
        if not src:
            continue
        wrap_classes = " ".join(wrap.get("class", [])).lower()
        is_gif = "gif" in wrap_classes or (
            video_tag is not None and video_tag.get("loop") is not None and video_tag.get("autoplay") is not None
        )
        target = gifs if is_gif else videos
        if src not in target:
            target.append(src)

    # استیکر
    for wrap in post.find_all(class_=re.compile(r"sticker_wrap")):
        img = wrap.find("img")
        vid = wrap.find("video")
        src = (img.get("src") if img else None) or (vid.get("src") if vid else None)
        if src and src not in stickers:
            stickers.append(src)

    # فایل‌ها / داکیومنت‌ها
    for wrap in post.find_all(class_=re.compile(r"document")):
        a_tag = wrap if wrap.name == "a" else wrap.find("a", href=True)
        if not a_tag or not a_tag.get("href"):
            continue
        href = a_tag["href"]
        title_tag = wrap.find(class_=re.compile(r"document_title"))
        name = title_tag.get_text(strip=True) if title_tag else None
        if not any(d["url"] == href for d in documents):
            documents.append({"url": href, "name": name})

    # فایل‌های ساده که فقط با لینک با پسوند مشخص هستن (fallback)
    for a in post.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(pdf|zip|rar|7z|docx?|xlsx?|pptx?|apk|mp3|ogg|txt)(\?.*)?$", href, re.I):
            if not any(d["url"] == href for d in documents):
                documents.append({"url": href, "name": None})

    return {
        "text": text,
        "post_url": post_url,
        "photos": photos,
        "videos": videos,
        "gifs": gifs,
        "stickers": stickers,
        "documents": documents,
    }


def get_latest_messages(channel):
    url = f"https://eitaa.com/{channel}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        posts = soup.find_all("div", class_="etme_widget_message")

        # ---- DEBUG: یه پست عکس‌دار رو کامل چاپ می‌کنه ----
        for p in posts[-LIMIT:]:
            if "photo" in str(p).lower():
                print("---DEBUG PHOTO POST START---")
                print(str(p)[:2500])
                print("---DEBUG PHOTO POST END---")
                break
        else:
            print("---DEBUG: هیچ پستی با کلمه‌ی photo در HTML پیدا نشد---")
        # ---- پایان DEBUG ----

        messages = [parse_post(post, channel) for post in posts[-LIMIT:]]
        return [m for m in messages if m["text"].strip() or m["photos"] or m["videos"]
                or m["gifs"] or m["stickers"] or m["documents"]]
    except Exception as e:
        print(f"خطا در دریافت پیام‌ها: {e}")
        return []


# ---------- ارسال به تلگرام ----------

def download_file(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"خطا در دانلود {url}: {e}")
        return None


def send_text(text, chat_id):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text[:4090], "disable_web_page_preview": False}
    try:
        return requests.post(url, data=data, timeout=30).status_code == 200
    except Exception as e:
        print(f"خطا در ارسال متن به {chat_id}: {e}")
        return False


def send_media(chat_id, media_type, file_bytes, filename, caption=""):
    method_map = {
        "photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument",
        "animation": "sendAnimation", "sticker": "sendSticker",
    }
    url = f"https://api.telegram.org/bot{TOKEN}/{method_map[media_type]}"
    data = {"chat_id": chat_id}
    if media_type != "sticker":
        data["caption"] = caption[:1024]
    files = {media_type: (filename, file_bytes)}
    try:
        resp = requests.post(url, data=data, files=files, timeout=120)
        if resp.status_code == 200:
            return True
        if media_type == "sticker":
            return send_media(chat_id, "photo", file_bytes, filename, caption) or \
                   send_media(chat_id, "document", file_bytes, filename, caption)
        print(f"خطا در ارسال {media_type} به {chat_id}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"خطا در ارسال {media_type} به {chat_id}: {e}")
        return False


def send_message(msg, chat_id):
    full_text = f"{msg['text']}\n\n🔗 {msg['post_url']}" if msg["post_url"] else msg["text"]
    caption = full_text[:1024]
    caption_used = False
    any_sent = False

    media_jobs = (
        [("photo", u, f"photo_{i}.jpg") for i, u in enumerate(msg["photos"])]
        + [("animation", u, f"gif_{i}.mp4") for i, u in enumerate(msg["gifs"])]
        + [("video", u, f"video_{i}.mp4") for i, u in enumerate(msg["videos"])]
    )

    for media_type, url, default_name in media_jobs:
        content = download_file(url)
        if content:
            cap = caption if not caption_used else ""
            if send_media(chat_id, media_type, content, default_name, cap):
                caption_used = True
                any_sent = True
            time.sleep(1)

    for i, sticker_url in enumerate(msg["stickers"]):
        content = download_file(sticker_url)
        if content:
            ext = "webp" if sticker_url.lower().endswith(".webp") else "png"
            if send_media(chat_id, "sticker", content, f"sticker_{i}.{ext}", ""):
                any_sent = True
            time.sleep(1)

    for i, doc in enumerate(msg["documents"]):
        content = download_file(doc["url"])
        if content:
            filename = doc["name"] or doc["url"].split("/")[-1].split("?")[0] or f"file_{i}"
            cap = caption if not caption_used else ""
            if send_media(chat_id, "document", content, filename, cap):
                caption_used = True
                any_sent = True
            time.sleep(1)

    if not caption_used and full_text.strip():
        any_sent = send_text(full_text, chat_id) or any_sent

    return any_sent


# ---------- وضعیت (جلوگیری از ارسال تکراری) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_state(sent_ids):
    ids = list(sent_ids)[-MAX_STATE_ITEMS:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False)


# ---------- اجرا ----------

if __name__ == "__main__":
    if not all([CHANNEL, TOKEN]) or not CHAT_IDS:
        print("خطا: متغیرهای محیطی تنظیم نشده‌اند")
        exit(1)

    print(f"در حال دریافت پیام‌های کانال {CHANNEL}...")
    sent_before = load_state()
    msgs = get_latest_messages(CHANNEL)
    new_msgs = [m for m in msgs if message_id(m) not in sent_before]

    print(f"پیام‌های جدید: {len(new_msgs)} از {len(msgs)} پیام دریافتی")

    for msg in new_msgs:
        mid = message_id(msg)
        ok_any = False
        for chat_id in CHAT_IDS:
            ok = send_message(msg, chat_id)
            ok_any = ok_any or ok
            print(f"پیام {mid[:12]}... به {chat_id} ارسال شد: {ok}")
        if ok_any:
            sent_before.add(mid)
        time.sleep(1.2)

    save_state(sent_before)
    print("تمام.")
