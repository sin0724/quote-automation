import os
import re
import threading
import logging
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from flask import Flask, jsonify
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Environment variables ─────────────────────────────────────────────────────

COMPANY_NAME            = os.environ.get('COMPANY_NAME', '회사명')
COMPANY_REPRESENTATIVE  = os.environ.get('COMPANY_REPRESENTATIVE', '대표자')
COMPANY_BUSINESS_NUMBER = os.environ.get('COMPANY_BUSINESS_NUMBER', '')
COMPANY_EMAIL           = os.environ.get('COMPANY_EMAIL', '')
COMPANY_ADDRESS         = os.environ.get('COMPANY_ADDRESS', '')
COMPANY_CONTACT         = os.environ.get('COMPANY_CONTACT', '')
COMPANY_LOGO            = os.environ.get('COMPANY_LOGO',  os.path.join(_DIR, '자산 17_투명배경.png'))
COMPANY_STAMP           = os.environ.get('COMPANY_STAMP', '')
OUTPUT_DIR              = os.environ.get('OUTPUT_DIR', '/tmp/output')

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Jinja2 template environment ───────────────────────────────────────────────

_jinja = Environment(loader=FileSystemLoader(_DIR))
_jinja.filters['commas'] = lambda n: f'{int(n):,}'

# ── Flask health-check ────────────────────────────────────────────────────────

web_app = Flask(__name__)

@web_app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@web_app.route('/')
def root():
    return 'ok'

# ── Slack ─────────────────────────────────────────────────────────────────────

_bot_token = os.environ.get('SLACK_BOT_TOKEN')
_app_token = os.environ.get('SLACK_APP_TOKEN')
if not _bot_token or not _app_token:
    missing = [k for k, v in [('SLACK_BOT_TOKEN', _bot_token), ('SLACK_APP_TOKEN', _app_token)] if not v]
    raise SystemExit(f'[ERROR] 환경변수 누락: {", ".join(missing)}')

bolt_app = App(token=_bot_token)

# ── Data helpers ──────────────────────────────────────────────────────────────

def parse_number(s: str) -> int:
    cleaned = re.sub(r'[,원₩\s]', '', s.strip())
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def parse_items(text: str) -> list:
    items = []
    for raw in text.strip().splitlines():
        line = raw.strip().lstrip('-•* ')
        if not line:
            continue
        parts = [p.strip() for p in line.split('/')]
        if len(parts) < 2:
            continue
        name, desc, price, qty, unit = parts[0], '', 0, 1, 'EA'
        if len(parts) == 2:
            price = parse_number(parts[1])
        elif len(parts) == 3:
            if parse_number(parts[1]) > 0:
                price, qty = parse_number(parts[1]), max(1, parse_number(parts[2]))
            else:
                desc, price = parts[1], parse_number(parts[2])
        elif len(parts) == 4:
            desc, price, qty = parts[1], parse_number(parts[2]), max(1, parse_number(parts[3]))
        else:
            desc  = parts[1]
            price = parse_number(parts[2])
            qty   = max(1, parse_number(parts[3]))
            unit  = parts[4] if parts[4] else 'EA'
        items.append({'name': name, 'desc': desc, 'price': price,
                      'qty': qty, 'unit': unit, 'amount': qty * price})
    return items


def calc_summary(items: list, discount: int, tax_type: str) -> dict:
    total = sum(i['amount'] for i in items)
    after = total - discount
    if '포함' in tax_type:
        supply = int(after / 1.1)
        vat    = after - supply
        final  = after
    else:
        supply = after
        vat    = int(supply * 0.1)
        final  = supply + vat
    disc_str = f'-{discount:,}원' if discount > 0 else '-'
    return {'items_total': total, 'discount': discount, 'discount_str': disc_str,
            'supply': supply, 'vat': vat, 'final': final}

# ── PDF generation ────────────────────────────────────────────────────────────

def generate_pdf(data: dict) -> tuple:
    today    = datetime.now().strftime('%Y년 %m월 %d일')
    today_fn = datetime.now().strftime('%Y-%m-%d')
    safe     = lambda s: re.sub(r'[\\/*?:"<>|]', '_', s)
    filename = f"{today_fn}_{safe(data['client'])}_{safe(data.get('quote_name', '견적'))}.pdf"
    filepath = os.path.join(OUTPUT_DIR, filename)

    summary   = calc_summary(data['items'], data['discount'], data['tax_type'])
    logo_path = COMPANY_LOGO  if (COMPANY_LOGO  and os.path.exists(COMPANY_LOGO))  else None
    stamp_path= COMPANY_STAMP if (COMPANY_STAMP and os.path.exists(COMPANY_STAMP)) else None

    # Convert local image paths to file:// URIs for WeasyPrint
    def to_uri(path):
        return 'file:///' + path.replace('\\', '/') if path else None

    # Minimum 5 visible item rows; pad with empty rows
    empty_rows = max(0, 5 - len(data['items']))

    tmpl = _jinja.get_template('template.html')
    html_str = tmpl.render(
        today=today,
        client=data['client'],
        logo_path=to_uri(logo_path),
        stamp_path=to_uri(stamp_path),
        company_name=COMPANY_NAME,
        company_representative=COMPANY_REPRESENTATIVE,
        company_business_number=COMPANY_BUSINESS_NUMBER,
        company_email=COMPANY_EMAIL,
        company_contact=COMPANY_CONTACT,
        company_address=COMPANY_ADDRESS,
        items=data['items'],
        empty_rows=empty_rows,
        summary=summary,
        extra_note=data.get('note', ''),
    )

    HTML(string=html_str, base_url=_DIR).write_pdf(filepath)
    return filepath, filename

