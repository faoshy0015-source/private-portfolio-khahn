"""HanaV Private Portfolio V1 -- personal/local pilot, NOT approved client service."""
from __future__ import annotations
from datetime import datetime
from io import BytesIO
import hashlib
import hmac
import os
import math
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from core import COLUMNS,normalize_portfolio,enrich_positions,portfolio_summary,risk_findings,build_individual_note,to_csv_bytes
from services import fetch_history_batch,fetch_fx_krw_per_usd,fetch_google_news,fetch_news_batch



# ---- Included inline to prevent Streamlit Cloud missing-module crashes ----
import re

CASH_CODE = 'HANAV_CASH_KRW'

COLUMN_ALIASES = {
    '평균단가':'매입단가', '평균매입단가':'매입단가', '평균매수가':'매입단가',
    '현금잔고':'현금', '원화현금':'현금', '예수금':'현금',
}

def _canonical(column):
    raw=str(column).strip()
    compact=re.sub(r'[\s_]+','',raw)
    return COLUMN_ALIASES.get(compact, raw)

def _number(value):
    if pd.isna(value) or str(value).strip()=='':
        return None
    v=str(value).strip().replace(',','').replace('원','').replace('₩','')
    try:
        n=float(v)
    except (TypeError,ValueError) as exc:
        raise ValueError(f'현금은 숫자로 입력하세요: {value!r}') from exc
    if not math.isfinite(n) or n<0:
        raise ValueError(f'현금은 0 이상의 유효한 금액이어야 합니다: {value!r}')
    return n

def parse_portfolio_file(frame: pd.DataFrame):
    """Return stock rows for core, customer cash totals, and import warnings.

    Repeated equal cash amounts per customer are interpreted as one customer
    balance (never summed); conflicting balances are rejected.
    """
    if frame is None or frame.empty:
        raise ValueError('입력된 데이터가 없습니다.')
    df=frame.copy()
    df.columns=[_canonical(c) for c in df.columns]
    if df.columns.duplicated().any():
        raise ValueError('평균단가/매입단가 등 같은 의미의 열이 중복되어 있습니다.')
    df=df.dropna(how='all')
    if df.empty:
        raise ValueError('실제 데이터 행을 입력해 주세요.')
    if '고객구분' not in df:
        for alias in ('고객','고객명','계좌구분','계좌별칭'):
            if alias in df:
                df=df.rename(columns={alias:'고객구분'})
                break
    if '고객구분' not in df:
        df['고객구분']='고객 A'
    df['고객구분']=df['고객구분'].fillna('고객 A').astype(str).str.strip()
    df.loc[df['고객구분'].isin(['','nan','None']),'고객구분']='고객 A'
    cash={}
    if '현금' in df:
        for cust,g in df.groupby('고객구분',sort=False):
            amounts={round(n,8) for n in (_number(v) for v in g['현금']) if n is not None}
            if len(amounts)>1:
                raise ValueError(f'고객구분 {cust}: 현금 금액이 서로 다릅니다. 고객별 현금잔고 하나만 입력해 주세요.')
            if amounts:
                cash[cust]=float(next(iter(amounts)))
    # Keep cash-only rows out of security normalization.
    code_field=next((c for c in ('종목코드','코드','ticker','symbol','티커','단축코드') if c in df),None)
    if code_field is None:
        raise ValueError('필수 열 누락: 종목코드')
    valid=df[code_field].notna() & df[code_field].astype(str).str.strip().ne('')
    stocks=df.loc[valid].drop(columns=['현금'],errors='ignore').copy()
    if stocks.empty:
        raise ValueError('유효한 보유종목이 없습니다. 현금만 있는 파일은 현재 버전에서 분석할 수 없습니다.')
    normalized,warnings=normalize_portfolio(stocks)
    # All six fields are enough for an upload; pricing comes from public quotes.
    simplified=set(('고객구분','종목코드','종목명','보유수량','매입단가','현금')).issuperset(set(df.columns))
    return normalized[COLUMNS].copy(), cash, warnings, simplified

def add_customer_cash(stock_positions:pd.DataFrame,cash_by_customer:dict,fx:float|None):
    """Append cash as explicitly identified KRW asset for allocation, not as equity.

    Stock results remain separate for research/news; cash is treated as 0% P/L.
    """
    rows=[]
    for cust,amount in cash_by_customer.items():
        if not amount or amount<=0:
            continue
        row={c:None for c in COLUMNS}
        row.update({'고객구분':cust,'종목코드':CASH_CODE,'종목명':'현금 (원화)',
                    '시장':'CASH','통화':'KRW','보유수량':1.0,
                    '매입단가':amount,'기준가':amount,'업종':'현금',
                    '종목유형':'현금','비고':'고객별 현금잔고 · 시세 조회 제외'})
        row['티커']=CASH_CODE
        rows.append(row)
    if not rows:
        return stock_positions.copy()
    cash_df=enrich_positions(pd.DataFrame(rows),{},fx)
    cash_df['가격출처']='엑셀 입력 현금잔고'
    cash_df['가격기준일']='사용자 입력'
    assets=pd.concat([stock_positions,cash_df],ignore_index=True)
    total=assets['평가액(원)'].sum(min_count=1)
    assets['평가액비중(%)']=assets['평가액(원)']/total*100 if pd.notna(total) and total>0 else np.nan
    return assets

MANUAL_COLUMNS = ['고객구분', '종목코드', '종목명', '보유수량', '평균단가', '현금']

def blank_manual_rows(count: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        '고객구분': pd.Series([None] * count, dtype='object'),
        '종목코드': pd.Series([None] * count, dtype='object'),
        '종목명': pd.Series([None] * count, dtype='object'),
        '보유수량': pd.Series([float('nan')] * count, dtype='float64'),
        '평균단가': pd.Series([float('nan')] * count, dtype='float64'),
        '현금': pd.Series([float('nan')] * count, dtype='float64'),
    })

def _text(value) -> str:
    if pd.isna(value):
        return ''
    return str(value).strip()

def parse_manual_rows(rows: pd.DataFrame):
    """Validate direct entry and return the existing common normalized import tuple.

    Requiring an explicit customer alias prevents accidentally merging entries for
    different clients under core's fallback '고객 A' label.
    """
    if rows is None or not isinstance(rows, pd.DataFrame):
        raise ValueError('입력할 표가 없습니다.')
    missing = [c for c in MANUAL_COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError('입력 열 누락: ' + ', '.join(missing))
    data = rows[MANUAL_COLUMNS].copy()
    meaningful = []
    for i, row in data.iterrows():
        code = _text(row['종목코드'])
        cash = _text(row['현금'])
        # A row with a code or cash is an actual data row. Stock names typed
        # without codes must not be silently ignored.
        active = bool(code or cash or _text(row['종목명']) or
                      _text(row['보유수량']) or _text(row['평균단가']))
        if not active:
            continue
        if not _text(row['고객구분']):
            raise ValueError(f'{i+1}번째 행: 고객구분(별칭)을 입력해 주세요.')
        if not code and (_text(row['종목명']) or _text(row['보유수량']) or _text(row['평균단가'])):
            raise ValueError(f'{i+1}번째 행: 보유종목 정보를 입력했다면 종목코드가 필요합니다.')
        meaningful.append(i)
    if not meaningful:
        raise ValueError('고객구분·종목코드·수량 등을 입력한 후 반영해 주세요.')
    data = data.loc[meaningful].reset_index(drop=True)
    return parse_portfolio_file(data)

def current_to_manual_rows(portfolio: pd.DataFrame, cash: dict) -> pd.DataFrame:
    """Seed the six-column editor with current portfolio, showing cash once/client."""
    if portfolio is None or portfolio.empty:
        return blank_manual_rows()
    result = []
    cash_seen = set()
    for _, p in portfolio.iterrows():
        client = _text(p.get('고객구분')) or '고객 A'
        code = _text(p.get('종목코드'))
        # Explicit Yahoo suffix is used for KOSDAQ so that six columns suffice.
        market = _text(p.get('시장')).upper()
        if code.isdigit() and market in {'KOSDAQ', 'KQ', '코스닥'}:
            code = code.zfill(6) + '.KQ'
        rec = {
            '고객구분': client,
            '종목코드': code,
            '종목명': _text(p.get('종목명')),
            '보유수량': p.get('보유수량'),
            '평균단가': p.get('매입단가'),
            '현금': None,
        }
        if client in cash and client not in cash_seen:
            rec['현금'] = cash[client]
            cash_seen.add(client)
        result.append(rec)
    # Add cash-only rows for other clients; cannot be applied alone without stocks.
    for client, amount in cash.items():
        if client not in cash_seen:
            result.append({'고객구분':client, '종목코드':None, '종목명':None,
                           '보유수량':None, '평균단가':None, '현금':amount})
    out = pd.DataFrame(result, columns=MANUAL_COLUMNS)
    for col in ('보유수량', '평균단가', '현금'):
        out[col] = pd.to_numeric(out[col], errors='coerce')
    return out

# ---- Editable Excel backup for six-column in-app portfolio draft (stdlib-only XLSX writer) ----
def manual_rows_excel_bytes(rows: pd.DataFrame) -> bytes:
    """Export even *unfinished* editor rows; do not require portfolio validation.

    Important: emit codes/aliases/names as literal Excel strings, never formulas.
    A downloaded file stays on the operator's PC (not on the Streamlit server).
    """
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape as xml_escape

    data = rows.reindex(columns=MANUAL_COLUMNS).copy()
    # Empty editor placeholder rows are excluded; a partially entered row is kept.
    data = data.loc[data.apply(lambda row: any(
        not pd.isna(v) and str(v).strip() != '' for v in row
    ), axis=1)]

    def clean_xml(v: object) -> str:
        # Excel XML 1.0 can't contain raw control characters.
        v = str(v)
        return ''.join(ch for ch in v if ord(ch) >= 32 or ch in ('\t', '\n', '\r'))

    def letter(col_zero: int) -> str:
        return 'ABCDEF'[col_zero]

    def text_cell(address: str, value: object, style: int = 2) -> str:
        escaped = xml_escape(clean_xml(value))
        return (f'<c r="{address}" s="{style}" t="inlineStr">'
                f'<is><t xml:space="preserve">{escaped}</t></is></c>')

    rows_xml = ['<row r="1" ht="26" customHeight="1">' +
                ''.join(text_cell(f'{letter(i)}1', name, 1)
                        for i,name in enumerate(MANUAL_COLUMNS)) + '</row>']
    for idx, (_, record) in enumerate(data.iterrows(), start=2):
        cells = []
        for j, column in enumerate(MANUAL_COLUMNS):
            v = record[column]
            if pd.isna(v) or str(v).strip() == '':
                continue
            ref = f'{letter(j)}{idx}'
            if column in ('고객구분', '종목코드', '종목명'):
                cells.append(text_cell(ref, v))
            else:
                try:
                    num = float(v)
                    if math.isfinite(num):
                        cells.append(f'<c r="{ref}" s="3"><v>{num:.15g}</v></c>')
                    else:
                        cells.append(text_cell(ref, v))
                except (ValueError, TypeError, OverflowError):
                    # Unfinished drafts remain faithfully restorable, errors are
                    # checked only when applying to the portfolio.
                    cells.append(text_cell(ref, v))
        rows_xml.append(f'<row r="{idx}">' + ''.join(cells) + '</row>')

    last = max(1, len(rows_xml))
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="19"/>'
        '<cols><col min="1" max="1" width="19" customWidth="1"/>'
        '<col min="2" max="2" width="20" customWidth="1"/>'
        '<col min="3" max="3" width="26" customWidth="1"/>'
        '<col min="4" max="4" width="15" customWidth="1"/>'
        '<col min="5" max="5" width="19" customWidth="1"/>'
        '<col min="6" max="6" width="19" customWidth="1"/></cols>'
        '<sheetData>' + ''.join(rows_xml) + '</sheetData>'
        f'<autoFilter ref="A1:F{last}"/>'
        '</worksheet>'
    )
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
             '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
             '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
             '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="보유현황" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font>'
              '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="맑은 고딕"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FF136D53"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="4">'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/>'
              '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              '</styleSheet>')
    files = {
        '[Content_Types].xml':types, '_rels/.rels':rels,
        'xl/workbook.xml':workbook, 'xl/_rels/workbook.xml.rels':wb_rels,
        'xl/worksheets/sheet1.xml':worksheet, 'xl/styles.xml':styles,
    }
    memory = BytesIO()
    with ZipFile(memory, 'w', compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode('utf-8'))
    return memory.getvalue()


