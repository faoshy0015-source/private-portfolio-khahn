"""Local PDF export. No data transmitted or persisted."""
from __future__ import annotations
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from urllib.parse import urlsplit
import os
import math
import pandas as pd
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,KeepTogether,HRFlowable
from core import portfolio_summary, risk_findings, build_individual_note

# Korean fonts installed at OS-level; in Streamlit Cloud see packages.txt.
# The font binary is never included in this repository.
FONT_PATHS = [
    # Debian/Ubuntu: apt install fonts-nanum
    '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
    '/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf',
    '/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf',
    # Some distributions install Korean fonts elsewhere
    '/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf',
    '/usr/local/share/fonts/NanumGothic.ttf',
    os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'malgun.ttf'),
    os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'NanumGothic.ttf'),
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Fonts', 'NanumGothic.ttf'),
]


def register_font():
    """Use an embeddable Korean TTF whenever available.

    Last-resort built-in CID font is only a fallback; it may be substituted in
    some PDF readers. The recommended Cloud path is packages.txt/fonts-nanum.
    """
    registered = set(pdfmetrics.getRegisteredFontNames())
    if 'HANAV_KO' in registered:
        return 'HANAV_KO'
    for path in FONT_PATHS:
        if not Path(path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont('HANAV_KO', path))
            return 'HANAV_KO'
        except (Exception):
            continue
    # A PDF should still be exportable even when an apt install was skipped.
    # This fallback doesn't embed glyph outlines; viewers may substitute fonts.
    if 'HYGothic-Medium' not in registered:
        pdfmetrics.registerFont(UnicodeCIDFont('HYGothic-Medium'))
    return 'HYGothic-Medium'

def fmt(v,d=0):
    try:return f'{float(v):,.{d}f}' if pd.notna(v) else '—'
    except Exception:return '—'

