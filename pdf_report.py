"""Local PDF export. No data transmitted or persisted."""
from __future__ import annotations
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
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
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,PageBreak,KeepTogether
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

def build_pdf(df:pd.DataFrame,mode:str,fx_source:str,demo:bool=False)->bytes:
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
    flow.append(par('상세 모니터링 · 평가액 상위 10개 종목','HKHead'))
    for _,r in sorted_df.head(10).iterrows():
        el=[par(f"{r['고객구분']} · {str(r['종목명']).strip() or r['종목코드']} ({r['종목코드']})",'HKHead')]
        el.extend(par('• '+x,'HKSmall') for x in build_individual_note(r))
        flow.append(KeepTogether(el))
    flow.append(par('자료 이용 안내','HKHead'))
    flow.append(par('입력한 고객별칭·보유수량·매입단가는 제3자에게 자동 전송하지 않습니다. 외부 시세/뉴스 요청 시 공개 티커·회사명만 조회합니다. 서비스의 데이터 제공 허용 범위, 고객정보 처리 및 외부 인프라 사용 여부는 회사 승인을 받아야 합니다.','HKSmall'))
    flow.append(par('이 문서는 담당자의 사실확인 및 준법 검토 전에는 고객 제공용 공식 리서치·투자자문 자료가 아닙니다. 목표가는 사용자 입력값만 표시하며 향후 성과를 보증하지 않습니다.','HKSmall'))
    def page(c,d):
        c.saveState();c.setFont(font,7);c.setFillColor(grey)
        c.drawString(17*mm,11*mm,'HanaV Private Portfolio  |  Internal working draft')
        c.drawRightString(195*mm,11*mm,str(d.page));c.restoreState()
    doc.build(flow,onFirstPage=page,onLaterPages=page)
    return buff.getvalue()