def manual_draft_from_excel(excel_bytes: bytes) -> pd.DataFrame:
    """Read an exported draft *without* requiring finished/valid holdings."""
    imported = pd.read_excel(BytesIO(excel_bytes), sheet_name=0, dtype={'종목코드': str})
    imported.columns = [str(c).strip() for c in imported.columns]
    missing = [col for col in MANUAL_COLUMNS if col not in imported.columns]
    if missing:
        raise ValueError('입력표 복구에 필요한 열이 없습니다: ' + ', '.join(missing))
    result = imported[MANUAL_COLUMNS].copy()
    # Excel sometimes interprets numeric codes as numbers; preserve 6-digit codes.
    result['종목코드'] = result['종목코드'].map(
        lambda value: (str(value).strip().zfill(6) if str(value).strip().isdigit()
                       and len(str(value).strip()) <= 6 else str(value).strip())
        if not pd.isna(value) else None
    )
    for name in ('보유수량', '평균단가', '현금'):
        result[name] = pd.to_numeric(result[name], errors='coerce')
    if result.empty:
        return blank_manual_rows()
    return result.reset_index(drop=True)
# ---- End editable Excel backup helpers ----

# ---- End built-in direct-entry and six-column import helpers ----

st.set_page_config(page_title='HanaV Private Portfolio',page_icon='💠',layout='wide',initial_sidebar_state='expanded')
st.markdown('''<style>
/* HanaV Private Portfolio V1.0.15 — draft save and resume.
   This visual layer intentionally works even if GitHub retains the old dark
   .streamlit/config.toml. No changes to financial calculations or uploads. */
:root {
  --hanav-bg:#1C4036;
  --hanav-bg-deep:#18372E;
  --hanav-panel:#285447;
  --hanav-panel-hover:#306452;
  --hanav-sidebar:#17382F;
  --hanav-border:#447969;
  --hanav-line:#355E50;
  --hanav-green:#66E5B4;
  --hanav-white:#F6FFF9;
  --hanav-text:#EBFAF1;
  --hanav-muted:#C6DFD3;
}
html,body,.stApp,div[data-testid="stApp"],div[data-testid="stMain"],
[data-testid="stAppViewContainer"],section[data-testid="stMain"] {
  background:var(--hanav-bg) !important;
  color:var(--hanav-text) !important;
}
[data-testid="stAppViewContainer"] {
  background:linear-gradient(135deg,#1C4036 0%,#234B3D 54%,#1D4038 100%) !important;
}
/* Cloud header: eliminate the white stripe without hiding essential controls. */
header[data-testid="stHeader"], [data-testid="stHeader"],
.stAppHeader, [data-testid="stToolbar"], .stAppToolbar {
  background:#1C4036 !important;
  color:var(--hanav-white) !important;
  box-shadow:none !important;
  border-color:transparent !important;
}
[data-testid="stHeader"] button,[data-testid="stToolbar"] button,
.stAppHeader button,.stAppToolbar button {
  background:transparent !important;
  color:var(--hanav-white) !important;
}
[data-testid="stHeader"] svg,[data-testid="stToolbar"] svg,
.stAppHeader svg,.stAppToolbar svg {color:var(--hanav-white) !important;fill:currentColor;}
[data-testid="stDecoration"] {background:#1C4036 !important;box-shadow:none !important;}
/* Use a little more breathing room without sacrificing chart space. */
.block-container {max-width:1480px;padding-top:1.5rem;padding-bottom:2.4rem;}
h1,h2,h3,h4,h5,h6 {color:var(--hanav-white) !important;letter-spacing:-.025em;}
p,li,label,[data-testid="stMarkdownContainer"],
[data-testid="stWidgetLabel"],div[data-testid="stText"] {color:var(--hanav-text);}
.stCaption,[data-testid="stCaptionContainer"],small,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {color:var(--hanav-muted) !important;}
a {color:#92F2CF;} a:hover{color:#B7FFE2;}
/* Main banner: slightly lighter, refined and readable. */
.kicker {font-size:12px;letter-spacing:.16em;color:#8AF5C6;font-weight:800;}
.hero {
  background:linear-gradient(110deg,#28624D 0%,#285A4B 58%,#224A3E 100%);
  border:1px solid #579780;
  border-left:5px solid #76EEC0;
  border-radius:16px;
  padding:22px 25px;
  margin:0 0 19px;
  box-shadow:0 10px 25px rgba(7,27,20,.10);
}
.hero h1 {font-size:2.07rem;color:#FFFFFF !important;margin:.2rem 0 .43rem;line-height:1.2;}
.hero p {color:#D8F3E5 !important;margin:0;font-size:14px;}
.info {background:#2B5B4B;border:1px solid #538A77;border-radius:12px;padding:14px 17px;}
/* Sidebar: differentiated from canvas, but no near-black text. */
section[data-testid="stSidebar"], [data-testid="stSidebar"],
[data-testid="stSidebar"] > div, [data-testid="stSidebarContent"] {
  background:var(--hanav-sidebar) !important;
  color:var(--hanav-text) !important;
}
[data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,[data-testid="stSidebar"] h4,
[data-testid="stSidebar"] p,[data-testid="stSidebar"] label,
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] {
  color:var(--hanav-white) !important;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {color:#C4E4D4 !important;}
[data-testid="stSidebar"] hr {border-color:#48705E !important;}
/* Metric tiles. */
[data-testid="stMetric"] {
  background:linear-gradient(160deg,#2A604D 0%,#285245 100%) !important;
  border:1px solid #4D9277 !important;
  padding:15px 16px !important;
  border-radius:14px !important;
  box-shadow:0 5px 18px rgba(6,22,16,.10);
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {color:#D6F1E1 !important;}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {color:#FFFFFF !important;}
[data-testid="stMetricDelta"], [data-testid="stMetricDelta"] * {font-weight:600;}
/* Avoid Streamlit's default ellipsis and use a compact value that fits cards. */
[data-testid="stMetricValue"], [data-testid="stMetricValue"] > div {
  font-size:clamp(1.35rem, 1.85vw, 2.05rem) !important;
  letter-spacing:-.035em !important;
  font-variant-numeric:tabular-nums;
  white-space:nowrap !important;
  text-overflow:clip !important;
}
[data-testid="stMetricValue"] {overflow:visible !important;}
/* Streamlit Markdown news links must stay readable on green panels. */
[data-testid="stMain"] [data-testid="stMarkdownContainer"] a,
[data-testid="stMain"] .stMarkdownContainer a {
  color:#B7FFE2 !important;
  text-decoration:underline !important;
  text-underline-offset:3px;
  font-weight:650;
}
[data-testid="stMain"] [data-testid="stMarkdownContainer"] a:visited {color:#D7D1FF !important;}
[data-testid="stMain"] [data-testid="stMarkdownContainer"] a:hover {color:#FFFFFF !important;}
/* Tabs: make both active and inactive menu labels legible. */
.stTabs [data-baseweb="tab-list"] {gap:12px;border-bottom:1px solid #507D6B !important;}
.stTabs [data-baseweb="tab"],.stTabs button[role="tab"] {
  color:#C5E5D5 !important;
  background:transparent !important;
  border-radius:8px 8px 0 0;
  font-weight:650 !important;
  padding:10px 11px !important;
}
.stTabs [data-baseweb="tab"] *,.stTabs button[role="tab"] * {color:inherit !important;}
.stTabs [data-baseweb="tab"]:hover,.stTabs button[role="tab"]:hover {
  color:#FFFFFF !important;background:rgba(117,225,179,.10) !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"],
.stTabs button[role="tab"][aria-selected="true"] {
  color:#FFFFFF !important;background:rgba(103,230,181,.14) !important;
}
.stTabs [data-baseweb="tab-highlight"] {background:#6CE7B7 !important;height:3px !important;}
.stTabs [data-baseweb="tab-border"] {background:#3C6D58 !important;}
/* Inputs, selects and file upload: remove glaring white boxes. */
[data-baseweb="select"] > div,
[data-baseweb="input"], [data-baseweb="base-input"],
[data-baseweb="textarea"], [data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input {
  background:#2B5949 !important;
  border-color:#5F9A7F !important;
  color:#FFFFFF !important;
  border-radius:9px !important;
}
[data-baseweb="select"] *, [data-baseweb="input"] input,
[data-baseweb="base-input"] input,[data-baseweb="textarea"] textarea {
  color:#FFFFFF !important;
}
[data-baseweb="select"] svg {color:#EAFDF0 !important;}
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="popover"] [role="option"],
[data-baseweb="menu"] {background:#244B3E !important;color:#FFFFFF !important;}
[data-baseweb="popover"] [role="option"]:hover {background:#336D55 !important;}
[data-testid="stFileUploaderDropzone"] {
  background:#294F41 !important;
  border:1px dashed #7CC5A6 !important;
  border-radius:12px !important;
}
[data-testid="stFileUploaderDropzone"] *,
[data-testid="stFileUploaderDropzoneInstructions"] * {color:#F1FFF8 !important;}
/* Buttons. White default buttons previously clashed with the dark dashboard. */
.stButton > button, .stDownloadButton > button,
[data-testid^="stBaseButton-"] {
  background:#315E4E !important;
  color:#FFFFFF !important;
  border:1px solid #70AD90 !important;
  border-radius:9px !important;
  font-weight:600 !important;
  box-shadow:none !important;
}
.stButton > button:hover,.stDownloadButton > button:hover,
[data-testid^="stBaseButton-"]:hover {
  background:#3A755C !important;
  border-color:#97EFC7 !important;
  color:#FFFFFF !important;
}
.stButton > button[kind="primary"],button[data-testid="stBaseButton-primary"] {
  background:#058C65 !important;border-color:#70E8B5 !important;color:#FFFFFF !important;
}
.stButton > button[kind="primary"]:hover,button[data-testid="stBaseButton-primary"]:hover {
  background:#0AA778 !important;border-color:#A2F3D0 !important;
}
button:focus-visible,input:focus-visible,[role="tab"]:focus-visible {
  outline:2px solid #9AF6CF !important;outline-offset:2px;
}
/* Native radios and alert panels. */
[data-testid="stRadio"] label,[data-testid="stRadio"] label p {color:#F0FFF6 !important;}
[data-testid="stAlert"] {border-radius:12px !important;}
[data-testid="stAlert"] [data-testid="stMarkdownContainer"] {color:inherit;}
[data-testid="stExpander"] {border-color:#4D816C !important;border-radius:10px !important;}
hr {border-color:#487662 !important;}
/* Responsive readability */
@media(max-width:700px) {
  .block-container {padding-left:1rem;padding-right:1rem;padding-top:1rem;}
  .hero {padding:17px 18px;}
  .hero h1 {font-size:1.68rem;}
  .stTabs [data-baseweb="tab"] {padding:9px 8px !important;}
}
</style>''',unsafe_allow_html=True)

