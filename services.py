"""Public data adapters. Portfolio contents never sent to third-party APIs."""
from __future__ import annotations
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import email.utils
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests


def _extract_price_history(frame: pd.DataFrame) -> dict:
    if frame is None or frame.empty: return {}
    if isinstance(frame.columns,pd.MultiIndex):
        try: frame=frame.droplevel(-1,axis=1)
        except Exception:return {}
    try:
        close=pd.to_numeric(frame['Close'],errors='coerce').dropna()
        if close.empty:return {}
        ts=close.index[-1]
        if hasattr(ts,'date'): date=ts.date().isoformat()
        else: date=str(ts)[:10]
        def r(n):
            return float((close.iloc[-1]/close.iloc[-n-1]-1)*100) if len(close)>n else np.nan
        return {'price':float(close.iloc[-1]),'date':date,'source':'Yahoo Finance (지연 가능 / 종가)',
                'ma20':float(close.tail(20).mean()) if len(close)>=20 else np.nan,
                'ma60':float(close.tail(60).mean()) if len(close)>=60 else np.nan,
                'ma120':float(close.tail(120).mean()) if len(close)>=120 else np.nan,
                'high52':float(close.tail(252).max()) if len(close)>=20 else np.nan,
                'ret_1m':r(21),'ret_3m':r(63),'history':close}
    except (KeyError,ValueError,TypeError):return {}


def fetch_history_batch(tickers: list[str], callback=None) -> tuple[dict,list[str]]:
    """Fetch public tickers only. Does NOT transmit quantity, cost basis or client aliases.

    Yahoo may rate-limit or block fetching; those tickers must remain unpriced.
    """
    import yfinance as yf
    symbols=list(dict.fromkeys(str(t).strip() for t in tickers if str(t).strip()))
    output={}; errors=[]
    if not symbols:return {}, []
    chunks=[symbols[i:i+20] for i in range(0,len(symbols),20)]
    for i,chunk in enumerate(chunks):
        try:
            hist=yf.download(tickers=chunk,period='1y',interval='1d',group_by='ticker',
                             threads=True,progress=False,auto_adjust=False,timeout=18)
            if isinstance(hist,pd.DataFrame) and not hist.empty:
                for ticker in chunk:
                    try:
                        if isinstance(hist.columns,pd.MultiIndex):
                            if ticker in hist.columns.get_level_values(0):
                                sub=hist[ticker]
                            elif ticker in hist.columns.get_level_values(1):
                                sub=hist.xs(ticker,axis=1,level=1)
                            else: sub=pd.DataFrame()
                        else: sub=hist if len(chunk)==1 else pd.DataFrame()
                        result=_extract_price_history(sub)
                        if result:output[ticker]=result
                        else:errors.append(f'{ticker}: 가격 이력 없음')
                    except Exception as e:errors.append(f'{ticker}: {str(e)[:90]}')
            else:errors.extend(f'{t}: 응답 데이터 없음' for t in chunk)
        except Exception as e:
            errors.append(f'시세 그룹 {i+1}: {str(e)[:140]}')
        if callback:callback((i+1)/len(chunks))
    return output,errors


def fetch_fx_krw_per_usd()->tuple[float|None,str]:
    try:
        import yfinance as yf
        data=yf.download('KRW=X',period='5d',interval='1d',progress=False,auto_adjust=False,threads=False,timeout=14)
        if data is None or data.empty:return None,'USD/KRW 시세가 없습니다.'
        close=data['Close']
        if isinstance(close,pd.DataFrame):close=close.iloc[:,0]
        v=pd.to_numeric(close,errors='coerce').dropna()
        if not v.empty and v.iloc[-1]>0:return float(v.iloc[-1]),'Yahoo Finance USD/KRW · 지연 종가'
    except Exception as e:return None,f'환율 조회 오류: {str(e)[:100]}'
    return None,'환율 조회 실패'


def fetch_google_news(ticker:str, name:str='',limit:int=5)->tuple[list[dict],str]:
    """News indexed by ticker/company only; no customer-level fields."""
    key=name.strip() if str(name).strip() else ticker.replace('.KS','').replace('.KQ','')
    if len(key)<2: key=ticker
    query=f'"{key}" 주식 OR 실적' if ticker.endswith(('.KS','.KQ')) else f'"{key}" stock earnings'
    params={'q':query,'hl':'ko','gl':'KR','ceid':'KR:ko'}
    url='https://news.google.com/rss/search?'+urllib.parse.urlencode(params)
    try:
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0 (HanaV Portfolio research)'},timeout=12)
        r.raise_for_status()
        root=ET.fromstring(r.content)
        articles=[]
        for item in root.findall('.//channel/item')[:limit]:
            title=(item.findtext('title') or '').strip()
            link=(item.findtext('link') or '').strip()
            date=(item.findtext('pubDate') or '').strip()
            source=item.find('source')
            src=source.text.strip() if source is not None and source.text else 'Google News'
            if title and link and link.startswith('https://'):
                articles.append({'제목':title,'발행일':date,'언론사':src,'원문':link})
        return articles,('Google News RSS 검색 결과이며 종목 관련성을 반드시 확인하세요.' if articles else '일치하는 기사 없음')
    except Exception as e:
        return [],f'뉴스 조회 실패: {str(e)[:150]}'


def fetch_news_batch(securities:list[tuple[str,str]], callback=None, max_workers=4)->tuple[dict,dict]:
    names=list(dict.fromkeys((str(t),str(n)) for t,n in securities))
    result={};failed={}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map={executor.submit(fetch_google_news,t,n):t for t,n in names}
        for i,f in enumerate(as_completed(future_map)):
            key=future_map[f]
            try: articles,message=f.result()
            except Exception as e:articles,message=[],str(e)
            result[key]=articles
            if not articles:failed[key]=message
            if callback:callback((i+1)/len(future_map))
    return result,failed
