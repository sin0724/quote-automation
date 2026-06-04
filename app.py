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
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Korean CID font — no external font files needed
pdfmetrics.registerFont(UnicodeCIDFont('HYSMyeongJo-Medium'))
KR_FONT = 'HYSMyeongJo-Medium'

COMPANY_NAME = os.environ.get('COMPANY_NAME', '회사명')
COMPANY_REPRESENTATIVE = os.environ.get('COMPANY_REPRESENTATIVE', '대표자')
COMPANY_BUSINESS_NUMBER = os.environ.get('COMPANY_BUSINESS_NUMBER', '')
COMPANY_ADDRESS = os.environ.get('COMPANY_ADDRESS', '')
COMPANY_CONTACT = os.environ.get('COMPANY_CONTACT', '')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/tmp/output')

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Flask health-check server ─────────────────────────────────────────────────

web_app = Flask(__name__)

@web_app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@web_app.route('/')
def root():
    return 'ok'

# ── Slack Bolt app ────────────────────────────────────────────────────────────

_bot_token = os.environ.get('SLACK_BOT_TOKEN')
_app_token = os.environ.get('SLACK_APP_TOKEN')
if not _bot_token or not _app_token:
    missing = [k for k, v in [('SLACK_BOT_TOKEN', _bot_token), ('SLACK_APP_TOKEN', _app_token)] if not v]
    raise SystemExit(f"[ERROR] 환경변수가 설정되지 않았습니다: {', '.join(missing)}\nRailway Variables 탭에서 추가하세요.")

bolt_app = App(token=_bot_token)

# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_number(s: str) -> int:
    cleaned = re.sub(r'[,원₩\s]', '', s.strip())
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def parse_quote(text: str) -> dict:
    data = {
        'client': '',
        'quote_name': '',
        'manager': '',
        'tax_type': '별도',
        'items': [],
        'note': '',
    }
    in_items = False

    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line:
            continue

        kv = re.match(r'^([가-힣A-Za-z]+)\s*:\s*(.*)', line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            in_items = False
            if key == '거래처명':
                data['client'] = val
            elif key == '견적명':
                data['quote_name'] = val
            elif key == '담당자':
                data['manager'] = val
            elif key == '부가세':
                data['tax_type'] = val
            elif key == '품목':
                in_items = True
            elif key == '비고':
                data['note'] = val
        elif in_items and re.match(r'^[-•*]\s*', line):
            item_text = re.sub(r'^[-•*]\s*', '', line)
            parts = [p.strip() for p in item_text.split('/')]
            if len(parts) >= 2:
                name = parts[0]
                if len(parts) == 2:
                    qty, price = 1, parse_number(parts[1])
                else:
                    qty = max(1, parse_number(parts[1]))
                    price = parse_number(parts[2])
                data['items'].append({
                    'name': name,
                    'qty': qty,
                    'price': price,
                    'amount': qty * price,
                })

    return data


def calc_tax(items: list, tax_type: str) -> tuple:
    total = sum(i['amount'] for i in items)
    if '포함' in tax_type:
        grand = total
        supply = int(grand / 1.1)
        tax = grand - supply
    else:
        supply = total
        tax = int(supply * 0.1)
        grand = supply + tax
    return supply, tax, grand


def fmt(n: int) -> str:
    return f'{n:,}'


def cell(text, size=9, align=0, color=colors.black):
    style = ParagraphStyle(
        'c',
        fontName=KR_FONT,
        fontSize=size,
        leading=size * 1.5,
        alignment=align,
        textColor=color,
    )
    return Paragraph(escape(str(text)), style)

# ── PDF generation ────────────────────────────────────────────────────────────

def generate_pdf(data: dict) -> tuple:
    today = datetime.now().strftime('%Y-%m-%d')

    def safe(s):
        return re.sub(r'[\\/*?:"<>|]', '_', s)

    filename = f"{today}_{safe(data['client'])}_{safe(data['quote_name'])}.pdf"
    filepath = os.path.join(OUTPUT_DIR, filename)

    supply, tax, grand = calc_tax(data['items'], data['tax_type'])

    doc = SimpleDocTemplate(
        filepath, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )

    BLUE  = colors.HexColor('#4472C4')
    GRAY  = colors.HexColor('#E8E8E8')
    LGRAY = colors.HexColor('#F5F5F5')
    WHITE = colors.white

    elems = []

    # Title
    elems.append(Paragraph(
        '견  적  서',
        ParagraphStyle('title', fontName=KR_FONT, fontSize=22, leading=30, alignment=1),
    ))
    elems.append(Spacer(1, 6*mm))

    # Header info  (22+58+8+22+60 = 170 mm)
    cw_info = [22*mm, 58*mm, 8*mm, 22*mm, 60*mm]
    info_rows = [
        [cell('거래처명'), cell(data['client']),            '', cell('공급사'),    cell(COMPANY_NAME)],
        [cell('견적명'),   cell(data['quote_name']),        '', cell('대표자'),    cell(COMPANY_REPRESENTATIVE)],
        [cell('견적일'),   cell(today),                     '', cell('사업자번호'), cell(COMPANY_BUSINESS_NUMBER)],
        [cell('담당자'),   cell(data['manager']),           '', cell('주소'),      cell(COMPANY_ADDRESS)],
        [cell(''),         cell(''),                        '', cell('연락처'),    cell(COMPANY_CONTACT)],
    ]
    info_t = Table(info_rows, colWidths=cw_info)
    info_t.setStyle(TableStyle([
        ('BACKGROUND',  (0, 0), (0, -1), GRAY),
        ('BACKGROUND',  (3, 0), (3, -1), GRAY),
        ('BOX',         (0, 0), (1, -1), 0.5, colors.grey),
        ('INNERGRID',   (0, 0), (1, -1), 0.3, colors.lightgrey),
        ('BOX',         (3, 0), (4, -1), 0.5, colors.grey),
        ('INNERGRID',   (3, 0), (4, -1), 0.3, colors.lightgrey),
        ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',  (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
    ]))
    elems.append(info_t)
    elems.append(Spacer(1, 6*mm))

    # Items table  (12+84+18+30+26 = 170 mm)
    cw_items = [12*mm, 84*mm, 18*mm, 30*mm, 26*mm]
    rows = [[
        cell('No',  align=1, color=WHITE),
        cell('품목',        color=WHITE),
        cell('수량', align=2, color=WHITE),
        cell('단가', align=2, color=WHITE),
        cell('금액', align=2, color=WHITE),
    ]]
    for i, item in enumerate(data['items'], 1):
        rows.append([
            cell(i,                   align=1),
            cell(item['name']),
            cell(fmt(item['qty']),    align=2),
            cell(fmt(item['price']),  align=2),
            cell(fmt(item['amount']), align=2),
        ])
    while len(rows) < 8:
        rows.append([cell('')] * 5)

    item_t = Table(rows, colWidths=cw_items)
    item_t.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1,  0), BLUE),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [WHITE, LGRAY]),
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
    ]))
    elems.append(item_t)
    elems.append(Spacer(1, 2*mm))

    # Totals  (130+40 = 170 mm)
    total_rows = [
        [cell('공급가액',       align=2), cell(f'₩ {fmt(supply)}', align=2)],
        [cell('부가세 (VAT)',   align=2), cell(f'₩ {fmt(tax)}',    align=2)],
        [cell('합   계', size=11, align=2, color=WHITE),
         cell(f'₩ {fmt(grand)}', size=11, align=2, color=WHITE)],
    ]
    total_t = Table(total_rows, colWidths=[130*mm, 40*mm])
    total_t.setStyle(TableStyle([
        ('GRID',          (0, 0), (-1, -1), 0.4, colors.grey),
        ('BACKGROUND',    (0, 2), (-1,  2), BLUE),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
    ]))
    elems.append(total_t)

    if data['note']:
        elems.append(Spacer(1, 5*mm))
        note_t = Table(
            [[cell('비고'), cell(data['note'])]],
            colWidths=[22*mm, 148*mm],
        )
        note_t.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (0, 0), GRAY),
            ('GRID',          (0, 0), (-1, -1), 0.4, colors.grey),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ]))
        elems.append(note_t)

    doc.build(elems)
    return filepath, filename