# ------- Authentication: required even for local pilot. No embedded default password. -------
def cfg(k):
    try:
        val=st.secrets.get(k,None)
        if val is not None:return str(val)
    except Exception:pass
    return os.environ.get(k,'')

password=cfg('APP_PASSWORD')
st.session_state.setdefault('auth',False)
if not password:
    st.error('APP_PASSWORD 미설정: 실행 전에 비밀번호를 설정해야 앱을 열 수 있습니다.')
    st.code('$env:APP_PASSWORD="원하는_긴_비밀번호"\npython -m streamlit run app.py --server.address 127.0.0.1',language='powershell')
    st.stop()
if not st.session_state['auth']:
    st.markdown('<div style="height:9vh"></div>',unsafe_allow_html=True)
    l,c,r=st.columns([1.05,1.15,1.05])
    with c:
        st.markdown('<div class="hero"><div class="kicker">PRIVATE WEALTH INTELLIGENCE</div><h1>HanaV<br>Private Portfolio</h1><p>보유자산을 이해하는 새로운 방법</p></div>',unsafe_allow_html=True)
        with st.form('login'):
            entered=st.text_input('비밀번호',type='password')
            submitted=st.form_submit_button('로그인',use_container_width=True,type='primary')
        if submitted:
            if hmac.compare_digest(entered,password):st.session_state['auth']=True;st.rerun()
            else:st.error('비밀번호가 다릅니다.')
        st.caption('독립 시험용 앱 · 공식 하나증권 고객서비스가 아닙니다.')
    st.stop()

@st.cache_data(ttl=900,show_spinner=False)
def cached_prices(symbols:tuple[str,...]):
    return fetch_history_batch(list(symbols))

@st.cache_data(ttl=1800,show_spinner=False)
def cached_news(ticker,name):
    return fetch_google_news(ticker,name)


# ---- Symbol-only research adapter. Free data may be incomplete or delayed. ----
def _numeric(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError,OverflowError):
        return None

def _amount(v, currency=''):
    x=_numeric(v)
    if x is None:return '미제공'
    unit=currency or ''
    a=abs(x)
    if a>=1e12:return f'{x/1e12:,.2f}조 {unit}'
    if a>=1e8:return f'{x/1e8:,.2f}억 {unit}'
    if a>=1e4:return f'{x/1e4:,.1f}만 {unit}'
    return f'{x:,.2f} {unit}'

def _val(v,suffix='',percent_raw=False):
    x=_numeric(v)
    return '미제공' if x is None else f'{(x*100 if percent_raw else x):,.2f}{suffix}'


def _compact_price(v, currency):
    """Keep the KPI tile short. Exact numeric amount is displayed separately."""
    x=_numeric(v)
    if x is None: return '미조회'
    c=str(currency).upper()
    if c=='KRW':
        if abs(x)>=100_000_000:return f'{x/100_000_000:,.2f}억'
        if abs(x)>=10_000:return f'{x/10_000:,.1f}만'
        return f'{x:,.0f}'
    if abs(x)>=100_000:return f'{x/1000:,.1f}k'
    return f'{x:,.2f}'


def _has_financial_data(result):
    """Check business fundamentals (not quote-only market capitalization)."""
    if not isinstance(result,dict):return False
    info=result.get('info') or {}
    keys=('trailingPE','forwardPE','priceToBook','trailingEps','forwardEps',
          'returnOnEquity','operatingMargins','profitMargins','revenueGrowth','earningsGrowth')
    if any(_numeric(info.get(k)) is not None for k in keys):return True
    if any(any(_numeric(q.get(field)) is not None for field in ('매출액','영업이익','당기순이익','희석EPS'))
           for q in (result.get('quarters') or [])): return True
    return any(_numeric(e.get('추정EPS')) is not None or _numeric(e.get('EPS 성장률')) is not None
               for e in (result.get('eps_estimates') or []))


def _research_status(result, was_attempted=False):
    if not was_attempted and not result:return '미시도'
    if _has_financial_data(result):return '재무 확보'
    if not isinstance(result,dict):return '조회 오류'
    if result.get('errors') or result.get('error'):return '자료 미제공/오류'
    return '재무 미제공'

def _income_rows(df):
    if not isinstance(df,pd.DataFrame) or df.empty:return []
    # yfinance returns CamelCase with pretty=False, and spaced labels with pretty=True.
    aliases={
        '매출액':('TotalRevenue','Total Revenue','OperatingRevenue','Operating Revenue'),
        '영업이익':('OperatingIncome','Operating Income'),
        '당기순이익':('NetIncome','Net Income','NetIncomeCommonStockholders'),
        '희석EPS':('DilutedEPS','Diluted EPS','DilutedEps'),
    }
    rows=[]
    for dt in sorted(df.columns,reverse=True)[:4]:
        row={'기간':str(dt)[:10]}
        for label,choices in aliases.items():
            row[label]=next((_numeric(df.loc[key,dt]) for key in choices if key in df.index
                             and _numeric(df.loc[key,dt]) is not None),None)
        if any(row[label] is not None for label in aliases):rows.append(row)
    return rows

def _estimate_rows(df):
    if not isinstance(df,pd.DataFrame) or df.empty:return []
    rows=[]
    periods={'0q':'이번 분기','+1q':'다음 분기','0y':'올해','+1y':'내년'}
    for idx in ['0q','+1q','0y','+1y']:
        if idx in df.index:
            r=df.loc[idx]
            rows.append({'기간':periods[idx], '추정EPS':_numeric(r.get('avg')),'분석기관수':_numeric(r.get('numberOfAnalysts')),'EPS 성장률':_numeric(r.get('growth'))})
    return rows

@st.cache_data(ttl=3600,show_spinner=False,max_entries=100)
def fetch_selected_research(ticker:str,is_etf:bool=False,include_price:bool=True,cache_buster:str='')->dict:
    """Fetch only the PUBLIC ticker (no holdings, customer ids, account or costs)."""
    import yfinance as yf
    from services import _extract_price_history
    import re
    if not re.fullmatch(r'[A-Z0-9^.=\-]{1,25}',ticker.upper()):
        return {'error':'종목코드 형식이 올바르지 않습니다.','asof':datetime.now().strftime('%Y-%m-%d %H:%M'),'errors':['잘못된 티커']}
    out={'ticker':ticker,'asof':datetime.now().strftime('%Y-%m-%d %H:%M'),'errors':[],
         'quote':{},'info':{},'analyst_targets':{},'quarters':[],'eps_estimates':[], 'price_currency':'','financial_currency':'', 'source_status':{}}
    t=yf.Ticker(ticker)
    if include_price:
        try:
            history=t.history(period='1y',interval='1d',auto_adjust=False,timeout=16)
            from services import _extract_price_history
            out['quote']=_extract_price_history(history)
            if out['quote']:
                close=out['quote'].get('history')
                if isinstance(close,pd.Series) and len(close)>=16:
                    diff=close.diff();up=diff.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
                    down=(-diff.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
                    d=_numeric(down.iloc[-1]);u=_numeric(up.iloc[-1])
                    if d is not None and u is not None:
                        out['quote']['rsi14']=100.0 if d==0 and u>0 else (100-(100/(1+(u/d)))) if d>0 else None
        except Exception as e:out['errors'].append('가격 이력: '+str(e)[:100])
        out['source_status']['가격']= '확보' if out['quote'] else '미제공/오류'
    else:
        out['source_status']['가격']='별도 일괄수집'
    try:
        info=t.get_info() or {}
        keys=['shortName','longName','currency','financialCurrency','sector','industry','longBusinessSummary',
              'marketCap','trailingPE','forwardPE','priceToBook','trailingEps','forwardEps',
              'returnOnEquity','operatingMargins','profitMargins','revenueGrowth','earningsGrowth',
              'dividendYield','totalAssets','fundFamily','category','numberOfAnalystOpinions','recommendationKey']
        out['info']={k:info.get(k) for k in keys if info.get(k) is not None}
        out['price_currency']=str(info.get('currency') or '').upper()
        out['financial_currency']=str(info.get('financialCurrency') or info.get('currency') or '').upper()
    except Exception as e:out['errors'].append('기본 지표: '+str(e)[:100])
    out['source_status']['기본지표']='확보' if out['info'] else '미제공/오류'
    if not is_etf:
        try:
            targets=t.get_analyst_price_targets() or {}
            if isinstance(targets,dict):
                out['analyst_targets']={k:_numeric(targets.get(k)) for k in ('current','low','mean','median','high')}
        except Exception as e:out['errors'].append('목표가 집계: '+str(e)[:100])
        out['source_status']['목표가']='확보' if _numeric(out['analyst_targets'].get('mean')) is not None else '미제공/오류'
        try:out['quarters']=_income_rows(t.get_income_stmt(freq='quarterly'))
        except Exception as e:out['errors'].append('분기 실적: '+str(e)[:100])
        out['source_status']['분기실적']='확보' if out['quarters'] else '미제공/오류'
        try:out['eps_estimates']=_estimate_rows(t.get_earnings_estimate())
        except Exception as e:out['errors'].append('EPS 추정: '+str(e)[:100])
        out['source_status']['EPS추정']='확보' if out['eps_estimates'] else '미제공/오류'
    else:
        out['source_status'].update({'목표가':'ETF 제외','분기실적':'ETF 제외','EPS추정':'ETF 제외'})
    if not _has_financial_data(out):
        out['errors'].append('재무·실적: 공개 공급처 응답에 활용 가능한 수치가 없습니다 (ETF·시장별 제한 또는 접근차단 가능).')
    return out

def _evidence_insights(row:pd.Series,market:dict)->list[str]:
    """Deterministic monitoring commentary; no invented price targets or predictions."""
    notes=[];q=market.get('quote',{}) or {};info=market.get('info',{}) or {}
    p=_numeric(q.get('price'));ma20=_numeric(q.get('ma20'));ma60=_numeric(q.get('ma60'))
    if p is not None and ma20 is not None and ma60 is not None:
        relative='상회' if p>ma20 else '하회'
        notes.append(f'최근 종가가 20일 이동평균을 {relative}합니다. 20일선과 60일선은 '+('상승 배열' if ma20>ma60 else '하락 또는 혼조 배열')+'입니다.')
    if p is not None and _numeric(q.get('high52')):
        draw=(p/q['high52']-1)*100
        notes.append(f'최근 1년 종가 고점 대비 {draw:.1f}% 위치입니다. 고점 갱신 여부와 낙폭 확대를 추적합니다.')
    if _numeric(q.get('ret_1m')) is not None and _numeric(q.get('ret_3m')) is not None:
        notes.append(f"직전 약 1개월 수익률 {q['ret_1m']:+.1f}%, 약 3개월 수익률 {q['ret_3m']:+.1f}%입니다. (거래일 기준)")
    rg=_numeric(info.get('revenueGrowth'));eg=_numeric(info.get('earningsGrowth'))
    if rg is not None:notes.append(f'Yahoo 제공 매출 성장률 {rg*100:+.1f}%입니다. 비교 기간·회계기준을 원문에서 확인해야 합니다.')
    if eg is not None:notes.append(f'Yahoo 제공 순이익 성장률 {eg*100:+.1f}%입니다. 일회성 요인과 추정치 변동을 점검하세요.')
    targets=market.get('analyst_targets',{}) or {}
    tm=_numeric(targets.get('mean'))
    if tm is not None and p is not None and p>0:
        valid_ccy=not market.get('price_currency') or market.get('price_currency')==str(row.get('통화','')).upper()
        if valid_ccy and market.get('price_currency'):notes.append(f'Yahoo 집계 목표가 평균과 최근 종가의 괴리는 {(tm/p-1)*100:+.1f}%입니다. 목표가 발표일과 집계기관을 확인할 수 없어 참고지표로만 사용합니다.')
    if not notes:notes.append('최신 시장데이터를 충분히 확보하지 못했습니다. 종목코드와 시세 제공 상태를 확인하세요.')
    notes.append('관리 원칙: 뉴스·실적·가격 추세를 재점검하고, 적정 비중과 관련 ETF 중복 노출은 담당자가 별도 검토합니다.')
    return notes

@st.cache_data(ttl=1800,show_spinner=False)
def cached_fx():
    return fetch_fx_krw_per_usd()

# Online deployment resilience: Github uploads sometimes omit the samples/ folder.
# The fallback contains ONLY synthetic demonstration rows, never real customer data.
import base64
import zlib
_EMBEDDED_DEMO = (
    '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'
    'vwG6yvB8'
)


# Embedded template ensures that the Excel download works even if a GitHub
# commit contains only app.py (no samples directory). Synthetic and blank only.
_EMBEDDED_XLSX=(
    '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'
)

# Self-contained simple workbook fallback when samples directory was not pushed.
_SIMPLE_TEMPLATE_ZLIB_B64 = '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'

@st.cache_data(show_spinner=False)
def portfolio_template_bytes() -> bytes:
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'samples', 'HanaV_Private_Portfolio_간편양식.xlsx')
    if os.path.isfile(path):
        try:
            with open(path,'rb') as f: return f.read()
        except OSError: pass
    return zlib.decompress(base64.b64decode(_SIMPLE_TEMPLATE_ZLIB_B64))

