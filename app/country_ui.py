"""Friendly country flags and fixed priority for any Grizzly SMS service.

Never fake stock: missing preferred countries appear as unavailable.
The provider country ID remains the only authority for purchases.
"""
import re
import unicodedata

import pycountry


FAVORITE_COUNTRIES = (
    ('usa', 'USA', 'US'),
    ('canada', 'Canada', 'CA'),
    ('uk', 'UK', 'GB'),
    ('nigeria', 'Nigeria', 'NG'),
    ('japan', 'Japan', 'JP'),
)

COUNTRY_ALIASES = {
    'usa': 'US',
    'u.s.a': 'US',
    'us': 'US',
    'united states': 'US',
    'united states of america': 'US',
    'uk': 'GB',
    'great britain': 'GB',
    'britain': 'GB',
    'england': 'GB',
    'scotland': 'GB',
    'wales': 'GB',
    'united kingdom': 'GB',
    'russia': 'RU',
    'south korea': 'KR',
    'north korea': 'KP',
    'vietnam': 'VN',
    'tanzania': 'TZ',
    'syria': 'SY',
    'bolivia': 'BO',
    'moldova': 'MD',
    'venezuela': 'VE',
    'ivory coast': 'CI',
    "cote d'ivoire": 'CI',
    'dr congo': 'CD',
    'democratic republic of congo': 'CD',
    'congo drc': 'CD',
    'reunion': 'RE',
    'kosovo': 'XK',
    'palestine': 'PS',
    'turkey': 'TR',
    'czech republic': 'CZ',
    'swaziland': 'SZ',
    'vatican': 'VA',
    'vatican city': 'VA',
    'taiwan': 'TW',
    'laos': 'LA',
    'brunei': 'BN',
    'cape verde': 'CV',
    'the bahamas': 'BS',
    'bahamas': 'BS',
    'curacao': 'CW',
    'macau': 'MO',
    'hong kong': 'HK',
    'us virgin islands': 'VI',
    'british virgin islands': 'VG',
    'south sudan': 'SS',
}


def country_key(value: str) -> str:
    value = re.sub(r'\s*\(\d+\)\s*$', '', value).strip().lower()
    value = unicodedata.normalize('NFKD', value)
    return ''.join(ch for ch in value if not unicodedata.combining(ch))


def country_iso2(name: str) -> str | None:
    key = country_key(name)
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    try:
        return pycountry.countries.lookup(key).alpha_2
    except LookupError:
        return None


def emoji_flag(code: str | None) -> str:
    if not code or len(code) != 2 or not code.isascii() or not code.isalpha():
        return '🌐'
    return ''.join(chr(127397 + ord(char)) for char in code.upper())


def country_flag(name: str) -> str:
    return emoji_flag(country_iso2(name))


def arrange_country_offers(items, names):
    """Show USA, Canada, UK, Nigeria, Japan first, then all other live offers.

    Returns (item | None, label, flag) rows. A None item is out of stock.
    """
    with_names = [(item, names.get(item.country, 'Country #' + item.country))
                  for item in items]
    remaining = list(with_names)
    featured = []
    for _, label, iso in FAVORITE_COUNTRIES:
        candidates = [(item, name) for item, name in remaining
                      if country_iso2(name) == iso]
        if candidates:
            item, _ = min(candidates, key=lambda pair: (pair[0].cost, pair[0].country))
            remaining = [(x, name) for x, name in remaining if x is not item]
            featured.append((item, label, emoji_flag(iso)))
        else:
            featured.append((None, label, emoji_flag(iso)))
    remaining.sort(key=lambda pair: (pair[0].cost, pair[1].casefold()))
    return featured + [(item, name, country_flag(name)) for item, name in remaining]
