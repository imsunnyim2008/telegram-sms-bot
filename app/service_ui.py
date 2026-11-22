"""Real Telegram custom-emoji brand logos for SMS receipts.

IDs from the public 'Modern Tech & Social Brands' custom-emoji catalogue:
https://github.com/thatonearyan-sh/TG_P_EMOJIS/blob/main/custom_emojis.py

Telegram renders these as real brand artwork (not a regular Unicode emoji)
when the bot is eligible to send custom emoji. Unknown services show their
name without a misleading generic emoji or an invented logo.
"""
import re
from html import escape, unescape

# Use literal strings: Telegram custom_emoji_id values exceed JS safe integers.
APP_ICONS = {
    "telegram": "5415999505952705793",
    "whatsapp": "5418150421279572914",
    "facebook": "5418039855936472761",
    "instagram": "5418261965875217473",
    "google": "5417924815237444975",
    "gmail": "5418023732629242173",
    "discord": "5415809432175026159",
    "amazon": "5417845976817761959",
    "microsoft": "5415724735419942913",
    "tiktok": "5418329736164179616",
    "twitter": "5415921127094522010",
    "snapchat": "5415833784639589467",
    "viber": "5415581932052318798",
    "netflix": "5416013194013476378",
    "youtube": "5415920877986413820",
    "reddit": "5418288929679903607",
    "skype": "5415647013691758630",
    "wechat": "5418322958705789345",
    "line": "5416081591367666764",
    "linkedin": "5418254484042188405",
    "pinterest": "5415986968943168836",
    "tinder": "5415902139044104469",
    "zoom": "5415974431933630202",
    "slack": "5417861915441398049",
    "paypal": "5418194217061087399",
    "spotify": "5415800674736703625",
    "apple": "5418188307186093821",
    "github": "5415999961219238388",
    "chrome": "5418277440642384006",
    "steam": "5415761667843723767",
    "messenger": "5415664610172766482",
}

NAMES = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "gmail": "Gmail",
    "tiktok": "TikTok",
    "discord": "Discord",
    "youtube": "YouTube",
    "microsoft": "Microsoft",
    "google": "Google",
    "twitter": "Twitter / X",
    "x": "X",
    "snapchat": "Snapchat",
    "openai": "OpenAI",
    "wechat": "WeChat",
    "paypal": "PayPal",
    "github": "GitHub",
    "linkedin": "LinkedIn",
    "netflix": "Netflix",
}
ALIASES = {"x": "twitter", "fb": "facebook", "ig": "instagram",
           "wa": "whatsapp", "yt": "youtube", "tg": "telegram"}

# Telegram only accepts a genuine Unicode emoji inside the tag. This is a
# fallback for clients without custom emoji support, not the visible logo.
_EMOJI_FALLBACK = "📱"
_CUSTOM_EMOJI_TAG = re.compile(
    r'<tg-emoji emoji-id="[0-9]{15,22}">[^<]*</tg-emoji>\s*'
)


def service_key(service: str) -> str:
    return re.sub(r"[\s_-]+", "", str(service or "").strip().lower())


def app_label(service: str) -> str:
    """Plain app name, safe for logs, notifications, and inaccessible clients."""
    key = service_key(service)
    if key in NAMES:
        return NAMES[key]
    value = str(service or "").strip().replace("_", " ").replace("-", " ")
    return value.title() if value else "Unknown service"


def app_label_html(service: str) -> str:
    """Telegram HTML entity showing the authentic branded custom-emoji logo."""
    key = ALIASES.get(service_key(service), service_key(service))
    name = escape(app_label(service))
    if emoji_id := APP_ICONS.get(key):
        return f'<tg-emoji emoji-id="{emoji_id}">{_EMOJI_FALLBACK}</tg-emoji> {name}'
    return name


def without_custom_emoji(html_text: str) -> str:
    """Text-only fallback when Telegram refuses branded custom emoji entities."""
    return unescape(_CUSTOM_EMOJI_TAG.sub("", html_text))


def custom_emoji_unavailable(error: Exception) -> bool:
    """Avoid repeating a message on unrelated Bot API errors."""
    text = str(error).lower()
    return any(part in text for part in (
        "custom emoji", "custom_emoji", "entity_text_invalid",
        "can't parse entities", "premium",
    ))
