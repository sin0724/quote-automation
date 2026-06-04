import os
import re
import threading
import logging
import urllib.request
from datetime import datetime
from xml.sax.saxutils import escape

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from flask import Flask, jsonify
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer, Image, HRFlowable,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Korean font setup ─────────────────────────────────────────────────────────

def _register_font() -> str:
    """NanumGothic(Regular+Bold) 다운로드 → 실패 시 HYSMyeongJo CID 폴백."""
    _base = 'https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/nanumgothic/'
    _fonts = [
        ('NanumGothic',      'NanumGothic-Regular.ttf', 'NanumGothic.ttf'),
        ('NanumGothic-Bold', 'NanumGothic-Bold.ttf',    'NanumGothic-Bold.ttf'),
    ]
    ok = True
    for name, url_file, local_file in _fonts:
        path = os.path.join(_DIR, local_file)
        if not os.path.exists(path):
            try:
                urllib.request.urlretrieve(_base + url_file, path)
            except Exception as e:
                logger.warning('폰트 다운로드 실패 (%s): %s', url_file, e)
                ok = False
                break
        if ok:
            try:
                pdfmetrics.registerFont(TTFont(name, path))
            except Exception as e:
                logger.warning('TTFont 등록 실패 (%s): %s', name, e)
                ok = False
                break

    if ok:
        pdfmetrics.registerFontFamily('NanumGothic',
            normal='NanumGothic', bold='NanumGothic-Bold')
        logger.info('NanumGothic 폰트 등록 완료')
        return 'NanumGothic'

    pdfmetrics.registerFont(UnicodeCIDFont('HYSMyeongJo-Medium'))
    logger.info('HYSMyeongJo-Medium CID 폰트 사용')
    return 'HYSMyeongJo-Medium'

KR = _register_font()

# ── Constants ─────────────────────────────────────────────────────────────────

RED   = colors.HexColor('#CC3333')
LGRAY = colors.HexColor('#F7F7F7')
MGRAY = colors.HexColor('#EEEEEE')
WHITE = colors.white
BLACK = colors.black
DKGRAY = colors.HexColor('#555555')

DEFAULT_NOTES = [
    '본 견적은 기획과 고객의 요구사항에 따라 일부 견적은 가감될 수 있습니다.',
    '최종 작업 범위 및 일정 확정 시 추가 비용이 발생할 수 있습니다.',
    '본 견적서의 내용은 서면 합의 없이 수정할 수 없습니다.',
    '본 견적 내용은 외부 유출을 금지합니다.',
]

# ── Environment ───────────────────────────────────────────────────────────────

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

# ── Flask ─────────────────────────────────────────────────────────────────────

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

# ── Helpers ───────────────────────────────────────────────────────────────────

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
            desc = parts[1]
            price = parse_number(parts[2])
            qty   = max(1, parse_number(parts[3]))
            unit  = parts[4] if parts[4] else 'EA'
        items.append({'name': name, 'desc': desc, 'price': price,
                      'qty': qty, 'unit': unit, 'amount': qty * price})
    return items


def calc_summary(items: list, discount: int, tax_type: str) -> dict:
    items_total    = sum(i['amount'] for i in items)
    after_discount = items_total - discount
    if '포함' in tax_type:
        supply = int(after_discount / 1.1)
        vat    = after_discount - supply
        final  = after_discount
    else:
        supply = after_discount
        vat    = int(supply * 0.1)
        final  = supply + vat
    return {'items_total': items_total, 'discount': discount,
            'supply': supply, 'vat': vat, 'final': final}


def won(n: int) -> str:
    return f'{n:,}원'

# ── Paragraph helper ──────────────────────────────────────────────────────────

def tx(text, size=9, bold=False, align=0, color=BLACK, leading=None):
    """Paragraph with auto XML-escape. bold=True wraps in <b> tag."""
    style = ParagraphStyle('_', fontName=KR, fontSize=size,
                           leading=leading or max(size * 1.55, size + 3),
                           alignment=align, textColor=color)
    safe = escape(str(text))
    return Paragraph(f'<b>{safe}</b>' if bold else safe, style)


