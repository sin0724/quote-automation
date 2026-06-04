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
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer, Image, HRFlowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Fonts (NanumGothic TTF bundled in repo) ───────────────────────────────────
pdfmetrics.registerFont(TTFont('NG',  os.path.join(_DIR, 'NanumGothic-Regular.ttf')))
pdfmetrics.registerFont(TTFont('NGb', os.path.join(_DIR, 'NanumGothic-Bold.ttf')))
pdfmetrics.registerFontFamily('NG', normal='NG', bold='NGb')

# ── Env ───────────────────────────────────────────────────────────────────────
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

RED    = colors.HexColor('#CC3333')
LGRAY  = colors.HexColor('#F7F7F7')
MGRAY  = colors.HexColor('#EEEEEE')
DGRAY  = colors.HexColor('#888888')
WHITE  = colors.white
BLACK  = colors.black

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

# ── Data helpers ──────────────────────────────────────────────────────────────
def parse_number(s: str) -> int:
    try:
        return int(float(re.sub(r'[,원₩\s]', '', s.strip())))
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
            unit  = parts[4] or 'EA'
        items.append({'name': name, 'desc': desc, 'price': price,
                      'qty': qty, 'unit': unit, 'amount': qty * price})
    return items

def calc_summary(items, discount, tax_type):
    total = sum(i['amount'] for i in items)
    after = total - discount
    if '포함' in tax_type:
        supply, vat, final = int(after / 1.1), after - int(after / 1.1), after
    else:
        supply = after
        vat    = int(supply * 0.1)
        final  = supply + vat
    return {'items_total': total, 'discount': discount,
            'supply': supply, 'vat': vat, 'final': final}

# ── Paragraph shortcuts ───────────────────────────────────────────────────────
def _style(size=9, bold=False, align=0, color=BLACK, leading=None):
    return ParagraphStyle('_',
        fontName='NGb' if bold else 'NG',
        fontSize=size,
        leading=leading or max(size * 1.6, size + 4),
        alignment=align,
        textColor=color,
    )

def tx(text, size=9, bold=False, align=0, color=BLACK, leading=None):
    return Paragraph(escape(str(text)), _style(size, bold, align, color, leading))

def tx_html(html, size=9, bold=False, align=0, color=BLACK, leading=None):
    return Paragraph(html, _style(size, bold, align, color, leading))

# ── PDF ───────────────────────────────────────────────────────────────────────
DEFAULT_NOTES = [
    '본 견적은 기획과 고객의 요구사항에 따라 일부 견적은 가감될 수 있습니다.',
    '최종 작업 범위 및 일정 확정 시 추가 비용이 발생할 수 있습니다.',
    '본 견적서의 내용은 서면 합의 없이 수정할 수 없습니다.',
    '본 견적 내용은 외부 유출을 금지합니다.',
]

