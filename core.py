"""HanaV Private Portfolio -- pure analysis; no network, disk writes or customer storage."""
from __future__ import annotations
from datetime import datetime
import re
import math
import pandas as pd
import numpy as np

COLUMNS = ["고객구분", "종목코드", "종목명", "시장", "통화", "보유수량", "매입단가", "기준가", "업종", "종목유형", "목표가", "목표가출처", "목표가기준일", "비고"]
REQUIRED = ["종목코드", "보유수량"]
ALIASES = {
    "고객": "고객구분", "계좌구분": "고객구분", "고객명": "고객구분", "계좌별칭": "고객구분",
    "코드": "종목코드", "ticker": "종목코드", "symbol": "종목코드", "티커": "종목코드", "단축코드": "종목코드",
    "종목": "종목명", "name": "종목명", "exchange": "시장", "market": "시장",
    "currency": "통화", "수량": "보유수량", "quantity": "보유수량", "shares": "보유수량",
    "평균매입가": "매입단가", "평단가": "매입단가", "매수가": "매입단가", "averagecost": "매입단가",
    "현재가": "기준가", "참고가": "기준가", "referenceprice": "기준가", "price": "기준가",
    "sector": "업종", "유형": "종목유형", "자산유형": "종목유형", "targetprice": "목표가", "목표주가": "목표가",
}
NUMERICS = ["보유수량", "매입단가", "기준가", "목표가"]

def _clean_symbol(value):
    if pd.isna(value): return ""
    s=str(value).strip().upper().replace(" ", "")
    if re.fullmatch(r"\d{1,6}(\.0+)?", s):
        return str(int(float(s))).zfill(6)
    return s

