"""Browser-only entry of holdings using the same import rules as file upload.

Does not send customer identifiers, quantities, purchase prices or cash to data feeds,
and does not persist customer records on the server or disk.
"""
from __future__ import annotations

import pandas as pd
from core import COLUMNS
from portfolio_input import parse_portfolio_file

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
