import hashlib
import hmac
import json

import pytest

from app.smspool_webhooks import valid_signature, parse_event


def test_signature_and_replay():
    secret = 'whsec_example_test'
    body = b'{"event":"webhook.test","event_id":"evt_test"}'
    timestamp = '1792843200'
    signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body,
                         hashlib.sha256).hexdigest()
    header = f't={timestamp},v1={signature}'
    assert valid_signature(body, header, secret, now=int(timestamp))
    assert not valid_signature(body + b' ', header, secret, now=int(timestamp))
    assert not valid_signature(body, header, secret, now=int(timestamp) + 301)
    assert not valid_signature(body, header, '', now=int(timestamp))
    assert parse_event(body) == {'event': 'webhook.test'}


def test_order_event_excludes_code():
    body = json.dumps({'event':'order.code','event_id':'evt_example',
                       'order_code':'AB12','phonenumber':'14155550123',
                       'code':'123456'}).encode()
    result = parse_event(body)
    assert result['order'] == 'AB12'
    assert 'code' not in result
    with pytest.raises(ValueError):
        parse_event(b'{"event":"order.code","event_id":"evt_example"}')