# ── Slack command ─────────────────────────────────────────────────────────────

HELP_TEXT = (
    '*📄 견적서 봇 사용법*\n\n'
    '`/견적서` 뒤에 아래 형식으로 입력하세요:\n\n'
    '```\n'
    '거래처명: ABC병원\n'
    '견적명: 대만 KOC 마케팅\n'
    '담당자: 김민수\n'
    '부가세: 포함\n'
    '품목:\n'
    '- 대만 KOC 체험단 10명 / 1 / 1500000\n'
    '- Threads 바이럴 20건 / 1 / 800000\n'
    '비고: 유효기간 7일\n'
    '```\n\n'
    '*부가세:* `포함` 또는 `별도`\n'
    '*품목 형식:* `품목명 / 수량 / 단가`\n'
    '*테스트:* `/견적서 테스트`'
)

TEST_INPUT = (
    '거래처명: 테스트 고객사\n'
    '견적명: 테스트 견적서\n'
    '담당자: 홍길동\n'
    '부가세: 별도\n'
    '품목:\n'
    '- 테스트 서비스 A / 2 / 500000\n'
    '- 테스트 서비스 B / 1 / 300000\n'
    '비고: 테스트용 견적서입니다'
)


@bolt_app.command('/견적서')
def handle_quote(ack, say, command, client):
    ack()
    text = command.get('text', '').strip()
    channel = command['channel_id']

    if not text or text.lower() in ['도움말', 'help', '사용법']:
        say(HELP_TEXT)
        return

    if text.lower() == '테스트':
        text = TEST_INPUT

    data = parse_quote(text)

    missing = [
        label for label, val in [
            ('거래처명', data['client']),
            ('견적명',   data['quote_name']),
            ('품목',     data['items']),
        ]
        if not val
    ]
    if missing:
        say(
            f"❌ 필수 항목이 누락되었습니다: {', '.join(missing)}\n"
            f"`/견적서 도움말`로 사용법을 확인하세요."
        )
        return

    try:
        filepath, filename = generate_pdf(data)
        _, _, grand = calc_tax(data['items'], data['tax_type'])

        with open(filepath, 'rb') as f:
            client.files_upload_v2(
                channel=channel,
                file=f,
                filename=filename,
                title=f"{data['client']} - {data['quote_name']} 견적서",
                initial_comment=(
                    f"✅ *{data['client']}* 견적서가 생성되었습니다.\n"
                    f"📋 견적명: {data['quote_name']}\n"
                    f"💰 합계: ₩{fmt(grand)}"
                ),
            )
        os.remove(filepath)

    except Exception as exc:
        logger.error('PDF generation error: %s', exc, exc_info=True)
        say(
            f"❌ 견적서 생성 중 오류가 발생했습니다.\n"
            f"```{exc}```\n"
            f"관리자에게 문의하세요."
        )

# ── Entry point ───────────────────────────────────────────────────────────────

def _run_web():
    port = int(os.environ.get('PORT', 3000))
    web_app.run(host='0.0.0.0', port=port, use_reloader=False)


if __name__ == '__main__':
    logger.info('Quote automation bot started')
    threading.Thread(target=_run_web, daemon=True).start()
    SocketModeHandler(bolt_app, _app_token).start()