def generate_pdf(data: dict) -> tuple:
    today    = datetime.now().strftime('%Y년 %m월 %d일')
    today_fn = datetime.now().strftime('%Y-%m-%d')
    safe     = lambda s: re.sub(r'[\\/*?:"<>|]', '_', s)
    filename = f"{today_fn}_{safe(data['client'])}_{safe(data.get('quote_name','견적'))}.pdf"
    filepath = os.path.join(OUTPUT_DIR, filename)

    sm = calc_summary(data['items'], data['discount'], data['tax_type'])
    W  = 170 * mm   # usable width (A4 210mm − 20mm × 2)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm)

    E = []   # elements list

    # ── Logo ──────────────────────────────────────────────────────────────────
    if COMPANY_LOGO and os.path.exists(COMPANY_LOGO):
        E.append(Image(COMPANY_LOGO, width=52*mm, height=12.8*mm))
    else:
        E.append(tx(COMPANY_NAME, size=16, bold=True, color=RED))
    E.append(Spacer(1, 5*mm))

    # ── Title ─────────────────────────────────────────────────────────────────
    E.append(tx('외주 견적서', size=24, bold=True))
    E.append(Spacer(1, 5*mm))

    # ── Date / recipient ──────────────────────────────────────────────────────
    E.append(Table(
        [[tx(f'견적일자: {today}', size=8.5, color=DGRAY),
          tx(f'수신자: {data["client"]}', size=8.5, color=DGRAY)]],
        colWidths=[W * 0.5, W * 0.5],
        style=[('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
               ('LEFTPADDING', (0,0), (-1,-1), 0),
               ('RIGHTPADDING', (0,0), (-1,-1), 0)],
    ))
    E.append(Spacer(1, 3*mm))
    E.append(HRFlowable(width='100%', thickness=0.6, color=colors.HexColor('#CCCCCC')))
    E.append(Spacer(1, 5*mm))

    # ── Supplier ──────────────────────────────────────────────────────────────
    info_rows = [
        [tx('공급자', size=8, color=DGRAY), ''],
        [tx(COMPANY_NAME, size=13, bold=True), ''],
    ]
    for label, val in [
        ('사업자', COMPANY_BUSINESS_NUMBER),
        ('대표자', COMPANY_REPRESENTATIVE),
        ('이메일', COMPANY_EMAIL),
        ('연락처', COMPANY_CONTACT),
        ('소재지', COMPANY_ADDRESS),
    ]:
        if val:
            info_rows.append([
                tx_html(f'<font color="#AAAAAA">{label}</font>  {escape(val)}', size=8.5),
                '',
            ])

    # stamp
    if COMPANY_STAMP and os.path.exists(COMPANY_STAMP):
        info_rows[0][1] = Image(COMPANY_STAMP, width=22*mm, height=22*mm)

    supplier_t = Table(info_rows, colWidths=[W * 0.75, W * 0.25])
    supplier_t.setStyle(TableStyle([
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING',    (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING',   (0,0), (-1,-1), 0),
        ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ('SPAN',          (1,1), (1,-1)),   # stamp cell spans remaining rows
    ]))
    E.append(supplier_t)
    E.append(Spacer(1, 5*mm))
    E.append(HRFlowable(width='100%', thickness=0.6, color=colors.HexColor('#CCCCCC')))
    E.append(Spacer(1, 5*mm))

    # ── Items table ───────────────────────────────────────────────────────────
    # 25+65+26+15+15+24 = 170mm
    cw = [25*mm, 65*mm, 26*mm, 15*mm, 15*mm, 24*mm]

    def hdr(t): return tx(t, size=9, bold=True, align=1, color=WHITE)

    rows = [[hdr('항목'), hdr('설명'), hdr('단가'), hdr('수량'), hdr('단위'), hdr('금액')]]
    for item in data['items']:
        rows.append([
            tx(item['name'],              size=8.5),
            tx(item['desc'],              size=8.5),
            tx(f"{item['price']:,}원",    size=8.5, align=2),
            tx(item['qty'],               size=8.5, align=1),
            tx(item['unit'],              size=8.5, align=1),
            tx(f"{item['amount']:,}원",   size=8.5, align=2),
        ])
    while len(rows) < 6:
        rows.append([tx('', size=8.5)] * 6)

    item_t = Table(rows, colWidths=cw, repeatRows=1)
    item_t.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0),  RED),
        ('ROWBACKGROUNDS',(0,1), (-1,-1), [WHITE, LGRAY]),
        ('GRID',          (0,0), (-1,-1), 0.4, colors.HexColor('#DDDDDD')),
        ('LINEBELOW',     (0,-1),(-1,-1), 0.8, colors.HexColor('#BBBBBB')),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING',   (0,0), (-1,-1), 5),
        ('RIGHTPADDING',  (0,0), (-1,-1), 5),
    ]))
    E.append(item_t)
    E.append(Spacer(1, 7*mm))

    # ── Bottom: notes (left) + summary (right) ────────────────────────────────
    note_lines = ['<b>참고사항</b><br/><br/>']
    for i, n in enumerate(DEFAULT_NOTES, 1):
        note_lines.append(f'{i}. {escape(n)}<br/>')
    if data.get('note'):
        note_lines.append(f'<br/>{escape(data["note"])}')
    notes_para = tx_html(''.join(note_lines), size=8.5, leading=16)

    disc_str = f'-{sm["discount"]:,}원' if sm['discount'] > 0 else '-'
    # summary table: 46+30 = 76mm
    sum_rows = [
        [tx('총 합계',            size=8.5, align=2), tx(f'{sm["items_total"]:,}원', size=8.5, align=2)],
        [tx('할인',               size=8.5, align=2), tx(disc_str,                   size=8.5, align=2)],
        [tx('공급가액',            size=8.5, align=2), tx(f'{sm["supply"]:,}원',      size=8.5, align=2)],
        [tx('VAT (10%)',          size=8.5, align=2), tx(f'{sm["vat"]:,}원',          size=8.5, align=2)],
        [tx('최종 견적 (VAT 포함)', size=9,   bold=True, align=2, color=WHITE),
         tx(f'{sm["final"]:,}원', size=9,   bold=True, align=2, color=WHITE)],
    ]
    sum_t = Table(sum_rows, colWidths=[46*mm, 30*mm])
    sum_t.setStyle(TableStyle([
        ('GRID',          (0,0), (-1,-2), 0.4, colors.HexColor('#CCCCCC')),
        ('BOX',           (0,0), (-1,-1), 0.4, colors.HexColor('#CCCCCC')),
        ('ROWBACKGROUNDS',(0,0), (-1,-2), [WHITE, MGRAY, WHITE, MGRAY]),
        ('BACKGROUND',    (0,4), (-1,4),  RED),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING',  (0,0), (-1,-1), 7),
        ('LEFTPADDING',   (0,0), (-1,-1), 5),
    ]))

    # 94+76 = 170mm
    bottom = Table([[notes_para, sum_t]], colWidths=[94*mm, 76*mm])
    bottom.setStyle(TableStyle([
        ('VALIGN',       (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING',   (0,0), (-1,-1), 0),
        ('BOTTOMPADDING',(0,0), (-1,-1), 0),
    ]))
    E.append(bottom)

    doc.build(E)
    return filepath, filename

# ── Modal ─────────────────────────────────────────────────────────────────────
def build_modal(channel_id):
    return {
        'type': 'modal', 'callback_id': 'quote_modal',
        'private_metadata': channel_id,
        'title':  {'type': 'plain_text', 'text': '견적서 생성'},
        'submit': {'type': 'plain_text', 'text': '📄 생성'},
        'close':  {'type': 'plain_text', 'text': '취소'},
        'blocks': [
            {'type':'input','block_id':'client',
             'label':{'type':'plain_text','text':'수신자 (거래처명)'},
             'element':{'type':'plain_text_input','action_id':'value',
                        'placeholder':{'type':'plain_text','text':'예: ABC병원'}}},
            {'type':'input','block_id':'quote_name',
             'label':{'type':'plain_text','text':'견적명 (파일명에 사용)'},
             'element':{'type':'plain_text_input','action_id':'value',
                        'placeholder':{'type':'plain_text','text':'예: 대만 KOC 마케팅'}}},
            {'type':'input','block_id':'tax_type',
             'label':{'type':'plain_text','text':'부가세'},
             'element':{'type':'static_select','action_id':'value',
                        'initial_option':{'text':{'type':'plain_text','text':'별도 (공급가액 + 10%)'},'value':'별도'},
                        'options':[
                            {'text':{'type':'plain_text','text':'별도 (공급가액 + 10%)'},'value':'별도'},
                            {'text':{'type':'plain_text','text':'포함 (총액 기준 역산)'},'value':'포함'},
                        ]}},
            {'type':'input','block_id':'items',
             'label':{'type':'plain_text','text':'품목  (항목 / 설명 / 단가 / 수량 / 단위)'},
             'element':{'type':'plain_text_input','action_id':'value','multiline':True,
                        'placeholder':{'type':'plain_text',
                                       'text':'시딩 / 왕홍 @kimi_0531 / 5000000 / 1 / EA\nThreads 바이럴 / 20건 운영 / 800000 / 1 / EA'}}},
            {'type':'input','block_id':'discount','optional':True,
             'label':{'type':'plain_text','text':'할인 금액 (없으면 비워두세요)'},
             'element':{'type':'plain_text_input','action_id':'value',
                        'placeholder':{'type':'plain_text','text':'예: 500000'}}},
            {'type':'input','block_id':'note','optional':True,
             'label':{'type':'plain_text','text':'추가 참고사항 (선택)'},
             'element':{'type':'plain_text_input','action_id':'value','multiline':True,
                        'placeholder':{'type':'plain_text','text':'추가로 기재할 내용이 있으면 입력하세요'}}},
        ],
    }

# ── Slash command + modal handler ─────────────────────────────────────────────
@bolt_app.command('/견적서')
def open_modal(ack, client, command):
    ack()
    client.views_open(trigger_id=command['trigger_id'], view=build_modal(command['channel_id']))

@bolt_app.view('quote_modal')
def handle_submission(ack, body, client):
    ack()
    vals = body['view']['state']['values']
    channel_id = body['view']['private_metadata']

    def v(block):
        f = vals.get(block, {}).get('value', {})
        if not f: return ''
        if f.get('type') == 'static_select':
            return (f.get('selected_option') or {}).get('value', '별도')
        return f.get('value') or ''

    items    = parse_items(v('items'))
    discount = parse_number(v('discount')) if v('discount') else 0

    if not v('client'):
        client.chat_postMessage(channel=channel_id, text='❌ 수신자(거래처명)를 입력해주세요.')
        return
    if not items:
        client.chat_postMessage(channel=channel_id,
            text='❌ 품목을 한 줄 이상 입력해주세요.\n형식: `항목 / 설명 / 단가 / 수량 / 단위`')
        return

    data = {'client': v('client'), 'quote_name': v('quote_name'), 'tax_type': v('tax_type'),
            'items': items, 'discount': discount, 'note': v('note')}
    try:
        filepath, filename = generate_pdf(data)
        sm = calc_summary(items, discount, data['tax_type'])
        with open(filepath, 'rb') as f:
            client.files_upload_v2(
                channel=channel_id, file=f, filename=filename,
                title=f"{data['client']} 외주 견적서",
                initial_comment=(f"✅ *{data['client']}* 견적서가 생성되었습니다.\n"
                                 f"💰 최종 견적 (VAT 포함): {sm['final']:,}원"),
            )
        os.remove(filepath)
    except Exception as exc:
        logger.error('PDF error: %s', exc, exc_info=True)
        client.chat_postMessage(channel=channel_id,
            text=f'❌ 견적서 생성 중 오류가 발생했습니다.\n```{exc}```')

# ── Entry point ───────────────────────────────────────────────────────────────
def _run_web():
    web_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)), use_reloader=False)

if __name__ == '__main__':
    logger.info('Quote automation bot started')
    threading.Thread(target=_run_web, daemon=True).start()
    SocketModeHandler(bolt_app, _app_token).start()