# ── Slack modal ───────────────────────────────────────────────────────────────

def build_modal(channel_id: str) -> dict:
    return {
        'type': 'modal',
        'callback_id': 'quote_modal',
        'private_metadata': channel_id,
        'title':  {'type': 'plain_text', 'text': '견적서 생성'},
        'submit': {'type': 'plain_text', 'text': '📄 생성'},
        'close':  {'type': 'plain_text', 'text': '취소'},
        'blocks': [
            {
                'type': 'input', 'block_id': 'client',
                'label': {'type': 'plain_text', 'text': '수신자 (거래처명)'},
                'element': {
                    'type': 'plain_text_input', 'action_id': 'value',
                    'placeholder': {'type': 'plain_text', 'text': '예: ABC병원'},
                },
            },
            {
                'type': 'input', 'block_id': 'quote_name',
                'label': {'type': 'plain_text', 'text': '견적명 (파일명에 사용)'},
                'element': {
                    'type': 'plain_text_input', 'action_id': 'value',
                    'placeholder': {'type': 'plain_text', 'text': '예: 대만 KOC 마케팅'},
                },
            },
            {
                'type': 'input', 'block_id': 'tax_type',
                'label': {'type': 'plain_text', 'text': '부가세'},
                'element': {
                    'type': 'static_select', 'action_id': 'value',
                    'initial_option': {
                        'text': {'type': 'plain_text', 'text': '별도 (공급가액 + 10%)'},
                        'value': '별도',
                    },
                    'options': [
                        {'text': {'type': 'plain_text', 'text': '별도 (공급가액 + 10%)'}, 'value': '별도'},
                        {'text': {'type': 'plain_text', 'text': '포함 (총액 기준 역산)'}, 'value': '포함'},
                    ],
                },
            },
            {
                'type': 'input', 'block_id': 'items',
                'label': {'type': 'plain_text', 'text': '품목  (항목 / 설명 / 단가 / 수량 / 단위)'},
                'element': {
                    'type': 'plain_text_input', 'action_id': 'value',
                    'multiline': True,
                    'placeholder': {
                        'type': 'plain_text',
                        'text': '시딩 / 왕홍 @kimi_0531 / 5000000 / 1 / EA\nThreads 바이럴 / 20건 운영 / 800000 / 1 / EA',
                    },
                },
            },
            {
                'type': 'input', 'block_id': 'discount',
                'label': {'type': 'plain_text', 'text': '할인 금액 (없으면 비워두세요)'},
                'optional': True,
                'element': {
                    'type': 'plain_text_input', 'action_id': 'value',
                    'placeholder': {'type': 'plain_text', 'text': '예: 500000'},
                },
            },
            {
                'type': 'input', 'block_id': 'note',
                'label': {'type': 'plain_text', 'text': '추가 참고사항 (선택)'},
                'optional': True,
                'element': {
                    'type': 'plain_text_input', 'action_id': 'value',
                    'multiline': True,
                    'placeholder': {'type': 'plain_text', 'text': '추가로 기재할 내용이 있으면 입력하세요'},
                },
            },
        ],
    }

# ── Slack handlers ────────────────────────────────────────────────────────────

@bolt_app.command('/견적서')
def open_modal(ack, client, command):
    ack()
    client.views_open(trigger_id=command['trigger_id'], view=build_modal(command['channel_id']))


@bolt_app.view('quote_modal')
def handle_submission(ack, body, client):
    ack()
    vals       = body['view']['state']['values']
    channel_id = body['view']['private_metadata']

    def v(block):
        field = vals.get(block, {}).get('value', {})
        if not field:
            return ''
        if field.get('type') == 'static_select':
            return (field.get('selected_option') or {}).get('value', '별도')
        return field.get('value') or ''

    items    = parse_items(v('items'))
    discount = parse_number(v('discount')) if v('discount') else 0

    if not v('client'):
        client.chat_postMessage(channel=channel_id, text='❌ 수신자(거래처명)를 입력해주세요.')
        return
    if not items:
        client.chat_postMessage(channel=channel_id,
            text='❌ 품목을 한 줄 이상 입력해주세요.\n형식: `항목 / 설명 / 단가 / 수량 / 단위`')
        return

    data = {
        'client':     v('client'),
        'quote_name': v('quote_name'),
        'tax_type':   v('tax_type'),
        'items':      items,
        'discount':   discount,
        'note':       v('note'),
    }

    try:
        filepath, filename = generate_pdf(data)
        sm = calc_summary(items, discount, data['tax_type'])

        with open(filepath, 'rb') as f:
            client.files_upload_v2(
                channel=channel_id,
                file=f,
                filename=filename,
                title=f"{data['client']} 외주 견적서",
                initial_comment=(
                    f"✅ *{data['client']}* 견적서가 생성되었습니다.\n"
                    f"💰 최종 견적 (VAT 포함): {sm['final']:,}원"
                ),
            )
        os.remove(filepath)

    except Exception as exc:
        logger.error('PDF error: %s', exc, exc_info=True)
        client.chat_postMessage(channel=channel_id,
            text=f'❌ 견적서 생성 중 오류가 발생했습니다.\n```{exc}```')

# ── Entry point ───────────────────────────────────────────────────────────────

def _run_web():
    port = int(os.environ.get('PORT', 3000))
    web_app.run(host='0.0.0.0', port=port, use_reloader=False)


if __name__ == '__main__':
    logger.info('Quote automation bot started')
    threading.Thread(target=_run_web, daemon=True).start()
    SocketModeHandler(bolt_app, _app_token).start()
