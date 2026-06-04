import os
import re
import threading
import logging
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Gothic (sans-serif) Korean CID font — matches template style
pdfmetrics.registerFont(UnicodeCIDFont('HYGoThic-Medium'))
KR = 'HYGoThic-Medium'

# ── Environment ───────────────────────────────────────────────────────────────

COMPANY_NAME            = os.environ.get('COMPANY_NAME', '회사명')
COMPANY_REPRESENTATIVE  = os.environ.get('COMPANY_REPRESENTATIVE', '대표자')
COMPANY_BUSINESS_NUMBER = os.environ.get('COMPANY_BUSINESS_NUMBER', '')
COMPANY_EMAIL           = os.environ.get('COMPANY_EMAIL', '')
COMPANY_ADDRESS         = os.environ.get('COMPANY_ADDRESS', '')
COMPANY_CONTACT         = os.environ.get('COMPANY_CONTACT', '')
_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANY_LOGO  = os.environ.get('COMPANY_LOGO',  os.path.join(_DIR, '자산 17_투명배경.png'))
COMPANY_STAMP = os.environ.get('COMPANY_STAMP', '')
OUTPUT_DIR              = os.environ.get('OUTPUT_DIR', '/tmp/output')

os.makedirs(OUTPUT_DIR, exist_ok=True)

RED   = colors.HexColor('#CC3333')
LGRAY = colors.HexColor('#F5F5F5')
WHITE = colors.white
BLACK = colors.black

DEFAULT_NOTES = [
    '본 견적은 기획과 고객의 요구사항에 따라 일부 견적은 가감될 수 있습니다.',
    '최종 작업 범위 및 일정 확정 시 추가 비용이 발생할 수 있습니다.',
    '본 견적서의 내용은 서면 합의 없이 수정할 수 없습니다.',
    '본 견적 내용은 외부 유출을 금지합니다.',
]

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

        name  = parts[0]
        desc  = ''
        price = 0
        qty   = 1
        unit  = 'EA'

        if len(parts) == 2:
            price = parse_number(parts[1])
        elif len(parts) == 3:
            if parse_number(parts[1]) > 0:      # name / price / qty
                price = parse_number(parts[1])
                qty   = max(1, parse_number(parts[2]))
            else:                                # name / desc / price
                desc  = parts[1]
                price = parse_number(parts[2])
        elif len(parts) == 4:                    # name / desc / price / qty
            desc  = parts[1]
            price = parse_number(parts[2])
            qty   = max(1, parse_number(parts[3]))
        else:                                    # name / desc / price / qty / unit
            desc  = parts[1]
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

    return {
        'items_total': items_total,
        'discount':    discount,
        'supply':      supply,
        'vat':         vat,
        'final':       final,
    }


def fmt(n: int) -> str:
    return f'{n:,}원'


def p(text, size=9, bold=False, align=0, color=BLACK, leading=None):
    style = ParagraphStyle(
        'x', fontName=KR, fontSize=size,
        leading=leading or size * 1.6,
        alignment=align, textColor=color,
        fontWeight='Bold' if bold else 'Normal',
    )
    return Paragraph(str(text), style)

# ── PDF ───────────────────────────────────────────────────────────────────────