@st.cache_data(show_spinner=False)
def load_demo():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'samples', 'demo_65.csv')
    if os.path.isfile(path):
        try:
            return pd.read_csv(path, dtype={'종목코드': str})
        except (OSError, UnicodeError, pd.errors.ParserError, ValueError):
            pass
    # The ZIP includes the CSV too. This embedded copy makes the web app work
    # even when the GitHub repo was created from a partial upload.
    raw = zlib.decompress(base64.b64decode(_EMBEDDED_DEMO))
    return pd.read_csv(BytesIO(raw), dtype={'종목코드': str})

if 'portfolio_raw' not in st.session_state:
    st.session_state['portfolio_raw'] = load_demo()
st.session_state.setdefault('is_demo',True)
st.session_state.setdefault('uploaded_hash','')
st.session_state.setdefault('cash_by_customer',{})
st.session_state.setdefault('simple_mode',False)
st.session_state.setdefault('auto_quote_pending',False)
st.session_state.setdefault('auto_fx_pending',False)
st.session_state.setdefault('price_mode_choice','입력 기준가')
st.session_state.setdefault('upload_rev',0)
st.session_state.setdefault('editor_rev',0)
st.session_state.setdefault('quotes',{})
st.session_state.setdefault('news_map',{})
st.session_state.setdefault('deep_research',{})
st.session_state.setdefault('attempted_research',[])
st.session_state.setdefault('report_quotes',{})
st.session_state.setdefault('fx_val',1350.0)  # 가상 65종목 시연 전용; 실제 업로드 시 0으로 초기화
st.session_state.setdefault('fx_source','가상 환율' if st.session_state['is_demo'] else '입력 없음')
st.session_state.setdefault('manual_entry_rows', blank_manual_rows())
st.session_state.setdefault('manual_editor_rev', 0)
st.session_state.setdefault('draft_restore_rev', 0)

def install_portfolio(normalized, cash_by_customer, warnings, is_simple, source_name):
    """Shared in-memory dataset loader for manual input and file uploads."""
    st.session_state['portfolio_raw'] = normalized[COLUMNS].copy()
    st.session_state['cash_by_customer'] = dict(cash_by_customer)
    st.session_state['simple_mode'] = bool(is_simple)
    st.session_state['upload_warnings'] = list(warnings)
    st.session_state['price_mode_choice'] = ('Yahoo 공개 종가 조회'
        if normalized['기준가'].isna().all() else '입력 기준가')
    st.session_state['auto_quote_pending'] = bool(normalized['기준가'].isna().all())
    st.session_state['auto_fx_pending'] = bool(normalized['통화'].eq('USD').any())
    st.session_state['is_demo'] = False
    st.session_state['quotes'] = {}
    st.session_state['quote_errors'] = []
    st.session_state['editor_rev'] += 1
    st.session_state['news_map'] = {}
    st.session_state['deep_research'] = {}
    st.session_state['attempted_research'] = []
    st.session_state['report_quotes'] = {}
    st.session_state.pop('selected_research_index', None)
    st.session_state['fx_val'] = 0.0
    st.session_state['fx_source'] = '입력 없음'
    st.session_state.pop('pdf_bytes', None)
    st.session_state['portfolio_source'] = source_name
    st.session_state['apply_flash'] = (
        f"보유종목 {len(normalized)}개 · 고객별 현금 합계 {sum(cash_by_customer.values()):,.0f}원 반영 완료")

# Applying a direct-entry submission on the NEXT run is intentional. The price
# radio widget is already instantiated when the editor submit button fires.
# This processes the update before the radio is created, without a state error.
if st.session_state.get('pending_manual_submission') is not None:
    normalized, cash, warnings, simple = st.session_state.pop('pending_manual_submission')
    install_portfolio(normalized, cash, warnings, simple, '직접 입력')
    st.session_state['uploaded_hash'] = ''
    st.session_state['upload_rev'] += 1



with st.sidebar:
    st.markdown('## 💠 HanaV Private')
    st.caption('Portfolio Intelligence | V1.0.15')
    if st.button('로그아웃',use_container_width=True):
        # Completely clear current browser session's customer-position data.
        st.session_state.clear()
        st.rerun()
    st.divider()
    st.markdown('#### 1. 보유현황 입력')
    st.success('✍ 엑셀 없이 입력: 오른쪽 화면의 **직접 입력** 탭을 이용하세요.')
    st.caption('입력 도중에도 ✍ 직접 입력 탭에서 엑셀로 저장하고 다음 접속 때 복구할 수 있습니다.')
    st.download_button('📥 보유종목 엑셀 양식 다운로드',data=portfolio_template_bytes(),file_name='HanaV_Private_Portfolio_간편양식.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',use_container_width=True)
    st.caption('간편 양식 6개 항목만 입력: 고객구분 · 종목코드 · 종목명 · 보유수량 · 평균단가 · 현금. 기존 상세양식도 지원합니다.')
    uploaded=st.file_uploader('고객별 보유종목 파일',type=['xlsx','csv'],key=f"input_upload_{st.session_state['upload_rev']}",help='고객구분(별칭), 종목코드, 종목명, 보유수량, 평균단가, 현금만 입력해도 됩니다. 계좌번호와 실명은 제외하세요.')
    if uploaded is not None:
        b=uploaded.getvalue();fingerprint=hashlib.sha256(b).hexdigest()
        if fingerprint!=st.session_state['uploaded_hash']:
            try:
                if uploaded.name.lower().endswith('.csv'):
                    try:d=pd.read_csv(BytesIO(b),dtype={'종목코드':str})
                    except UnicodeDecodeError:d=pd.read_csv(BytesIO(b),encoding='cp949',dtype={'종목코드':str})
                else:d=pd.read_excel(BytesIO(b),sheet_name=0,dtype={'종목코드':str})
                normalized,cash_by_customer,upload_warnings,is_simple=parse_portfolio_file(d)
                install_portfolio(normalized, cash_by_customer, upload_warnings, is_simple, '파일 업로드')
                st.session_state['uploaded_hash']=fingerprint
                st.session_state['manual_entry_rows']=current_to_manual_rows(normalized, cash_by_customer)
                st.session_state['manual_editor_rev']+=1
                st.success(st.session_state['apply_flash'])
            except Exception as e:st.error(f'파일을 읽을 수 없습니다: {e}')
    st.caption('저장한 엑셀은 업로드 가능하며, 입력 중인 미완성 표는 직접 입력 탭의 복구 기능을 이용하세요. 서버에 자동 저장하지 않습니다.')
    if st.button('가상 65종목 데모 다시 열기',use_container_width=True):
        st.session_state['portfolio_raw']=load_demo()
        st.session_state['cash_by_customer']={}
        st.session_state['simple_mode']=False
        st.session_state['auto_quote_pending']=False
        st.session_state['auto_fx_pending']=False
        st.session_state['price_mode_choice']='입력 기준가'
        st.session_state['is_demo']=True
        st.session_state['manual_entry_rows']=blank_manual_rows()
        st.session_state['manual_editor_rev']+=1
        st.session_state['uploaded_hash']=''
        st.session_state['upload_rev']+=1
        st.session_state['editor_rev']+=1
        st.session_state['quote_errors']=[]
        st.session_state['quotes']={}
        st.session_state['news_map']={}
        st.session_state['deep_research']={}
        st.session_state['attempted_research']=[]
        st.session_state['report_quotes']={}
        st.session_state.pop('selected_research_index',None)
        st.session_state['fx_val']=1350.0
        st.session_state['fx_source']='가상 환율'
        st.session_state.pop('pdf_bytes',None)
        st.rerun()
    st.divider()
    st.markdown('#### 2. 시세 / 환율')
    price_mode=st.radio('가격 기준',['입력 기준가','Yahoo 공개 종가 조회'],horizontal=False,
                        key='price_mode_choice',help='간편 양식에는 기준가가 없으므로 공개 종가 조회를 사용합니다. 공개 시세는 지연되거나 미제공될 수 있습니다.')
    if st.session_state.pop('auto_quote_pending',False) and price_mode=='Yahoo 공개 종가 조회':
        symbols=tuple(sorted(set(normalize_portfolio(st.session_state['portfolio_raw'])[0]['티커'])))
        if len(symbols)<=60:
            try:
                with st.spinner(f'간편양식 {len(symbols)}개 종목의 공개 종가를 자동 조회 중입니다'):
                    quotes,errors=cached_prices(symbols)
                st.session_state['quotes']=quotes
                st.session_state['quote_errors']=errors
                st.session_state['price_fetch_at']=datetime.now().strftime('%Y-%m-%d %H:%M')
                if errors:st.warning(f'{len(errors)}개 종목의 시세가 조회되지 않았습니다. 해당 평가액은 미확인 상태로 남깁니다.')
            except Exception as e:
                st.warning(f'자동 시세 조회 실패: {e}. 아래 일괄 시세 갱신 버튼으로 다시 시도할 수 있습니다.')
        else:
            st.info(f'종목이 {len(symbols)}개라 자동 시세 조회를 생략했습니다. 아래 일괄 시세 갱신 버튼을 눌러 주세요.')
    if price_mode=='Yahoo 공개 종가 조회':
        if st.button('📊 60종목 일괄 시세 갱신',type='primary',use_container_width=True):
            try:
                raw,_=normalize_portfolio(st.session_state['portfolio_raw'])
                symbol_tuple=tuple(sorted(set(raw['티커'])))
                with st.spinner(f'공개 티커 {len(symbol_tuple)}개 조회 중 · 고객정보는 전송하지 않습니다'):
                    with st.status('시세 다운로드',expanded=False) as status:
                        q,errors=cached_prices(symbol_tuple)
                        status.update(label=f'{len(q)}개 시세 수집 완료',state='complete')
                st.session_state['quotes']=q
                st.session_state['quote_errors']=errors
                st.session_state['price_fetch_at']=datetime.now().strftime('%Y-%m-%d %H:%M')
            except Exception as e:st.error(f'시세 조회 중 오류: {e}')
        if st.session_state['quotes']:
            st.caption(f"수집 {len(st.session_state['quotes'])}개 · {st.session_state.get('price_fetch_at','')}")
        if st.session_state.get('quote_errors'):
            with st.expander(f"조회 실패 {len(st.session_state['quote_errors'])}건"):
                st.write(st.session_state['quote_errors'][:100])
    if st.session_state.pop('auto_fx_pending',False):
        try:
            rate,src=cached_fx()
            if rate:
                st.session_state['fx_val']=float(rate)
                st.session_state['fx_source']=src
                st.caption(f'해외주식 원화 환산을 위해 USD/KRW 지연 환율 {rate:,.2f}원을 자동 조회했습니다.')
            else:
                st.warning(f'환율 자동 조회에 실패했습니다. 아래 버튼으로 재시도 또는 환율을 직접 입력하세요: {src}')
        except Exception as e:
            st.warning(f'환율 자동 조회 오류: {e}. 환율을 직접 입력해 주세요.')
    if st.button('USD/KRW 환율 조회',use_container_width=True):
        rate,src=cached_fx()
        if rate:
            st.session_state['fx_val']=float(rate)
            st.session_state['fx_source']=src
            st.success(f'1 USD = {rate:,.2f} KRW')
        else:st.warning(src)
    # Do not assume unknown foreign exchange rate or present placeholder as real.
    fx_input=st.number_input('1달러당 원화 환산율',min_value=0.0,max_value=100000.0,value=float(st.session_state['fx_val']),step=1.0,format='%.2f',help='0이면 USD 포지션의 원화 평가액 계산 불가. 데모 1,350원은 가상 수치입니다.')
    fx_source=st.session_state['fx_source'] if fx_input==st.session_state['fx_val'] else '사용자 수동 입력'
    st.session_state['fx_val']=fx_input
    st.caption('기준가를 입력하지 않아도 공개 종가 조회가 가능합니다. 해외주식은 USD, 국내주식은 KRW로 환산합니다. 시세 미제공 종목은 임의 평가하지 않습니다.')
    st.divider()
    st.warning('**고객정보 보안**: 실명·계좌번호 입력 금지. 사내 고객데이터 적용 전 보안·전산·준법 승인 필수.',icon='🔒')

