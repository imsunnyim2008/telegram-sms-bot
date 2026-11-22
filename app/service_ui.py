"""Friendly service/app names for SMS order receipts and status messages.

The provider's service identifier remains unchanged for billing and API calls.
These labels are only displayed to the user.
"""

_SERVICE_NAMES = {
    'telegram': '✈️ Telegram',
    'whatsapp': '💬 WhatsApp',
    'facebook': '📘 Facebook',
    'instagram': '📸 Instagram',
    'google': '🌐 Google',
    'gmail': '✉️ Gmail',
    'discord': '🎮 Discord',
    'amazon': '📦 Amazon',
    'microsoft': '🪟 Microsoft',
    'tiktok': '🎵 TikTok',
    'twitter': '🐦 Twitter / X',
    'x': '𝕏 X',
    'snapchat': '👻 Snapchat',
    'signal': '🔐 Signal',
    'viber': '📞 Viber',
    'uber': '🚕 Uber',
    'netflix': '🎬 Netflix',
    'openai': '🤖 OpenAI',
}


def app_label(service: str) -> str:
    """Display the actual purchased application, including unknown services."""
    value = str(service or '').strip()
    normalized = value.lower().replace('_', ' ').replace('-', ' ')
    key = normalized.replace(' ', '')
    if key in _SERVICE_NAMES:
        return _SERVICE_NAMES[key]
    return f'📱 {normalized.title()}' if normalized else '📱 Unknown service'