def generate_pdf(data: dict) -> tuple:
    today    = datetime.now().strftime('%Y년 %m월 %d일')
    today_fn = datetime.now().strftime('%Y-%m-%d')
    safe     = lambda s: re.sub(r'[\\/*?:"<>|]', '_', s)
    filename = f"{today_fn}_{safe(data['client'])}_{safe(data.get('quote_name','견적'))}.pdf"
    filepath = os.path.join(OUTPUT_DIR, filename)

    summary = calc_summary(data['items'], data['discount'], data['tax_type'])

    doc = SimpleDocTemplate(
        filepath, pagesize=A4,
        rightMargin=18*mm, leftMargin=18*mm,
        topMargin=16*mm, bottomMargin=16*mm,
    )
    W = A4[0] - 36*mm   # usable width = 174mm

    elems = []

    # ── Logo ──────────────────────────────────────────────────────────────────
    if COMPANY_LOGO and os.path.exists(COMPANY_LOGO):
        # 원본 비율 3403×838 유지, 폭 55mm 기준
        elems.append(Image(COMPANY_LOGO, width=55*mm, height=13.5*mm))
    else:
        elems.append(p(COMPANY_NAME, size=16, bold=True, color=RED))
    elems.append(Spacer(1, 4*mm))

    # ── Title ─────────────────────────────────────────────────────────────────
    elems.append(p('외주 견적서', size=22, bold=True))
    elems.append(Spacer(1, 4*mm))

    # ── Date / recipient line ─────────────────────────────────────────────────
    date_row = Table(
        [[p(f'견적일자: {today}', size=8.5),
          p(f'수신자: {escape(data["client"])}', size=8.5)]],
        colWidths=[W * 0.45, W * 0.55],
    )
    date_row.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    elems.append(date_row)
    elems.append(Spacer(1, 2*mm))
    elems.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey))
    elems.append(Spacer(1, 3*mm))

    # ── Supplier block ────────────────────────────────────────────────────────
    def info_line(label, val):
        return p(f'{label}  {escape(val)}', size=8.5)

    supplier_lines = [
        p('공급자', size=8, color=colors.grey),
        p(COMPANY_NAME, size=13, bold=True),
        Spacer(1, 1*mm),
        info_line('사업자', COMPANY_BUSINESS_NUMBER),
        info_line('대표자', COMPANY_REPRESENTATIVE),
    ]
    if COMPANY_EMAIL:
        supplier_lines.append(info_line('이메일', COMPANY_EMAIL))
    if COMPANY_CONTACT:
        supplier_lines.append(info_line('연락처', COMPANY_CONTACT))
    if COMPANY_ADDRESS:
        supplier_lines.append(info_line('소재지', COMPANY_ADDRESS))

    # Stamp image (right of supplier block)
    if COMPANY_STAMP and os.path.exists(COMPANY_STAMP):
        stamp_cell = Image(COMPANY_STAMP, width=22*mm, height=22*mm)
    else:
        stamp_cell = p('')

    from reportlab.platypus import KeepInFrame
    supplier_frame = KeepInFrame(
        maxWidth=W * 0.72, maxHeight=35*mm,
        content=supplier_lines, mode='shrink',
    )
    supplier_row = Table(
        [[supplier_frame, stamp_cell]],
        colWidths=[W * 0.72, W * 0.28],
    )
    supplier_row.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elems.append(supplier_row)
    elems.append(Spacer(1, 4*mm))

    # ── Items table ───────────────────────────────────────────────────────────
    # 항목(28) 설명(72) 단가(26) 수량(16) 단위(16) 금액(16) = 174mm
    cw = [28*mm, 72*mm, 26*mm, 16*mm, 16*mm, 16*mm]

    def hdr(txt):
        return p(txt, size=9, bold=True, align=1, color=WHITE)

    rows = [[hdr('항목'), hdr('설명'), hdr('단가'), hdr('수량'), hdr('단위'), hdr('금액')]]

    for item in data['items']:
        rows.append([
            p(escape(item['name']), size=8.5),
            p(escape(item['desc']), size=8.5),
            p(f"{item['price']:,}원", size=8.5, align=2),
            p(str(item['qty']),       size=8.5, align=1),
            p(item['unit'],           size=8.5, align=1),
            p(f"{item['amount']:,}원", size=8.5, align=2),
        ])

    while len(rows) < 6:
        rows.append([p('')] * 6)

    item_t = Table(rows, colWidths=cw, repeatRows=1)
    item_t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0),  RED),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, LGRAY]),
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.HexColor('#DDDDDD')),
        ('LINEBELOW',     (0, 0), (-1, 0),  1,   RED),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
    ]))
    elems.append(item_t)
    elems.append(Spacer(1, 6*mm))

    # ── Bottom: 참고사항 (left) + summary (right) ─────────────────────────────
    # Notes
    note_lines = ['<b>참고사항</b><br/><br/>']
    for i, n in enumerate(DEFAULT_NOTES, 1):
        note_lines.append(f'{i}. {escape(n)}<br/>')
    if data.get('note'):
        note_lines.append(f'<br/>{escape(data["note"])}')
    note_para = Paragraph(
        ''.join(note_lines),
        ParagraphStyle('notes', fontName=KR, fontSize=8.5, leading=15),
    )

    # Summary table
    disc_str = f'-{fmt(summary["discount"])}' if summary['discount'] > 0 else '-'
    sum_rows = [
        [p('총 합계',           size=9, align=2), p(fmt(summary['items_total']), size=9, align=2)],
        [p('할인',              size=9, align=2), p(disc_str,                    size=9, align=2)],
        [p('공급가액',           size=9, align=2), p(fmt(summary['supply']),      size=9, align=2)],
        [p('VAT (10%)',         size=9, align=2), p(fmt(summary['vat']),          size=9, align=2)],
        [p('최종 견적 (VAT 포함)', size=9, bold=True, align=2, color=WHITE),
         p(fmt(summary['final']),size=9, bold=True, align=2, color=WHITE)],
    ]
    sum_t = Table(sum_rows, colWidths=[42*mm, 30*mm])
    sum_t.setStyle(TableStyle([
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
        ('BACKGROUND',    (0, 4), (-1,  4), RED),
        ('LINEABOVE',     (0, 4), (-1,  4), 1, RED),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
    ]))

    bottom = Table(
        [[note_para, sum_t]],
        colWidths=[W - 72*mm, 72*mm],
    )
    bottom.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
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
                'label': {'type': 'plain_text', 'text': '할인 금액 (없으면 0)'},
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
        s = calc_summary(items, discount, data['tax_type'])

        with open(filepath, 'rb') as f:
            client.files_upload_v2(
                channel=channel_id,
                file=f,
                filename=filename,
                title=f"{data['client']} 외주 견적서",
                initial_comment=(
                    f"✅ *{escape(data['client'])}* 견적서가 생성되었습니다.\n"
                    f"💰 최종 견적 (VAT 포함): {fmt(s['final'])}"
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