st.markdown('<div class="hero"><div class="kicker">PREMIER PORTFOLIO INTELLIGENCE</div><h1>HanaV Private Portfolio</h1><p>60+ holdings · Family views · Risk monitor · Research briefing · PDF report</p></div>',unsafe_allow_html=True)
if st.session_state['is_demo']:
    if not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'samples', 'demo_65.csv')):
        st.info('GitHub에 samples/demo_65.csv 파일이 없어 내장된 가상 65종목 데모를 사용합니다. 프로그램 실행에는 문제가 없습니다.')
    st.warning('현재 **가상 65종목 시연 데이터**입니다. 가격·수량·환율·평가금액은 실제 고객 보유자산이나 실시간 가격이 아닙니다.',icon='🧪')
else:
    st.info('실제 계좌정보는 입력하지 마세요. 고객별칭만 사용하며 본 버전은 내부 테스트용입니다.',icon='🔒')

try:
    normalized,warnings=normalize_portfolio(st.session_state['portfolio_raw'])
except ValueError as e:
    st.error(str(e));st.stop()

names=['전체 포트폴리오']+sorted(set(normalized['고객구분'].unique().tolist()) | set(st.session_state.get('cash_by_customer',{})))
left,right=st.columns([1.6,4])
with left:scope=st.selectbox('분석 범위',names)
with right:
    st.caption(f"분석대상 {len(normalized)}개 포지션 · 고객구분 {normalized['고객구분'].nunique()}개 · 자동저장 없음")
sub=normalized if scope==names[0] else normalized[normalized['고객구분']==scope].copy()
use_quotes=st.session_state['quotes'] if price_mode=='Yahoo 공개 종가 조회' else {}
fx=fx_input if fx_input>0 else None
positions=enrich_positions(sub,use_quotes,fx)
# Include customer KRW cash in allocation/total only; do not send it to market data providers.
cash_scope=st.session_state.get('cash_by_customer',{})
if scope!='전체 포트폴리오':cash_scope={scope:cash_scope.get(scope,0.0)}
asset_positions=add_customer_cash(positions,cash_scope,fx)
# Re-weight equity positions against equity+cash (not equities alone).
if len(asset_positions)>len(positions):
    positions=asset_positions.iloc[:len(positions)].copy()
summary=portfolio_summary(asset_positions)
warns=warnings[:] + st.session_state.get('upload_warnings',[])
if price_mode=='입력 기준가':warns.append('가격은 입력 기준가입니다. 입력일이 확인되지 않으므로 실시간 가격으로 간주하면 안 됩니다.')
for w in warns[:5]:st.caption('ⓘ '+w)
if price_mode=='Yahoo 공개 종가 조회' and st.session_state.get('quotes'):
    backup=positions['가격출처'].eq('파일 입력 기준가').sum()
    if backup:
        st.warning(f'{backup}개 종목은 온라인 시세가 없어 엑셀 입력 기준가를 사용 중입니다. 날짜가 섞인 평가액이므로 재확인해 주세요.')


tab_manual,tab_dashboard,tab_holdings,tab_research,tab_news,tab_report=st.tabs(
    ['✍ 직접 입력','📊 대시보드','📋 보유종목 관리','🔎 종목별 심층분석','📰 뉴스 모니터링','📑 보고서'])

with tab_manual:
    st.subheader('보유종목·현금 직접 입력')
    st.markdown('**엑셀 작성·전송 없이 이 화면에서 바로 입력할 수 있습니다.** 종목을 추가하려면 표 맨 아래 `+` 행에 입력하거나 행 추가 메뉴를 이용하세요.')
    st.caption('필수: 고객구분(별칭), 종목코드, 보유수량. 종목명·평균단가·현금도 입력할 수 있습니다. 현금은 같은 고객당 한 번만 입력하며 원화 기준입니다.')
    st.caption('국내 코스닥은 종목코드 끝에 `.KQ`를 붙이세요(예: `247540.KQ`). 코스피 `005930`, 미국 `AAPL`처럼 입력합니다. 고객 실명·계좌번호는 입력하지 마세요.')
    c_load,c_clear=st.columns(2)
    with c_load:
        if st.button('↩ 현재 보유현황을 입력표에 불러오기', use_container_width=True):
            st.session_state['manual_entry_rows']=current_to_manual_rows(
                st.session_state['portfolio_raw'], st.session_state.get('cash_by_customer',{}))
            st.session_state['manual_editor_rev']+=1
            st.rerun()
    with c_clear:
        if st.button('🧹 입력표만 비우기',use_container_width=True):
            st.session_state['manual_entry_rows']=blank_manual_rows()
            st.session_state['manual_editor_rev']+=1
            st.rerun()
    # Intentionally NOT in st.form: st.data_editor updates on each edit/rerun,
    # so Excel backup can include the *latest draft* without first applying it.
    direct_rows=st.data_editor(
        st.session_state['manual_entry_rows'],
        num_rows='dynamic', hide_index=True, use_container_width=True, height=345,
        column_order=MANUAL_COLUMNS,
        column_config={
            '고객구분':st.column_config.TextColumn('고객구분',help='실명이 아닌 고객 별칭을 입력하세요.'),
            '종목코드':st.column_config.TextColumn('종목코드',help='앞자리 0 유지. 코스닥은 .KQ를 붙입니다.'),
            '종목명':st.column_config.TextColumn('종목명'),
            '보유수량':st.column_config.NumberColumn('보유수량',min_value=0.0,step=1.0),
            '평균단가':st.column_config.NumberColumn('평균단가',min_value=0.0,step=1.0),
            '현금':st.column_config.NumberColumn('현금(원)',min_value=0.0,step=100000.0),
        }, key=f"direct_editor_{st.session_state['manual_editor_rev']}")
    st.caption('💡 종목을 입력한 뒤 **엑셀로 저장**하면 분석 반영 전의 미완성 행까지 그대로 보관할 수 있습니다.')
    valid_count=sum(any(not pd.isna(v) and str(v).strip()!='' for v in row)
                    for row in direct_rows[MANUAL_COLUMNS].itertuples(index=False, name=None))
    c_save,c_apply=st.columns([1, 1])
    with c_save:
        st.download_button(
            '💾 현재 입력표 엑셀 저장',
            data=manual_rows_excel_bytes(direct_rows),
            file_name=f'HanaV_보유현황_입력저장_{datetime.now():%Y%m%d_%H%M}.xlsx',
            mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            disabled=valid_count==0, use_container_width=True,
            help='브라우저의 다운로드 폴더에 저장합니다. 포트폴리오 반영 여부와 무관하게 저장됩니다.')
    with c_apply:
        submitted=st.button('✅ 입력 내용으로 포트폴리오 반영',type='primary',use_container_width=True)
    if submitted:
        try:
            parsed=parse_manual_rows(direct_rows)
            st.session_state['manual_entry_rows']=direct_rows[MANUAL_COLUMNS].copy()
            st.session_state['manual_editor_rev']+=1
            st.session_state['pending_manual_submission']=parsed
            st.rerun()
        except Exception as e:
            st.error(f'직접 입력을 확인해 주세요: {e}')

    with st.expander('📂 저장한 엑셀을 불러와 이어서 입력하기', expanded=False):
        draft_uploaded=st.file_uploader('이전에 저장한 6열 엑셀 파일',
            type=['xlsx'], key=f"draft_restore_{st.session_state['draft_restore_rev']}",
            help='현재 입력표를 복구된 내용으로 교체합니다. 미완성 행도 불러옵니다.')
        if draft_uploaded is not None:
            st.warning('불러오기를 실행하면 현재 입력표가 교체됩니다. 기존 내용이 필요하면 먼저 엑셀로 저장하세요.')
            if st.button('↩ 저장한 입력표 불러오기', use_container_width=True):
                try:
                    restored=manual_draft_from_excel(draft_uploaded.getvalue())
                    st.session_state['manual_entry_rows']=restored
                    st.session_state['manual_editor_rev']+=1
                    st.session_state['draft_restore_rev']+=1
                    st.session_state['draft_restore_flash']=f'엑셀에서 {len(restored)}개 행을 불러왔습니다. 이어서 수정하거나 포트폴리오에 반영하세요.'
                    st.rerun()
                except Exception as e:
                    st.error(f'입력표 복구 오류: {e}')
    if st.session_state.pop('draft_restore_flash', None):
        st.success('저장한 엑셀 입력표를 복구했습니다. 이어서 수정할 수 있습니다.')
    if st.session_state.get('apply_flash') and st.session_state.get('portfolio_source')=='직접 입력':
        st.success(st.session_state['apply_flash'])
    st.info('저장 버튼은 **엑셀 파일을 PC로 다운로드**합니다. 서버에 자동 영구저장되지는 않으므로 로그아웃 전 저장 여부를 확인하세요. 고객 실명·계좌번호 등 식별정보를 입력하지 말고 사내 승인 정책을 준수하세요.')