def normalize_portfolio(data: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Normalize input without falsely completing missing prices or costs."""
    if data is None or data.empty: raise ValueError("입력한 보유종목이 없습니다.")
    df=data.copy()
    clean_map={}
    for c in df.columns:
        k=str(c).strip().replace(" ", "").replace("_", "").lower()
        matching=next((t for t in COLUMNS if t.replace(" ", "").lower()==k), None)
        clean_map[c]=matching or ALIASES.get(k, str(c).strip())
    df=df.rename(columns=clean_map)
    if df.columns.duplicated().any(): raise ValueError("중복된 입력 열 이름이 있습니다. 원본 파일을 확인해 주세요.")
    for required in REQUIRED:
        if required not in df: raise ValueError(f"필수 열 누락: {required}")
    for col in COLUMNS:
        if col not in df: df[col]=None
    df=df[COLUMNS].copy()
    df["종목코드"]=df["종목코드"].map(_clean_symbol)
    df=df[df["종목코드"]!=""].copy()
    if df.empty:raise ValueError("유효한 종목코드가 없습니다.")
    for col in NUMERICS:
        df[col]=pd.to_numeric(df[col].astype(str).str.replace(",", "",regex=False).str.replace("원", "",regex=False).str.strip(), errors="coerce")
    if df["보유수량"].isna().any() or (df["보유수량"]<=0).any():
        raise ValueError("보유수량은 모든 종목에서 0보다 큰 숫자여야 합니다.")
    for col in ["매입단가","기준가","목표가"]:
        if (df[col].dropna()<0).any(): raise ValueError(f"{col}에 음수 입력이 있습니다.")
    df["고객구분"]=df["고객구분"].fillna("고객 A").astype(str).str.strip().replace({"":"고객 A", "nan":"고객 A", "None":"고객 A"})
    df["시장"]=df["시장"].fillna("").astype(str).str.upper().str.strip()
    df["통화"]=df["통화"].fillna("").astype(str).str.upper().str.strip()
    for c in ["종목명","업종","종목유형","목표가출처","목표가기준일","비고"]:
        df[c]=df[c].fillna("").astype(str).replace("nan","")
    warnings=[]
    df["티커"] = df.apply(lambda x: symbol_to_yahoo(x["종목코드"],x["시장"]), axis=1)
    for i,r in df.iterrows():
        if not r["통화"]:
            df.at[i,"통화"]="KRW" if re.match(r"^\d{6}(\.K[QS])?$",r["종목코드"]) else "USD"
        if r["시장"]=="" and re.fullmatch(r"\d{6}", r["종목코드"]):
            warnings.append(f"{r['종목코드']}: 시장 미기입으로 KOSPI(.KS)를 가정했습니다. KOSDAQ이면 시장=KOSDAQ을 입력하세요.")
        if r["통화"] and r["통화"] not in ["KRW","USD"]:
            warnings.append(f"{r['종목코드']}: 통화 {r['통화']}은(는) V1 원화환산에 포함되지 않습니다.")
    if df.duplicated(["고객구분","종목코드"]).any():
        warnings.append("동일 고객·종목코드 중복 행이 있습니다. 포지션별로 별도 표시되며 합산 지표에는 포함됩니다.")
    return df.reset_index(drop=True), list(dict.fromkeys(warnings))

def symbol_to_yahoo(code: str, market: str="") -> str:
    code=_clean_symbol(code)
    m=str(market).strip().upper()
    if code.endswith(('.KS','.KQ')): return code
    if re.fullmatch(r"\d{6}",code):
        return code + (".KQ" if m in {"KOSDAQ","KQ","코스닥"} else ".KS")
    return code

def safe_div(a,b):
    try:
        if pd.isna(a) or pd.isna(b) or float(b)==0:return np.nan
        return float(a)/float(b)
    except (ValueError,TypeError): return np.nan

def enrich_positions(df:pd.DataFrame, quotes:dict, krw_per_usd:float|None) -> pd.DataFrame:
    """Quotes are symbol-> {'price': float, 'date':str,'history':DataFrame,...}."""
    out=df.copy()
    out['현재가']=out.apply(lambda r: quotes.get(r['티커'],{}).get('price',np.nan),axis=1)
    out['가격기준일']=out.apply(lambda r: quotes.get(r['티커'],{}).get('date',''),axis=1)
    out['가격출처']=out.apply(lambda r: quotes.get(r['티커'],{}).get('source',''),axis=1)
    # In manual mode, '기준가' is a fallback ONLY if the download has no price.
    manual=out['현재가'].isna() & out['기준가'].notna() & (out['기준가']>0)
    out.loc[manual,'현재가']=out.loc[manual,'기준가']
    out.loc[manual,'가격출처']='파일 입력 기준가'
    out.loc[manual,'가격기준일']='사용자 입력 (기준일 미제공)'
    out['평가금액(현지)']=out['현재가']*out['보유수량']
    out['투자원금(현지)']=out['매입단가']*out['보유수량']
    out['평가손익(현지)']=out['평가금액(현지)']-out['투자원금(현지)']
    out.loc[out['매입단가'].isna() | (out['매입단가']<=0),['투자원금(현지)','평가손익(현지)']]=np.nan
    out['수익률(%)']=out.apply(lambda r:100*(safe_div(r['현재가'],r['매입단가'])-1) if pd.notna(r['현재가']) and pd.notna(r['매입단가']) and r['매입단가']>0 else np.nan,axis=1)
    out['환산율']=out['통화'].map({'KRW':1.0,'USD':float(krw_per_usd) if krw_per_usd and krw_per_usd>0 else np.nan})
    out['평가액(원)']=out['평가금액(현지)']*out['환산율']
    out['손익(원)']=out['평가손익(현지)']*out['환산율']
    out['평가액비중(%)']=out['평가액(원)']/out['평가액(원)'].sum(min_count=1)*100
    out['20일선']=out['티커'].map(lambda x:quotes.get(x,{}).get('ma20',np.nan))
    out['60일선']=out['티커'].map(lambda x:quotes.get(x,{}).get('ma60',np.nan))
    out['120일선']=out['티커'].map(lambda x:quotes.get(x,{}).get('ma120',np.nan))
    out['52주고점']=out['티커'].map(lambda x:quotes.get(x,{}).get('high52',np.nan))
    out['52주고점대비(%)']=out.apply(lambda r:100*(safe_div(r['현재가'],r['52주고점'])-1) if pd.notna(r['52주고점']) else np.nan,axis=1)
    out['1개월수익률(%)']=out['티커'].map(lambda x:quotes.get(x,{}).get('ret_1m',np.nan))
    out['3개월수익률(%)']=out['티커'].map(lambda x:quotes.get(x,{}).get('ret_3m',np.nan))
    out['모니터링']=out.apply(monitoring_label,axis=1)
    return out

def monitoring_label(row):
    items=[]
    w=row.get('평가액비중(%)',np.nan)
    if pd.notna(w) and w>=15:items.append('비중 집중')
    if pd.notna(row.get('52주고점대비(%)',np.nan)) and row['52주고점대비(%)']<=-25:items.append('고점대비 낙폭')
    a,b=row.get('20일선',np.nan),row.get('60일선',np.nan)
    if pd.notna(a) and pd.notna(b) and a<b:items.append('추세 확인')
    if pd.isna(row.get('현재가',np.nan)):items.append('가격 미조회')
    return ' · '.join(items) or '정기 점검'

def build_individual_note(row) -> list[str]:
    """Deterministic analysis only: no unsupported target or trading instructions."""
    notes=[]
    wt=row.get('평가액비중(%)',np.nan)
    if pd.notna(wt):
        if wt>=15:notes.append(f"전체 평가자산에서 {wt:.1f}%를 차지해 단일종목 집중위험을 점검할 필요가 있습니다.")
        elif wt>=8:notes.append(f"평가자산 내 비중 {wt:.1f}%로, 업종 및 ETF 중복 노출을 확인할 만합니다.")
    ret=row.get('수익률(%)',np.nan)
    if pd.notna(ret):notes.append(f"입력 매입단가 대비 평가수익률 {ret:+.1f}%입니다. 이는 실현손익 및 배당을 포함하지 않습니다.")
    else:notes.append("매입단가가 없어 보유수익률은 계산하지 않았습니다.")
    ma20,ma60=row.get('20일선',np.nan),row.get('60일선',np.nan)
    if pd.notna(ma20) and pd.notna(ma60):
        notes.append("20일 이동평균이 60일선보다 높아 중단기 추세가 상대적으로 강한 모습입니다." if ma20>ma60 else "20일 이동평균이 60일선 이하에 있어 추세 변화를 살펴볼 필요가 있습니다.")
    else:notes.append("이동평균을 계산할 시계열 데이터가 부족합니다. 추세 판단을 보류합니다.")
    h=row.get('52주고점대비(%)',np.nan)
    if pd.notna(h):notes.append(f"최근 52주 종가 최고치 대비 {h:.1f}% 수준입니다.")
    t=row.get('목표가',np.nan); price=row.get('현재가',np.nan)
    if pd.notna(t) and t>0:
        src=str(row.get('목표가출처') or '출처 미입력').strip()
        day=str(row.get('목표가기준일') or '날짜 미입력').strip()
        if pd.notna(price) and price>0:
            notes.append(f"입력된 외부 목표가 대비 가격 차이는 {(t/price-1)*100:+.1f}%입니다 (출처: {src}, 발표일: {day}). 목표가의 유효성은 별도 확인이 필요합니다.")
        else:notes.append(f"목표가 {t:,.0f}({src}, {day})가 입력됐지만 현재가 부재로 괴리율은 계산하지 않았습니다.")
    else:notes.append("검증 가능한 목표주가가 등록되지 않아 수치 전망을 제시하지 않습니다.")
    notes.append("관리 전략: 다음 실적 발표·핵심 뉴스·동종업종 익스포저를 확인하고 필요 시 담당자 검토 후 재평가합니다.")
    return notes

def portfolio_summary(df:pd.DataFrame) -> dict:
    assessed=df['평가액(원)'].dropna()
    complete=bool(len(assessed)==len(df) and len(df)>0)
    total=float(assessed.sum()) if len(assessed) else np.nan
    gain=float(df['손익(원)'].sum()) if bool(df['손익(원)'].notna().all()) else np.nan
    principal=total-gain if pd.notna(total) and pd.notna(gain) else np.nan
    hhi=float(((df['평가액(원)']/total)**2).sum()) if pd.notna(total) and total>0 and complete else np.nan
    return {'positions':len(df),'clients':df['고객구분'].nunique(),'value_krw':total,'unpriced':int(df['현재가'].isna().sum()),'unconverted':int(df['평가액(원)'].isna().sum()),'complete_total':complete,'gain_krw':gain,'profit_pct':100*gain/principal if pd.notna(principal) and principal>0 else np.nan,'largest_weight':float(df['평가액비중(%)'].max()) if len(assessed) else np.nan,'hhi':hhi}

def risk_findings(df:pd.DataFrame) -> list[str]:
    m=portfolio_summary(df); n=[]
    if not m['complete_total']:n.append(f"전체 {len(df)}건 중 {m['unconverted']}건의 원화 평가액이 누락되어 합계·비중은 확인 가능한 종목만 기준입니다.")
    if m['unpriced']:n.append(f"{m['unpriced']}개 포지션의 가격을 조회하지 못했습니다. 해당 포지션은 평가액 계산에서 제외됐습니다.")
    if pd.notna(m['largest_weight']) and m['complete_total'] and m['largest_weight']>=15:n.append(f"최대 단일포지션 비중이 {m['largest_weight']:.1f}%입니다. 집중 위험을 점검하세요.")
    sec=df.groupby('업종',dropna=False)['평가액(원)'].sum(min_count=1).sort_values(ascending=False)
    if len(sec)>0 and pd.notna(m['value_krw']) and m['value_krw']>0 and m['complete_total']:
        sec=sec[sec.index.astype(str).str.strip()!='']
        if len(sec)>0 and (sec.iloc[0]/m['value_krw']>0.35):n.append(f"입력 업종 분류상 {sec.index[0]} 비중이 {sec.iloc[0]/m['value_krw']*100:.1f}%입니다. ETF 기초자산 중복은 아직 반영되지 않았습니다.")
    etf=df['종목유형'].astype(str).str.contains('ETF',case=False,na=False).sum()
    if etf>0:n.append(f"ETF {etf}건이 포함돼 있습니다. V1은 ETF 보유비중을 구성종목으로 전개하지 않으므로 개별주식과의 실질 중복 노출은 계산하지 않습니다.")
    n.append('가격, 실적, 뉴스 및 입력된 목표가는 그 출처·기준시점을 확인해야 합니다. 본 결과는 자동매매 신호 또는 투자 자문 의견이 아닙니다.')
    return n

def to_csv_bytes(df:pd.DataFrame)->bytes:
    # Escape spreadsheet formula prefixes from all untrusted string cells before CSV export.
    safe=df.copy()
    for col in safe.select_dtypes(include=['object','string']).columns:
        safe[col]=safe[col].map(lambda v: '\u0027'+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')) else v)
    return '\ufeff'.encode('utf-8')+safe.to_csv(index=False).encode('utf-8')