def build_pdf(df:pd.DataFrame,mode:str,fx_source:str,demo:bool=False,
              research_map:dict|None=None,news_map:dict|None=None,quote_map:dict|None=None)->bytes:
    research_map=research_map or {}
    news_map=news_map or {}
    quote_map=quote_map or {}
    font=register_font()
    buff=BytesIO()
    doc=SimpleDocTemplate(buff,pagesize=A4,topMargin=17*mm,bottomMargin=16*mm,leftMargin=17*mm,rightMargin=17*mm,
                          title='HanaV Private Portfolio 분석보고서',author='HanaV Private Portfolio')
    green=colors.HexColor('#087D57'); dark=colors.HexColor('#162A25'); grey=colors.HexColor('#65736E')
    sty=getSampleStyleSheet()
    sty.add(ParagraphStyle(name='HKTitle',fontName=font,fontSize=18,leading=26,textColor=green,spaceAfter=6))
    sty.add(ParagraphStyle(name='HKHead',fontName=font,fontSize=11,leading=16,textColor=dark,spaceBefore=12,spaceAfter=6))
    sty.add(ParagraphStyle(name='HKNormal',fontName=font,fontSize=8.7,leading=14,textColor=dark,spaceAfter=5))
    sty.add(ParagraphStyle(name='HKSmall',fontName=font,fontSize=7,leading=11,textColor=grey,spaceAfter=5))
    sty.add(ParagraphStyle(name='HKDetail',fontName=font,fontSize=7.8,leading=12.2,textColor=dark,spaceAfter=4))
    sty.add(ParagraphStyle(name='HKSub',fontName=font,fontSize=7.4,leading=11.4,textColor=green,spaceAfter=4))
    cell=ParagraphStyle(name='HKCell',fontName=font,fontSize=7,leading=10,textColor=dark)
    headercell=ParagraphStyle(name='HKHeaderCell',fontName=font,fontSize=7,leading=10,textColor=colors.white)
    def par(s,style='HKNormal'):return Paragraph(escape(str(s)),sty[style])
    def pc(s,header=False):return Paragraph(escape(str(s)),headercell if header else cell)
    m=portfolio_summary(df); flow=[]
    flow.append(par('HANAV PRIVATE PORTFOLIO','HKTitle'))
    flow.append(par('VVIP 자산 현황 및 리스크 모니터링 리포트','HKHead'))
    flow.append(par(f"발행일 {datetime.now().strftime('%Y-%m-%d %H:%M')} | 가격모드: {mode} | 환율: {fx_source}",'HKSmall'))
    if demo:flow.append(par('가상 시뮬레이션 데이터입니다. 실제 자산·가격·수익이 아닙니다.','HKHead'))
    flow.append(par(f"보유 포지션 {m['positions']:,}개 · 구분 계정 {m['clients']:,}개 · 원화 평가 가능 {m['positions']-m['unconverted']:,}개"))
    flow.append(par(f"평가가능 금액 합계 {fmt(m['value_krw'])}원" + (' (일부 포지션 누락, 전체 자산 합계 아님)' if not m['complete_total'] else '')))
    if not pd.isna(m['gain_krw']):flow.append(par(f"입력 매입단가 기준 평가손익 {fmt(m['gain_krw'])}원 / 손익률 {fmt(m['profit_pct'],2)}% (배당·수수료·실현손익 제외)"))
    flow.append(par('주요 점검사항','HKHead'))
    for x in risk_findings(df):flow.append(par('• '+x))
    total_tickers=df['티커'].astype(str).nunique() if '티커' in df else len(df)
    covered_quotes=sum(t in quote_map and bool(quote_map[t]) for t in df['티커'].astype(str).unique()) if '티커' in df else 0
    covered_deep=sum(t in research_map and bool(research_map[t]) for t in df['티커'].astype(str).unique()) if '티커' in df else 0
    covered_news=sum(t in news_map and bool(news_map[t]) for t in df['티커'].astype(str).unique()) if '티커' in df else 0
    flow.append(par(f'시장자료 수집 현황: 가격추세 {covered_quotes}/{total_tickers}종목 · 기업분석 {covered_deep}/{total_tickers}종목 · 뉴스 {covered_news}/{total_tickers}종목','HKSmall'))
    flow.append(par('종목별 평가 현황','HKHead'))
    headers=['고객별칭','종목','수량','현재가','평가액(원)','비중','관리상태']
    rows=[[pc(h,True) for h in headers]]
    sorted_df=df.sort_values('평가액(원)',ascending=False,na_position='last')
    for _,r in sorted_df.iterrows():
        label=str(r['종목명']).strip() or str(r['종목코드'])
        if len(label)>21:label=label[:19]+'…'
        rows.append([pc(str(r['고객구분'])[:8]),pc(label),pc(fmt(r['보유수량'])),pc(fmt(r['현재가'],2)),pc(fmt(r['평가액(원)'])),pc(fmt(r['평가액비중(%)'],1)+'%'),pc(str(r['모니터링'])[:12])])
    tbl=Table(rows,colWidths=[23*mm,36*mm,17*mm,23*mm,30*mm,15*mm,31*mm],repeatRows=1,hAlign='LEFT')
    tbl.setStyle(TableStyle([
       ('BACKGROUND',(0,0),(-1,0),green),('TEXTCOLOR',(0,0),(-1,0),colors.white),
       ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F2F7F5')]),
       ('LINEBELOW',(0,0),(-1,0),0.5,green),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
       ('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),
    ]))
    flow.append(tbl)
    flow.append(PageBreak())
    flow.append(par('전 종목 개별 분석 카드','HKTitle'))
    flow.append(par('보유 중인 모든 포지션을 순서대로 점검합니다. 자료가 부족한 항목은 미조회로 표시되며, 동일한 투자전략 문장을 자동 반복하지 않습니다.','HKSmall'))
    flow.append(par('가격추세 및 업계 지표는 외부 공개 데이터 조회 성공 시에만 기입합니다. 엑셀 입력 기준가와 조회 종가를 혼동하지 마세요.','HKSmall'))

    def nonempty_number(value):
        try:
            f=float(value)
            return f if math.isfinite(f) else None
        except (TypeError,ValueError,OverflowError):
            return None

    def fmt_percent(value):
        v=nonempty_number(value)
        return f'{v:+.1f}%' if v is not None else '미조회'

    def text_value(value):
        return str(value).strip() if value is not None and str(value).strip() and str(value).strip().lower()!='nan' else '미기재'

    for serial,(_,r) in enumerate(sorted_df.iterrows(),1):
        symbol=str(r.get('티커','')).strip()
        label=text_value(r.get('종목명',''))
        if label=='미기재':label=str(r['종목코드'])
        res=research_map.get(symbol,{}) or {}
        quoted=quote_map.get(symbol,{}) or {}
        deep_quote=res.get('quote',{}) or {}
        q=deep_quote if nonempty_number(deep_quote.get('price')) is not None else quoted
        info=res.get('info',{}) or {}
        targets=res.get('analyst_targets',{}) or {}
        is_etf='ETF' in str(r.get('종목유형','')).upper()

        parts=[par(f'{serial:02d}. {r["고객구분"]}  |  {label}  ({r["종목코드"]})','HKHead')]
        valtxt=f"보유 {fmt(r.get('보유수량'))}주  ·  평가액 {fmt(r.get('평가액(원)'))}원  ·  비중 {fmt(r.get('평가액비중(%)'),1)}%"
        parts.append(par(valtxt,'HKSub'))
        pf_price=nonempty_number(r.get('현재가'))
        src=text_value(r.get('가격출처'))
        date=text_value(r.get('가격기준일'))
        parts.append(par(f"[계좌 평가] 적용가격 {fmt(pf_price,2)} {r.get('통화','')}  |  기준 {date}  |  출처 {src}",'HKDetail'))
        gain=nonempty_number(r.get('수익률(%)'))
        if gain is not None:parts.append(par(f'[보유현황] 입력 매입단가 대비 미실현 손익률 {gain:+.1f}% (배당·실현손익·비용 제외)','HKDetail'))
        else:parts.append(par('[보유현황] 매입단가 미제공으로 수익률 산정 보류','HKDetail'))

        market_price=nonempty_number(q.get('price'))
        if market_price is not None:
            parts.append(par(f"[시장 추세] 조회 종가 {fmt(market_price,2)}  |  {text_value(q.get('date'))}  |  1개월 {fmt_percent(q.get('ret_1m'))}  /  3개월 {fmt_percent(q.get('ret_3m'))}  |  {text_value(q.get('source'))}",'HKDetail'))
            ma20=nonempty_number(q.get('ma20'));ma60=nonempty_number(q.get('ma60'))
            high=nonempty_number(q.get('high52'))
            stats=[]
            if ma20 is not None and ma60 is not None:
                stats.append(f'20일선 {fmt(ma20,2)} / 60일선 {fmt(ma60,2)}')
                stats.append('단기선 > 중기선' if ma20>ma60 else '단기선 ≤ 중기선')
            if high and high>0:stats.append(f'52주 종가고점 대비 {(market_price/high-1)*100:+.1f}%')
            if stats:parts.append(par('[기술 지표] '+'  ·  '.join(stats),'HKDetail'))
        else:
            # There may still be a previous app valuation price, but that is not market research.
            parts.append(par('[시장 추세] 별도 종가·이동평균 데이터 미수집 (엑셀 입력가로 추세를 추정하지 않음)','HKDetail'))

        fin=[]
        pe=nonempty_number(info.get('trailingPE'))
        roe=nonempty_number(info.get('returnOnEquity'))
        rev=nonempty_number(info.get('revenueGrowth'))
        if pe is not None:fin.append(f'PER {pe:,.1f}배')
        if roe is not None:fin.append(f'ROE {roe*100:,.1f}%')
        if rev is not None:fin.append(f'매출성장률 {rev*100:+.1f}%')
        if fin:parts.append(par('[기업 지표] '+'  ·  '.join(fin)+' (Yahoo 공개 집계, 기간·기준 추가 확인 필요)','HKDetail'))
        else:parts.append(par('[기업 지표] '+('ETF는 기업 PER/ROE 비교대상에서 제외' if is_etf else '공개 실적 및 밸류에이션 자료 미수집'),'HKDetail'))

        estimates=res.get('quarters',[]) or []
        latest=estimates[0] if estimates else None
        if latest:
            qbits=[]
            for key in ('매출액','영업이익','당기순이익'):
                v=nonempty_number(latest.get(key))
                if v is not None:qbits.append(f'{key} {v/1e8:,.1f}억')
            if qbits:
                cc=text_value(res.get('financial_currency'))
                parts.append(par(f"[분기 실적] {text_value(latest.get('기간'))} · {' / '.join(qbits)} ({cc} 기준)",'HKDetail'))
        user_target=nonempty_number(r.get('목표가'))
        mean=nonempty_number(targets.get('mean'))
        if is_etf:
            parts.append(par('[목표가] ETF는 개별 기업 애널리스트 목표가와 직접 비교하지 않습니다.','HKDetail'))
        elif user_target and user_target>0:
            parts.append(par(f"[목표가] 입력값 {fmt(user_target,2)} {r.get('통화','')} · {text_value(r.get('목표가출처'))} · {text_value(r.get('목표가기준일'))} (사용자 입력값, 검증 필요)",'HKDetail'))
        elif mean is not None:
            cc=text_value(res.get('price_currency'))
            parts.append(par(f'[컨센서스] Yahoo 집계 목표가 평균 {fmt(mean,2)} {cc} (개별 기관·발표일 미확인, 검증 전 참고)','HKDetail'))
        else:parts.append(par('[목표가] 근거·발표일이 확인된 목표주가 없음','HKDetail'))

        articles=news_map.get(symbol,[]) or []
        if articles:
            entry=articles[0]
            headline=text_value(entry.get('제목'))
            if len(headline)>100:headline=headline[:98]+'…'
            parts.append(par(f"[최근 뉴스] {headline}  |  {text_value(entry.get('언론사'))}  |  {text_value(entry.get('발행일'))}",'HKDetail'))
            url=str(entry.get('원문',''))
            if url and urlsplit(url).scheme=='https' and urlsplit(url).netloc:
                link=escape(url,{'\"':'&quot;'})
                parts.append(Paragraph(f'[뉴스 원문] <link href=\"{link}\" color=\"#087D57\">기사 원문 바로가기</link>',sty['HKSmall']))
        else:parts.append(par('[최근 뉴스] 해당 종목 뉴스 미조회 또는 검색 결과 없음','HKDetail'))
        # Risk note is conditional and evidence based, not a blanket buy/sell signal.
        flags=[]
        wt=nonempty_number(r.get('평가액비중(%)'))
        if wt is not None and wt>=10:flags.append(f'보유 집중도 {wt:.1f}%')
        if market_price is not None and q.get('high52') is not None:
            h=nonempty_number(q.get('high52'))
            if h and (market_price/h-1)*100<-25:flags.append('52주 고점대비 -25% 초과 하락')
        if market_price is not None:
            fast=nonempty_number(q.get('ma20'));slow=nonempty_number(q.get('ma60'))
            if fast is not None and slow is not None and fast<slow:
                flags.append('20일선이 60일선을 하회')
        if flags:parts.append(par('[추가 점검] '+' · '.join(flags),'HKDetail'))
        elif market_price is not None and info:
            parts.append(par('[관리 포인트] 실적 전망 변경과 가격 추세가 동시에 유지되는지 정기 확인','HKDetail'))
        parts.append(Spacer(1,3*mm))
        flow.append(KeepTogether(parts))
    flow.append(par('자료 이용 안내','HKHead'))
    flow.append(par('시세/뉴스 조회에는 공개 티커·회사명만 요청합니다. 다만 온라인 Streamlit Cloud에 업로드한 자료는 해당 서버에서 처리되므로, 실고객의 보유내역 사용 전 보안·전산·준법 승인 및 데이터 라이선스 검토가 필요합니다.','HKSmall'))
    flow.append(par('이 문서는 사실확인 및 준법 검토 전 고객 제공용 공식 리서치·투자자문 자료가 아닙니다. 제공처 집계 목표가와 사용자 입력 목표가의 시점 및 정확성은 별도 확인해야 하며 미래 수익을 보증하지 않습니다.','HKSmall'))
    def page(c,d):
        c.saveState();c.setFont(font,7);c.setFillColor(grey)
        c.drawString(17*mm,11*mm,'HanaV Private Portfolio  |  Internal working draft')
        c.drawRightString(195*mm,11*mm,str(d.page));c.restoreState()
    doc.build(flow,onFirstPage=page,onLaterPages=page)
    return buff.getvalue()