# ---- Dashboard charts: readable even with many holdings and a narrow sidebar. ----
def build_top_holdings_chart(positions: pd.DataFrame):
    """15 positions, distinct labels, explicit left margin, and readable KRW 100M units."""
    data = positions.dropna(subset=['평가액(원)']).copy()
    data = data[data['평가액(원)'] > 0].nlargest(15, '평가액(원)').reset_index(drop=True)
    if data.empty:
        return None

    # Multiple family members can own the same security; show which account.
    duplicate = data['종목명'].fillna('').astype(str).duplicated(keep=False)
    labels = []
    for i, r in data.iterrows():
        name = str(r.get('종목명') or r.get('종목코드') or '미확인')
        name = name[:12] + ('…' if len(name) > 12 else '')
        if duplicate.iloc[i]:
            label = f"{str(r.get('고객구분',''))[:4]} · {name}"
        else:
            label = name
        labels.append(label)

    amounts = (data['평가액(원)'].astype(float) / 1e8).tolist()
    texts = [f'{v:,.2f}억' for v in amounts]
    positions_y = list(range(len(data)))
    fig = go.Figure(go.Bar(
        x=amounts, y=positions_y, orientation='h',
        marker=dict(color='#24B993', line=dict(color='#56DBB1', width=.4)),
        text=texts, textposition='outside', cliponaxis=False,
        textfont=dict(size=11, color='#F7FFF9'),
        customdata=[[str(r.get('고객구분','')), str(r.get('종목코드',''))] for _,r in data.iterrows()],
        hovertemplate='%{customdata[0]} · %{customdata[1]}<br>평가액: %{x:,.2f}억 원<extra></extra>',
    ))
    fig.update_layout(
        height=510, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(family='Arial, Malgun Gothic, sans-serif', color='#F4FFF8', size=12),
        margin=dict(l=135, r=64, t=12, b=48), showlegend=False,
        bargap=.30,
    )
    fig.update_yaxes(
        tickmode='array', tickvals=positions_y, ticktext=labels,
        tickfont=dict(color='#F4FFF8',size=11), showticklabels=True,
        showgrid=False, showline=False, zeroline=False,
        autorange='reversed', fixedrange=True,
    )
    fig.update_xaxes(
        title='평가액 (억원)', title_font=dict(color='#DCF6E9',size=11),
        tickfont=dict(color='#DCF6E9',size=11),
        gridcolor='rgba(209,245,224,0.14)', showgrid=True,
        zeroline=False, showline=False, fixedrange=True,
        range=[0, max(amounts)*1.31],
    )
    return fig


def build_sector_chart(positions: pd.DataFrame):
    """Aggregate less significant sectors to avoid a 20-item unreadable legend."""
    data = positions.dropna(subset=['평가액(원)']).copy()
    data = data[data['평가액(원)'] > 0]
    if data.empty:
        return None, 0
    data['업종'] = data['업종'].fillna('').astype(str).str.strip().replace('', '미분류')
    sectors = data.groupby('업종', dropna=False)['평가액(원)'].sum().sort_values(ascending=False)
    total = float(sectors.sum())
    if total <= 0: return None, 0
    n_original = len(sectors)
    if n_original > 9:
        top = sectors.iloc[:8].copy()
        top.loc['기타 업종'] = float(sectors.iloc[8:].sum())
        sectors = top
    names = [s[:8]+'…' if len(s)>8 else s for s in sectors.index.astype(str)]
    shares = (sectors / total * 100.0).tolist()
    fig = go.Figure(go.Bar(
        x=shares, y=list(range(len(shares))), orientation='h',
        marker=dict(color=['#25BC93' if i==0 else '#5BC8B3' if i%2 else '#3BAA97' for i in range(len(shares))],
                    line=dict(color='#91E8CE', width=.35)),
        text=[f'{x:.1f}%' for x in shares], textposition='outside', cliponaxis=False,
        textfont=dict(color='#F7FFF9',size=11),
        customdata=(sectors/1e8).tolist(),
        hovertemplate='자산 비중 %{x:.2f}%<br>평가액 %{customdata:,.2f}억 원<extra></extra>',
    ))
    fig.update_layout(
        height=510, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(family='Arial, Malgun Gothic, sans-serif',color='#F4FFF8',size=12),
        margin=dict(l=92,r=60,t=12,b=48),showlegend=False,bargap=.33,
    )
    fig.update_yaxes(
        tickmode='array',tickvals=list(range(len(shares))),ticktext=names,
        tickfont=dict(color='#F4FFF8',size=11),showticklabels=True,
        showgrid=False,showline=False,zeroline=False,autorange='reversed',fixedrange=True,
    )
    fig.update_xaxes(
        title='평가액 비중 (%)',title_font=dict(color='#DCF6E9',size=11),
        tickfont=dict(color='#DCF6E9',size=11),gridcolor='rgba(209,245,224,0.14)',
        showgrid=True,zeroline=False,showline=False,fixedrange=True,
        range=[0,max(shares)*1.38],
    )
    return fig,n_original

with tab_dashboard:
    c1,c2,c3,c4=st.columns(4)
    c1.metric('보유 포지션',f"{summary['positions']:,}개")
    value_string=(f"{summary['value_krw']/1e8:,.1f}억" if pd.notna(summary['value_krw']) else '평가 불가')
    c2.metric('평가 가능한 자산 합계 (현금 포함)',value_string,help='고객별 입력 현금은 원화로 포함하며, 주식 시세 미조회 시 총액은 불완전합니다.')
    if pd.notna(summary['value_krw']):
        st.caption(f"조회 기준 평가액: **{summary['value_krw']:,.0f}원 ({summary['value_krw']/1e8:,.2f}억 원)** · 가격/환율 미반영 {summary['unconverted']}개")
    if cash_scope:
        st.caption(f"고객별 원화 현금 합계: **{sum(cash_scope.values()):,.0f}원** · 각 고객당 1회만 계산 · 외화현금 제외")
    c3.metric('가격/환율 미반영',f"{summary['unconverted']}개")
    c4.metric('최대 보유비중',f"{summary['largest_weight']:.1f}%" if pd.notna(summary['largest_weight']) else '—')
    if not summary['complete_total']:st.error('**불완전한 평가금액**: 미조회 포지션이 있으므로 위 합계와 비중은 일부 자산 기준입니다.')
    # Separate readable bars replace the clipped names and overfull pie legend.
    g1,g2=st.columns([1.25,1], gap='large')
    with g1:
        st.subheader('평가액 상위 15개')
        fig=build_top_holdings_chart(asset_positions)
        if fig is None:
            st.info('평가액을 표시할 수 있는 기준가가 없습니다.')
        else:
            st.plotly_chart(fig,use_container_width=True,theme=None,config={'displayModeBar':False})
    with g2:
        st.subheader('업종별 자산 비중')
        fig,n_sectors=build_sector_chart(asset_positions)
        if fig is None:
            st.info('업종분포 계산에 필요한 평가액이 없습니다.')
        else:
            st.plotly_chart(fig,use_container_width=True,theme=None,config={'displayModeBar':False})
            if n_sectors>9:
                st.caption(f'전체 {n_sectors}개 업종 중 상위 8개와 기타 업종 합계를 표시했습니다.')
    st.subheader('VVIP 모니터링 포인트')
    for msg in risk_findings(asset_positions):st.markdown('• '+msg)
    st.caption('업종 분류는 입력된 데이터 기준이며 ETF 기초자산 중복은 V1에서 미계산합니다. 투자권고·매매신호가 아닙니다.')

if positions.empty:
    st.info('선택한 고객은 현금만 입력되어 종목별 분석 및 뉴스 조회를 생략합니다. 고객 전체 범위에서 주식·현금 통합현황을 확인할 수 있습니다.')
    st.stop()

