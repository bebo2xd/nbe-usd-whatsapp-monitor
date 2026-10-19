"""Read NBE USD banknote BUY and notify through Evolution API v2."""
import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE = 'https://www.nbe.com.eg/NBE/E/#/EN/ExchangeRatesAndCurrencyConverter'
# Currency, transaction type and direction are checked together; never trust row order.
BUY_XPATH = "//tr[td[@id='flag']//span[normalize-space()='US DOLLAR']]/td[@id='Banknote']/table/tbody/tr[td[1][normalize-space()='Buy']]/td[2]"
STATE = Path(os.environ.get('STATE_FILE', 'state.json'))


def normalize_price(raw):
    raw = raw.strip()
    if not re.fullmatch(r'\d{1,4}(?:\.\d{1,6})?', raw):
        raise ValueError('NBE returned an invalid price')
    price = Decimal(raw)
    if not price.is_finite() or price <= 0:
        raise ValueError('NBE returned a nonpositive price')
    return format(price.normalize(), 'f')


def read_price():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale='en-US')
            page.set_default_timeout(90000)
            page.goto(SOURCE, wait_until='domcontentloaded', timeout=90000)
            cell = page.locator('xpath=' + BUY_XPATH)
            cell.wait_for(state='visible')
            if cell.count() != 1:
                raise ValueError('USD banknote BUY cell is ambiguous')
            return normalize_price(cell.inner_text())
        finally:
            browser.close()


def api(base, key, path, body=None):
    headers = {'apikey': key, 'Accept': 'application/json'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = Request(base.rstrip('/') + path, data=data, headers=headers)
    try:
        with urlopen(request, timeout=40) as response:
            result = json.load(response)
    except HTTPError as exc:
        # Do not print response bodies: they may contain numbers or credentials.
        raise RuntimeError(f'Evolution HTTP {exc.code}') from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError('Evolution connection or JSON response failed') from None
    return result


def resolve_instance(base, key, configured=''):
    if configured:
        result = api(base, key, '/instance/connectionState/' + quote(configured, safe=''))
        if result.get('instance', {}).get('state') != 'open':
            raise RuntimeError('Configured Evolution instance is not connected')
        return configured
    result = api(base, key, '/instance/fetchInstances')
    if not isinstance(result, list):
        raise RuntimeError('Unexpected Evolution instances response; set EVOLUTION_INSTANCE')
    connected = []
    for item in result:
        instance = item.get('instance', item)
        if instance.get('connectionStatus', instance.get('state')) == 'open':
            name = instance.get('name') or instance.get('instanceName')
            if name:
                connected.append(name)
    if len(connected) != 1:
        raise RuntimeError('Need exactly one connected instance, or set EVOLUTION_INSTANCE')
    return connected[0]


def send_notification(price, previous):
    base = os.environ['EVOLUTION_URL'].rstrip('/')
    if not base.startswith('https://') or base.endswith('/manager'):
        raise ValueError('EVOLUTION_URL must be the HTTPS server root, without /manager')
    key = os.environ['EVOLUTION_API_KEY']
    number = re.sub(r'\D', '', os.environ['WHATSAPP_NUMBER'])
    if not re.fullmatch(r'[1-9]\d{7,14}', number):
        raise ValueError('WHATSAPP_NUMBER must include country code')
    instance = resolve_instance(base, key, os.environ.get('EVOLUTION_INSTANCE', '').strip())
    when = datetime.now(ZoneInfo('Africa/Cairo')).strftime('%Y-%m-%d %H:%M:%S')
    if previous is None:
        text = f'✅ تم تشغيل متابعة الدولار في البنك الأهلي المصري.\nسعر الشراء النقدي الحالي: {price} جنيه.'
    else:
        delta = Decimal(price) - Decimal(previous)
        text = (f'🔔 تغير سعر شراء الدولار في البنك الأهلي المصري\n'
                f'السابق: {previous} جنيه\nالجديد: {price} جنيه\n'
                f'التغير: {delta:+f} جنيه')
    text += f'\nوقت الرصد بتوقيت القاهرة: {when}\n{SOURCE}'
    result = api(base, key, '/message/sendText/' + quote(instance, safe=''),
                 {'number': number, 'text': text})
    if not isinstance(result, dict) or not result.get('key', {}).get('id'):
        raise RuntimeError('Evolution did not acknowledge the message with an ID')


def load_state():
    if not STATE.exists():
        return {}
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if state.get('version') != 1 or state.get('source') != SOURCE:
        raise ValueError('State format/source invalid; refusing to reset baseline')
    state['price'] = normalize_price(state['price'])
    return state


def run(dry_run=False, reader=read_price, sender=send_notification):
    price = reader()
    print(f'NBE USD banknote BUY: {price} EGP')
    if dry_run:
        print('Read-only check passed; no message or state change.')
        return
    state = load_state()
    previous = state.get('price')
    if previous != price:
        sender(price, previous)
        print('Evolution accepted the notification.')
    else:
        print('No price change; no notification.')
    # Write only after successful send. Daily heartbeat keeps the scheduled repo active.
    today = datetime.now(timezone.utc).date().isoformat()
    if previous != price or state.get('last_success_day') != today:
        updated = {'version': 1, 'source': SOURCE, 'price': price,
                   'last_success_day': today}
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
        tmp.replace(STATE)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.dry_run:
        for name in ('EVOLUTION_URL', 'EVOLUTION_API_KEY', 'WHATSAPP_NUMBER'):
            if not os.environ.get(name):
                raise SystemExit(f'Missing secret: {name}')
    # Retry page reads only. Retrying an ambiguous WhatsApp POST can duplicate a message.
    def reader_with_retry():
        for attempt in range(3):
            try:
                return read_price()
            except Exception:
                if attempt == 2:
                    raise RuntimeError('Unable to read NBE USD banknote BUY after 3 attempts') from None
                time.sleep(5)
    run(args.dry_run, reader=reader_with_retry)
