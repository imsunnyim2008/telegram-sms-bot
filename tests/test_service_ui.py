"""Branded app logos are Telegram custom emoji, not decorative Unicode emoji."""
import pytest

from app.service_ui import (app_label, app_label_html, APP_ICONS,
                            without_custom_emoji, custom_emoji_unavailable)


@pytest.mark.parametrize('service, emoji_id, display', [
    ('telegram', '5415999505952705793', 'Telegram'),
    ('whatsapp', '5418150421279572914', 'WhatsApp'),
    ('facebook', '5418039855936472761', 'Facebook'),
    ('instagram', '5418261965875217473', 'Instagram'),
    ('gmail', '5418023732629242173', 'Gmail'),
    ('discord', '5415809432175026159', 'Discord'),
    ('google', '5417924815237444975', 'Google'),
    ('viber', '5415581932052318798', 'Viber'),
    ('netflix', '5416013194013476378', 'Netflix'),
])
def test_real_app_logo_uses_genuine_custom_emoji_id(service, emoji_id, display):
    assert APP_ICONS[service] == emoji_id
    assert app_label_html(service) == (
        f'<tg-emoji emoji-id="{emoji_id}">📱</tg-emoji> {display}'
    )


def test_unknown_apps_show_correct_name_without_fake_emoji():
    assert app_label_html('my_special_app') == 'My Special App'
    assert app_label('my_special_app') == 'My Special App'
    assert '<tg-emoji' not in app_label_html('my_special_app')


def test_unknown_names_are_escaped_for_html():
    assert app_label_html('<bad>&') == '&lt;Bad&gt;&amp;'


def test_fallback_drops_custom_emoji_not_service_name():
    html = 'App: ' + app_label_html('whatsapp') + '\nNumber: 1234'
    assert without_custom_emoji(html) == 'App: WhatsApp\nNumber: 1234'
    assert custom_emoji_unavailable(Exception("can't parse entities"))
    assert not custom_emoji_unavailable(Exception("chat not found"))