with tab_holdings:
    st.subheader('보유종목 입력 · 수정')
    st.download_button('⬇ 엑셀 입력양식 다운로드',data=portfolio_template_bytes(),file_name='HanaV_Private_Portfolio_간편양식.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    st.caption('아래 표는 원본 전체 고객군의 데이터입니다. 편집 후 자동으로 재분석됩니다. 계좌번호와 실명은 넣지 마세요.')
    with st.expander('엑셀 양식 안내 / 입력예시',expanded=False):
        st.write('간편 업로드: 고객구분 / 종목코드 / 종목명 / 보유수량 / 평균단가 / 현금. 현금은 고객별 원화 잔고이며 같은 금액을 반복 입력해도 한 번만 합산합니다. 다른 금액이 섞이면 오류로 알려드립니다.')
        st.write('기존 상세양식도 지원합니다. KOSDAQ 숫자 종목은 시장=KOSDAQ을 추가하면 정확한 시장 티커로 조회합니다.')
        st.write('코스닥 숫자 종목은 시장=KOSDAQ을 꼭 기재해 주세요. 005930처럼 앞에 0이 있는 코드도 유지됩니다.')
    edited=st.data_editor(st.session_state['portfolio_raw'],num_rows='dynamic',hide_index=True,use_container_width=True,height=430,
                          column_config={'고객구분':st.column_config.TextColumn('고객 별칭'),'종목코드':st.column_config.TextColumn('종목코드'),'보유수량':st.column_config.NumberColumn('보유수량',min_value=0.0),'매입단가':st.column_config.NumberColumn('매입단가'),'기준가':st.column_config.NumberColumn('기준가(입력값)'),'목표가':st.column_config.NumberColumn('입력 목표가')},key=f"editor_{st.session_state['editor_rev']}")
    if st.button('✳ 수정 내용 반영',type='primary'):
        try:
            nd,_=normalize_portfolio(edited)
            st.session_state['portfolio_raw']=nd[COLUMNS].copy()
            st.session_state['editor_rev']+=1
            st.session_state['news_map']={}
            st.session_state.pop('pdf_bytes',None)
            st.success('변경사항을 현재 브라우저 세션에 반영했습니다.')
            st.rerun()
        except Exception as e:st.error(f'입력 오류: {e}')
    st.divider()
    if cash_scope:
        st.subheader('고객별 현금잔고 (원화)')
        st.dataframe(pd.DataFrame([{'고객구분':k,'현금(원)':v} for k,v in cash_scope.items()]),use_container_width=True,hide_index=True)
        st.caption('현금은 별도 보유종목이 아니라 자산배분 및 전체 평가액에 포함되며, 수익률·기업분석 대상에서는 제외됩니다.')
    st.subheader('현재 분석 결과 전체표')
    display=['고객구분','종목코드','종목명','통화','보유수량','매입단가','현재가','가격기준일','평가액(원)','평가액비중(%)','수익률(%)','모니터링']
    st.dataframe(positions[display],use_container_width=True,height=470,hide_index=True,
                 column_config={'평가액(원)':st.column_config.NumberColumn(format='%,.0f'),'평가액비중(%)':st.column_config.NumberColumn(format='%.2f%%'),'수익률(%)':st.column_config.NumberColumn(format='%+.2f%%')})
    st.download_button('⬇ 분석 결과 CSV 다운로드',data=to_csv_bytes(asset_positions.drop(columns=['환산율'])),file_name=f'hanav_portfolio_{datetime.now():%Y%m%d}.csv',mime='text/csv')

with tab_research:
    st.subheader('🔎 종목별 심층분석 · 실제 시장데이터 연동')
    st.caption('종목을 고른 뒤 분석 버튼을 누르면 공개 데이터로 종가·기술지표·주요 재무·분기 실적·EPS 추정·목표가 집계·뉴스를 확인합니다. 종목별 제공 범위는 다릅니다.')
    pos_options=[f"{i} | {r['고객구분']} · {str(r['종목명']) or r['종목코드']} ({r['종목코드']})" for i,r in positions.iterrows()]
    picked=st.selectbox('분석할 종목 선택',list(range(len(pos_options))),format_func=lambda i:pos_options[i],key='selected_research_index')
    row=positions.iloc[picked]
    symbol=str(row['티커'])
    is_etf='ETF' in str(row.get('종목유형','')).upper()
    col_refresh,col_news=st.columns([2,1])
    with col_refresh:
        if st.button('📡 종합분석 실행: 시세 · 실적 · 목표가 · 뉴스',type='primary',use_container_width=True):
            with st.spinner(f'{symbol} 공개 시장데이터 조회 중 — 종목코드 외 고객정보는 전송하지 않습니다'):
                result=fetch_selected_research(symbol,is_etf)
            st.session_state['deep_research'][symbol]=result
            st.session_state.pop('pdf_bytes',None)
            articles,msg=cached_news(symbol,str(row['종목명']))
            st.session_state['news_map'][symbol]=articles
            st.session_state['news_msg']=msg
            if result.get('quote'):
                st.session_state['quotes'][symbol]=result['quote']
            st.session_state['research_refreshed']=symbol
            st.rerun()
    with col_news:
        if st.button('📰 최신 뉴스 조회',use_container_width=True,key='deep_news'):
            items,msg=cached_news(symbol,str(row['종목명']))
            st.session_state['news_map'][symbol]=items
            st.session_state['news_msg']=msg
            st.session_state.pop('pdf_bytes',None)
            st.rerun()
    research=st.session_state.get('deep_research',{}).get(symbol,{})
    quote=research.get('quote') or st.session_state.get('quotes',{}).get(symbol,{})
    info=research.get('info',{}) or {}
    price=_numeric(quote.get('price')) if quote else None
    if research:
        st.success(f"{symbol} 시장데이터 요청 시각: {research.get('asof','')} · 공개 데이터(지연/누락 가능)")
        if research.get('errors'):
            with st.expander(f"일부 자료 조회 실패 · {len(research['errors'])}건"):
                for message in research['errors']:st.write('• '+str(message))
    else:
        st.info('아직 시장데이터를 조회하지 않았습니다. 위의 **시세 · 실적 · 목표가 조회** 버튼을 눌러주세요. 파일 입력 기준가와 실시간 시세는 구분됩니다.')
    metrics=st.columns(5)
    metrics[0].metric(f'최근 공개 종가 ({row["통화"]})',_compact_price(price,row['통화']),help='정확한 소수점 종가는 아래 기준일/출처에 함께 표시됩니다.')
    metrics[1].metric('1개월 등락률',_val(quote.get('ret_1m'),'%' ) if quote else '미조회')
    metrics[2].metric('3개월 등락률',_val(quote.get('ret_3m'),'%' ) if quote else '미조회')
    metrics[3].metric('보유 수익률',f"{row['수익률(%)']:+.2f}%" if pd.notna(row['수익률(%)']) else '매입가 필요')
    metrics[4].metric('평가액 비중',f"{row['평가액비중(%)']:.2f}%" if pd.notna(row['평가액비중(%)']) else '평가 불가')
    st.caption(f"정확한 최근 종가: **{price:,.2f} {row['통화']}** · 기준일 {quote.get('date','미조회')}" if price is not None else '공개 종가 미조회')
    st.caption(f"출처: {quote.get('source','없음') if quote else '없음'} · 수익률/비중은 현재 선택된 평가 기준으로 계산됩니다.")
    ta,fun,ta_target,news_tab,monitor=st.tabs(['📈 주가 · 기술지표','📊 재무 · 실적','🎯 목표가 · 추정','📰 최근 뉴스','🧭 관리 전략'])
    with ta:
        d1,d2=st.columns([1.7,1])
        with d1:
            st.markdown('#### 최근 1년 가격 추이')
            hist=quote.get('history',pd.Series(dtype=float)) if quote else pd.Series(dtype=float)
            if isinstance(hist,pd.Series) and not hist.empty:
                fig=go.Figure()
                fig.add_trace(go.Scatter(x=hist.index,y=hist.values,mode='lines',name='종가',line=dict(color='#55F0BC',width=2.5)))
                for days,name,color in [(20,'MA20','#EEC978'),(60,'MA60','#92BAFA'),(120,'MA120','#D1A9EA')]:
                    if len(hist)>=days:fig.add_trace(go.Scatter(x=hist.index,y=hist.rolling(days).mean(),name=name,line=dict(color=color,width=1.3)))
                fig.update_layout(height=440,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font_color='#F1FFF7',margin=dict(l=0,r=0,t=0,b=0),legend=dict(orientation='h',y=1.08))
                st.plotly_chart(fig,use_container_width=True,theme=None)
            else:st.info('가격 차트는 해당 종목의 시장데이터 조회가 성공하면 표시됩니다.')
        with d2:
            st.markdown('#### 기술지표 요약')
            indicators=[('20일 이동평균',quote.get('ma20')),('60일 이동평균',quote.get('ma60')),('120일 이동평균',quote.get('ma120')),('52주 종가 최고',quote.get('high52')),('RSI(14)',quote.get('rsi14'))] if quote else []
            if indicators:
                st.dataframe(pd.DataFrame([{'지표':k,'값':_val(v)} for k,v in indicators]),hide_index=True,use_container_width=True)
                if price is not None and _numeric(quote.get('high52')):
                    st.metric('52주 종가 고점 대비',f"{(price/quote['high52']-1)*100:+.1f}%")
            else:st.info('시세를 조회하면 이동평균·RSI가 표시됩니다.')
            st.caption('RSI는 14일 종가 변화의 지수평활 평균으로 계산합니다. 52주 고점은 일중 최고가가 아닌 조회 기간 종가의 최고치입니다.')
    with fun:
        st.markdown('#### 기업 재무 핵심지표')
        if not research:st.info('상단의 종합분석 실행 버튼을 눌러주세요.')
        else:
            if not _has_financial_data(research):
                st.warning('기업 실적/재무 수치를 확보하지 못했습니다. 시세 조회 성공과 재무 데이터 제공은 별개입니다.')
            with st.expander('재무 데이터 조회 진단 · 어떤 단계가 실패했나요?',expanded=not _has_financial_data(research)):
                st.json(research.get('source_status',{}))
                for reason in research.get('errors',[]): st.caption('• '+str(reason))
            info_cols=st.columns(4)
            items=[('시가총액',_amount(info.get('marketCap'),research.get('price_currency',''))),
                   ('PER (TTM)',_val(info.get('trailingPE'))),('예상 PER',_val(info.get('forwardPE'))),('PBR',_val(info.get('priceToBook'))),
                   ('ROE',_val(info.get('returnOnEquity'),'%',True)),('영업이익률',_val(info.get('operatingMargins'),'%',True)),
                   ('매출 성장률',_val(info.get('revenueGrowth'),'%',True)),('순이익 성장률',_val(info.get('earningsGrowth'),'%',True))]
            for i,(label,value) in enumerate(items):info_cols[i%4].metric(label,value)
            st.caption(f"Yahoo Finance 기본 통계 · {info.get('longName') or info.get('shortName') or symbol} · 주가통화 {research.get('price_currency','확인 불가')} / 실적통화 {research.get('financial_currency','확인 불가')}")
            st.markdown('#### 최근 분기 손익계산서')
            rows=research.get('quarters',[])
            if rows:
                financial_ccy=research.get('financial_currency','')
                frame=pd.DataFrame([{'분기':q['기간'], '매출액':_amount(q.get('매출액'),financial_ccy), '영업이익':_amount(q.get('영업이익'),financial_ccy), '순이익':_amount(q.get('당기순이익'),financial_ccy), '희석EPS':_val(q.get('희석EPS'))} for q in rows])
                st.dataframe(frame,hide_index=True,use_container_width=True)
            else:st.info('제공처에서 분기 실적 데이터를 제공하지 않거나 요청이 제한됐습니다. 특히 국내 종목·ETF는 미제공될 수 있습니다.')
    with ta_target:
        st.markdown('#### 애널리스트 목표가 집계')
        if pd.notna(row['목표가']) and row['목표가']>0:
            st.info(f"엑셀 입력 목표가: **{row['목표가']:,.2f} {row['통화']}** · 출처: {row['목표가출처'] or '미입력'} · 발표일: {row['목표가기준일'] or '미입력'}")
        targets=research.get('analyst_targets',{})
        if is_etf:
            st.info('ETF는 개별 기업과 같은 증권사 목표주가 집계를 제공하지 않습니다. 기초자산·지수·괴리율·보수 등을 별도로 평가해야 합니다.')
        elif _numeric(targets.get('mean')) is not None:
            pcols=st.columns(3)
            pcols[0].metric('목표가 평균',_val(targets.get('mean')))
            pcols[1].metric('목표가 최저',_val(targets.get('low')))
            pcols[2].metric('목표가 최고',_val(targets.get('high')))
            currency=research.get('price_currency') or str(row['통화'])
            st.caption(f'출처: Yahoo Finance 애널리스트 집계 · 가격통화 {currency} · 개별 증권사·발표일·집계 시각은 확인되지 않습니다.')
            if price is not None and price>0 and currency==str(row['통화']).upper():
                st.metric('평균 목표가와 공개 종가 차이',f"{(targets['mean']/price-1)*100:+.1f}%")
            else:st.warning('통화 일치/공개 종가 확인이 어려워 목표가 괴리율을 산출하지 않았습니다.')
        elif not pd.notna(row['목표가']):
            st.info('목표가 정보가 조회되지 않았습니다. 임의 목표가를 생성하지 않습니다. 검증된 목표가가 있으면 엑셀에 출처·발표일과 함께 입력하세요.')
        st.markdown('#### EPS 시장 추정치')
        est=research.get('eps_estimates',[])
        if est:
            st.dataframe(pd.DataFrame([{'기간':p['기간'],'평균 EPS':_val(p.get('추정EPS')),'애널리스트 수':_val(p.get('분석기관수')),'추정 EPS 성장률':_val(p.get('EPS 성장률'),'%',True)} for p in est]),hide_index=True,use_container_width=True)
            st.caption('Yahoo Finance 공개 분석가 집계. 제공처의 최신 갱신 시각과 추정 산식을 확인하지 못한 참고자료입니다.')
        else:st.caption('EPS 컨센서스 미제공 또는 조회 실패')
    with news_tab:
        items=st.session_state['news_map'].get(symbol,[])
        if not items:
            st.info('위 **최신 뉴스 조회** 버튼을 누르면 이 종목의 최근 검색 기사와 출처 링크가 표시됩니다.')
        for a in items[:8]:
            st.markdown(f"**[{a['제목']}]({a['원문']})**")
            st.caption(f"{a['언론사']} · {a['발행일']}")
        if symbol in st.session_state['news_map'] and not items:st.warning(st.session_state.get('news_msg','검색 결과 없음 또는 RSS 제한'))
        st.caption('Google News RSS의 종목명·티커 검색결과입니다. 제목과 기업명 일치 여부를 원문으로 검증하세요.')
    with monitor:
        st.markdown('#### 보유 포지션 분석')
        for note in build_individual_note(row):st.markdown('• '+note)
        st.markdown('#### 공개 시장데이터 기반 모니터링')
        if research:
            for note in _evidence_insights(row,research):st.markdown('• '+note)
        else:st.info('공개 시장데이터를 조회하면 가격과 실적에 근거한 관리 포인트가 추가됩니다.')
        st.warning('위 관찰사항은 정량 지표를 설명한 것이며 매수·매도 권유나 목표가 보증이 아닙니다. 투자 판단 및 외부 제공 전 내부 검토가 필요합니다.')

with tab_news:
    st.subheader('보유종목 뉴스 모니터링')
    st.caption('각 종목별로 뉴스를 일괄 조회합니다. 호출 제한이 있는 무료 공개 RSS이므로 일부 누락될 수 있습니다. 고객의 수량·평단가·계좌별칭은 전송하지 않습니다.')
    n_limit=st.slider('일괄 조회할 종목 수 (평가액 상위순)',1,min(100,len(positions)),min(15,len(positions)))
    desired=positions.sort_values('평가액(원)',ascending=False,na_position='last').head(n_limit)
    if st.button('🔄 선택 종목 뉴스 일괄 조회',type='primary'):
        pairs=list(zip(desired['티커'],desired['종목명']))
        with st.spinner('공개 뉴스 RSS 조회 중'):
            news_map,failed=fetch_news_batch(pairs,max_workers=4)
        st.session_state['news_map'].update(news_map)
        st.session_state['news_fetch_at']=datetime.now().strftime('%Y-%m-%d %H:%M')
        st.success(f"뉴스 조회 {len(news_map)}개 종목 · 기사 없음/오류 {len(failed)}개")
    if st.session_state['news_map']:
        options=[t for t in positions['티커'].unique() if t in st.session_state['news_map']]
        for t in options:
            p=positions[positions['티커']==t].iloc[0]
            with st.expander(f"{p['종목명'] or p['종목코드']} ({t}) · {len(st.session_state['news_map'][t])}건"):
                items=st.session_state['news_map'][t]
                if not items:st.write('검색 결과 없음 또는 수집 제한')
                for a in items:
                    st.markdown(f"[{a['제목']}]({a['원문']})")
                    st.caption(f"{a['언론사']} / {a['발행일']}")
    else:st.info('조회 버튼을 누르면 뉴스 목록이 나타납니다.')

with tab_report:
    st.subheader('전담 포트폴리오 정기 점검 보고서')
    st.write('**트리맵 · 전 종목 수익률 히트맵 · 업종/고객별 자산 분포**를 앞부분에 보여주고, 뒤에는 종목별 심층 분석 카드를 첨부합니다. 뉴스·실적은 수집된 항목만 반영합니다.')
    st.caption('기업 전망을 임의 작성하거나 단순 뉴스 제목으로 매매를 권고하지 않습니다. 모든 데이터는 출처·기준시점 확인과 준법 검토가 필요합니다.')
    symbols_for_report=list(dict.fromkeys(positions.sort_values('평가액(원)',ascending=False,na_position='last')['티커'].astype(str).tolist()))
    quotes_for_report={**st.session_state.get('quotes',{}),**st.session_state.get('report_quotes',{})}
    research_for_report=st.session_state.get('deep_research',{})
    news_for_report=st.session_state.get('news_map',{})
    def has_analysis_data(result):
        return _has_financial_data(result)
    covered_prices=sum(_numeric((quotes_for_report.get(t) or {}).get('price')) is not None or
                       _numeric((research_for_report.get(t) or {}).get('quote',{}).get('price')) is not None
                       for t in symbols_for_report)
    covered_deep=sum(has_analysis_data(research_for_report.get(t)) for t in symbols_for_report)
    covered_news=sum(bool(news_for_report.get(t)) for t in symbols_for_report)
    attempted_tickers=set(st.session_state.get('attempted_research',[])) | set(research_for_report)
    attempted_count=len(attempted_tickers.intersection(symbols_for_report))
    failed_fundamentals=[t for t in symbols_for_report if t in attempted_tickers and not has_analysis_data(research_for_report.get(t))]
    not_attempted=len(symbols_for_report)-attempted_count
    metrics=st.columns(3)
    metrics[0].metric('시장 추세 수집',f'{covered_prices}/{len(symbols_for_report)}')
    metrics[1].metric('재무·실적 확보',f'{covered_deep}/{len(symbols_for_report)}',help='시세 조회와 별개. ② 기업분석 실행으로 재무지표, 분기실적 또는 EPS 추정치를 확보한 종목 수')
    metrics[2].metric('최근 뉴스',f'{covered_news}/{len(symbols_for_report)}')
    if attempted_count==0:
        st.info('재무 조회 **미실행**: ① 시세만 수집한 상태입니다. ② 다음 미조회 기업 분석을 눌러 5~10개씩 진행하세요.')
    else:
        st.caption(f'재무·실적 조회 진행: **시도 {attempted_count}/{len(symbols_for_report)}건 · 확보 {covered_deep}건 · 미제공/오류 {len(failed_fundamentals)}건 · 미시도 {not_attempted}건**')
        if failed_fundamentals:
            with st.expander(f'재무 미확보 {len(failed_fundamentals)}종목 조회 진단'):
                for ticker in failed_fundamentals[:65]:
                    result=research_for_report.get(ticker,{}) or {}
                    error_msgs=result.get('errors',[]) or ([str(result.get('error'))] if result.get('error') else [])
                    status=result.get('source_status',{})
                    st.write(f'**{ticker}** · {", ".join(error_msgs[:2]) or "지표 미제공"}')
                    if status:st.caption(' · '.join(f'{k}: {v}' for k,v in status.items()))
    st.markdown('#### 보고서용 자료 수집')
    st.caption('외부에는 공개 티커·종목명만 조회하며 고객별칭·수량·매입단가는 전송하지 않습니다. 시세 조회는 엑셀 기준가 평가액을 자동으로 변경하지 않습니다.')
    cprice,cdeep,cnews=st.columns(3)
    with cprice:
        if st.button('① 전 종목 가격·추세 수집',use_container_width=True):
            with st.spinner(f'{len(symbols_for_report)}종목 시세 이력 다운로드 중'):
                try:
                    fetched,failed=cached_prices(tuple(sorted(symbols_for_report)))
                    st.session_state['report_quotes'].update(fetched)
                    st.session_state.pop('pdf_bytes',None)
                    st.success(f'시세 수집 {len(fetched)}종목 / 실패 {len(failed)}건')
                    if failed:
                        with st.expander('시세 미조회 목록'):
                            for e in failed[:70]:st.write(str(e))
                except Exception as e:st.error(f'시세 조회 실패: {str(e)[:200]}')
    with cdeep:
        analysis_count=st.selectbox('한 번에 조회할 기업 수',[5,10,20],index=1,
            help='최상위 종목부터 이미 조회한 종목을 건너뛰고 다음 미조회 종목으로 이어집니다.')
        retry_empty=st.checkbox('자료 없는 종목 재시도',value=False,
            help='체크하면 과거 요청했지만 재무지표가 확보되지 않은 종목부터 재조회합니다.')
        st.caption('가격 조회와 별도 작업입니다. 무료 공개 데이터 특성상 실적 미제공 또는 호출제한이 발생할 수 있습니다.')
        if st.button('② 다음 미조회 기업 분석',use_container_width=True):
            attempted=set(st.session_state.get('attempted_research',[])) | set(st.session_state.get('deep_research',{}))
            if retry_empty:
                candidates=[t for t in symbols_for_report if not has_analysis_data(st.session_state['deep_research'].get(t))]
            else:
                candidates=[t for t in symbols_for_report if t not in attempted]
            targets=candidates[:analysis_count]
            if not targets:
                st.info('새로 조회할 종목이 없습니다. 재조회가 필요하다면 자료 없는 종목 재시도를 선택하세요.')
            else:
                bar=st.progress(0,text='기업분석 수집 시작')
                successes=0
                retry_token=datetime.now().isoformat(timespec='microseconds') if retry_empty else ''
                for idx,t in enumerate(targets,1):
                    is_etf=bool(positions.loc[positions['티커']==t,'종목유형'].astype(str).str.upper().str.contains('ETF').any())
                    try:
                        result=fetch_selected_research(t,is_etf,include_price=not bool(quotes_for_report.get(t)),cache_buster=retry_token)
                        st.session_state['deep_research'][t]=result
                        if has_analysis_data(result):successes+=1
                    except Exception as e:
                        st.session_state['deep_research'][t]={'error':str(e)[:150],'errors':['자료 수집 실패']}
                    if t not in st.session_state['attempted_research']:
                        st.session_state['attempted_research'].append(t)
                    bar.progress(idx/len(targets),text=f'기업분석 {idx}/{len(targets)}')
                st.session_state.pop('pdf_bytes',None)
                st.success(f'{len(targets)}개 종목 조회 시도 · 유효한 기업 지표 {successes}개 · 누적 조회 시도 {len(st.session_state["attempted_research"])}개')
    with cnews:
        news_count=st.selectbox('한 번에 뉴스 조회할 종목 수',[5,10,20],index=1)
        if st.button('③ 다음 미조회 뉴스 수집',use_container_width=True):
            targets=[t for t in symbols_for_report if t not in st.session_state['news_map']][:news_count]
            if not targets:
                st.info('모든 종목의 뉴스 조회를 한 번 이상 시도했습니다. 새로 조회하려면 뉴스 모니터링 탭을 이용하세요.')
            else:
                pairs=[]
                for t in targets:
                    r=positions.loc[positions['티커']==t].iloc[0]
                    pairs.append((t,str(r.get('종목명') or r.get('종목코드'))))
                with st.spinner(f'{len(pairs)}종목 뉴스 RSS 수집 중'):
                    try:
                        new_items,failed=fetch_news_batch(pairs,max_workers=4)
                        st.session_state['news_map'].update(new_items)
                        st.session_state.pop('pdf_bytes',None)
                        st.success(f'{len(new_items)}개 종목 뉴스 조회 · 기사 확보 {len(new_items)-len(failed)}종목 · 기사 없음/오류 {len(failed)}종목')
                    except Exception as e:st.error(f'뉴스 수집 실패: {str(e)[:160]}')
    st.divider()
    st.caption('**권장 순서:** ① 가격·추세 수집 → ② 다음 미조회 기업분석(필요한 만큼 반복) → ③ 다음 미조회 뉴스(필요한 만큼 반복) → ④ PDF 생성. 데이터가 부족해도 PDF는 생성되지만 전망을 자동으로 만들어 채우지는 않습니다.')
    if covered_deep==0 and covered_news==0:
        st.warning('실적·컨센서스·뉴스 자료가 0건입니다. 지금 PDF를 만들면 평가액과 가격추세 중심의 요약만 나타납니다. ②·③ 자료 수집을 먼저 권장합니다.')
    if st.button('④ 📄 시각형 포트폴리오 PDF 생성',type='primary'):
        try:
            from pdf_report import build_pdf  # 전체 ZIP의 pdf_report.py 사용
            st.session_state['pdf_bytes']=build_pdf(asset_positions,price_mode,fx_source,st.session_state['is_demo'],
                            research_map=st.session_state.get('deep_research',{}),
                            news_map=st.session_state.get('news_map',{}),
                            quote_map={**st.session_state.get('quotes',{}),**st.session_state.get('report_quotes',{})})
            st.session_state['pdf_scope']=scope
            st.success('보고서가 생성됐습니다.')
        except ModuleNotFoundError as e:
            if e.name=='reportlab':
                st.error(r'PDF 생성에 필요한 reportlab 패키지가 설치되지 않았습니다. 앱 폴더에서 .\.venv\Scripts\python.exe -m pip install reportlab 을 실행하세요.')
            else:st.error(f'PDF 모듈 오류: {e}')
        except Exception as e:st.error(f'PDF 생성 오류: {e}\n한글 글꼴이 필요하면 GitHub 저장소 최상위에 packages.txt(fonts-nanum)를 추가하세요.')
    if 'pdf_bytes' in st.session_state:
        st.download_button('⬇ PDF 다운로드',data=st.session_state['pdf_bytes'],file_name=f'hanav_private_{datetime.now():%Y%m%d}.pdf',mime='application/pdf',use_container_width=True)
        st.caption(f"생성된 범위: {st.session_state.get('pdf_scope','')} · 범위/자료 수정 시 PDF를 다시 생성하세요.")
    st.download_button('⬇ 결과 데이터 CSV 다운로드',data=to_csv_bytes(asset_positions),file_name=f'hanav_detail_{datetime.now():%Y%m%d}.csv',mime='text/csv')
    st.divider()
    st.markdown('#### 사내 검토 전 필수 확인')
    st.markdown('• 데이터 제공사의 라이선스 및 대외 재배포 가능 여부  \n• 고객정보 처리 및 전산 보안 승인 여부  \n• 목표가·증권사 의견 인용의 적법성 및 근거 출처  \n• 고객 제공자료의 준법 검토 및 버전 이력 관리')

st.divider()
st.caption('HanaV Private Portfolio V1.0.14 · 기존 HanaV Trading과 별개인 내부 시험용 도구 · 주문/자동매매/개인정보 서버저장 미지원')