def tx_raw(html, size=9, align=0, color=BLACK, leading=None):
    """Paragraph with raw HTML (caller handles escaping)."""
    style = ParagraphStyle('_', fontName=KR, fontSize=size,
                           leading=leading or max(size * 1.55, size + 3),
                           alignment=align, textColor=color)
    return Paragraph(html, style)

# ── PDF generation ────────────────────────────────────────────────────────────

def generate_pdf(data: dict) -> tuple:
    today    = datetime.now().strftime('%Y년 %m월 %d일')
    today_fn = datetime.now().strftime('%Y-%m-%d')
    safe_fn  = lambda s: re.sub(r'[\\/*?:"<>|]', '_', s)
    filename = f"{today_fn}_{safe_fn(data['client'])}_{safe_fn(data.get('quote_name','견적'))}.pdf"
    filepath = os.path.join(OUTPUT_DIR, filename)

    s   = calc_summary(data['items'], data['discount'], data['tax_type'])
    W   = 170 * mm   # 210mm - 20mm*2 margins

    doc = SimpleDocTemplate(
        filepath, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
    )

    elems = []

    # ── 1. Logo ───────────────────────────────────────────────────────────────
    if COMPANY_LOGO and os.path.exists(COMPANY_LOGO):
        elems.append(Image(COMPANY_LOGO, width=52*mm, height=12.8*mm))
    else:
        elems.append(tx(COMPANY_NAME, size=15, bold=True, color=RED))
    elems.append(Spacer(1, 5*mm))

    # ── 2. Title ──────────────────────────────────────────────────────────────
    elems.append(tx('외주 견적서', size=24, bold=True))
    elems.append(Spacer(1, 5*mm))

    # ── 3. Date / recipient ───────────────────────────────────────────────────
    meta = Table(
        [[tx(f'견적일자: {today}', size=8.5, color=DKGRAY),
          tx(f'수신자: {data["client"]}', size=8.5, color=DKGRAY)]],
        colWidths=[W * 0.5, W * 0.5],
    )
    meta.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    elems.append(meta)
    elems.append(Spacer(1, 3*mm))
    elems.append(HRFlowable(width='100%', thickness=0.6, color=colors.HexColor('#CCCCCC')))
    elems.append(Spacer(1, 4*mm))

    # ── 4. Supplier info ──────────────────────────────────────────────────────
    info_lines = [
        f'<font color="#888888" size="8">공급자</font>',
        f'<b><font size="13">{escape(COMPANY_NAME)}</font></b>',
        '',
    ]
    for label, val in [
        ('사업자', COMPANY_BUSINESS_NUMBER),
        ('대표자', COMPANY_REPRESENTATIVE),
        ('이메일', COMPANY_EMAIL),
        ('연락처', COMPANY_CONTACT),
        ('소재지', COMPANY_ADDRESS),
    ]:
        if val:
            info_lines.append(f'<font size="8.5"><font color="#888888">{label}</font>  {escape(val)}</font>')

    supplier_para = tx_raw('<br/>'.join(info_lines), size=8.5, leading=15)

    if COMPANY_STAMP and os.path.exists(COMPANY_STAMP):
        stamp_cell = Image(COMPANY_STAMP, width=22*mm, height=22*mm)
    else:
        stamp_cell = Spacer(1, 1)

    supplier_row = Table(
        [[supplier_para, stamp_cell]],
        colWidths=[W * 0.72, W * 0.28],
    )
    supplier_row.setStyle(TableStyle([
        ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elems.append(supplier_row)
    elems.append(Spacer(1, 5*mm))
    elems.append(HRFlowable(width='100%', thickness=0.6, color=colors.HexColor('#CCCCCC')))
    elems.append(Spacer(1, 4*mm))

    # ── 5. Items table ────────────────────────────────────────────────────────
    # 항목26 + 설명68 + 단가26 + 수량15 + 단위15 + 금액20 = 170mm
    cw = [26*mm, 68*mm, 26*mm, 15*mm, 15*mm, 20*mm]

    def hdr(t):
        return tx(t, size=9, bold=True, align=1, color=WHITE)

    rows = [[hdr('항목'), hdr('설명'), hdr('단가'), hdr('수량'), hdr('단위'), hdr('금액')]]

    for item in data['items']:
        rows.append([
            tx(item['name'], size=8.5),
            tx(item['desc'], size=8.5),
            tx(won(item['price']), size=8.5, align=2),
            tx(item['qty'],        size=8.5, align=1),
            tx(item['unit'],       size=8.5, align=1),
            tx(won(item['amount']),size=8.5, align=2),
        ])

    # 최소 5행 유지 (빈 행으로 채움)
    while len(rows) < 6:
        rows.append([tx('')] * 6)

    item_t = Table(rows, colWidths=cw, repeatRows=1)
    item_t.setStyle(TableStyle([
        # Header
        ('BACKGROUND',    (0, 0), (-1,  0), RED),
        ('LINEBELOW',     (0, 0), (-1,  0), 1.5, RED),
        # Body alternating
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, LGRAY]),
        # Grid
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.HexColor('#DDDDDD')),
        ('LINEBELOW',     (0,-1), (-1, -1), 0.8, colors.HexColor('#BBBBBB')),
        # Alignment & padding
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
    ]))
    elems.append(item_t)
    elems.append(Spacer(1, 7*mm))

    # ── 6. Bottom: notes (left) + summary (right) ─────────────────────────────
    # Notes ─────────────────────────────────────────────────────
    note_html = ['<b>참고사항</b><br/><br/>']
    for i, n in enumerate(DEFAULT_NOTES, 1):
        note_html.append(f'{i}. {escape(n)}<br/>')
    if data.get('note'):
        note_html.append(f'<br/>{escape(data["note"])}')
    notes_para = tx_raw(''.join(note_html), size=8.5, leading=16)

    # Summary ────────────────────────────────────────────────────
    disc_str = f'-{won(s["discount"])}' if s['discount'] > 0 else '  -'
    sum_rows = [
        [tx('총 합계',           size=8.5, align=2),
         tx(won(s['items_total']),size=8.5, align=2)],
        [tx('할인',              size=8.5, align=2),
         tx(disc_str,            size=8.5, align=2)],
        [tx('공급가액',           size=8.5, align=2),
         tx(won(s['supply']),    size=8.5, align=2)],
        [tx('VAT (10%)',         size=8.5, align=2),
         tx(won(s['vat']),       size=8.5, align=2)],
        [tx('최종 견적 (VAT 포함)', size=9, bold=True, align=2, color=WHITE),
         tx(won(s['final']),      size=9, bold=True, align=2, color=WHITE)],
    ]
    # 44+32 = 76mm (summary 전체 너비)
    sum_t = Table(sum_rows, colWidths=[44*mm, 32*mm])
    sum_t.setStyle(TableStyle([
        ('GRID',          (0, 0), (-1, -2), 0.4, colors.HexColor('#CCCCCC')),
        ('LINEABOVE',     (0, 4), (-1,  4), 0,   WHITE),  # last row no top grid
        ('BOX',           (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
        ('BACKGROUND',    (0, 4), (-1,  4), RED),
        ('ROWBACKGROUNDS',(0, 0), (-1, -2), [WHITE, MGRAY, WHITE, MGRAY]),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 7),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
    ]))

    # 94+76 = 170mm
    bottom = Table(
        [[notes_para, sum_t]],
        colWidths=[94*mm, 76*mm],
    )
    bottom.setStyle(TableStyle([
        ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING',   (0,0), (-1,-1), 0),
    ]))
    elems.append(bottom)

    doc.build(elems)
    return filepath, filename

# ── Modal ─────────────────────────────────────────────────────────────────────

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

# ── Slash command ─────────────────────────────────────────────────────────────

@bolt_app.command('/견적서')
def open_modal(ack, client, command):
    ack()
    client.views_open(trigger_id=command['trigger_id'], view=build_modal(command['channel_id']))

# ── Modal submission ──────────────────────────────────────────────────────────

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
                    f"✅ *{escape(data['client'])}* 견적서가 생성되었습니다.\n"
                    f"💰 최종 견적 (VAT 포함): {won(sm['final'])}"
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
