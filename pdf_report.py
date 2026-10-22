"""Premium, evidence-first portfolio PDF. No third-party requests or persistence.

Builds from account valuation and market research already retrieved by the Streamlit app.
Never imputes missing forecasts or substitutes purchase price for market data.
"""
from __future__ import annotations
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
import math
import os
import pandas as pd
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    KeepTogether, HRFlowable,
)
from core import portfolio_summary, risk_findings

BASE = colors.HexColor('#14664C')
DARK = colors.HexColor('#162A26')
MUTED = colors.HexColor('#64766E')
PALE = colors.HexColor('#EDF6F1')
PANEL = colors.HexColor('#F7FAF8')
LINE = colors.HexColor('#D5E4DC')
AMBER = colors.HexColor('#A46817')

FONT_PATHS = [
    ('/usr/share/fonts/truetype/nanum/NanumGothic.ttf', '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf'),
    ('/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf', '/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf'),
    ('/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf', None),
    (os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'malgun.ttf'),
     os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'malgunbd.ttf')),
    (os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Windows', 'Fonts', 'NanumGothic.ttf'), None),
]


def register_font():
    """Use host-installed Korean font; NEVER redistribute font binaries."""
    existing = set(pdfmetrics.getRegisteredFontNames())
    if 'HVP' in existing:
        return 'HVP', ('HVPB' if 'HVPB' in existing else 'HVP')
    for regular, bold in FONT_PATHS:
        if Path(regular).is_file():
            try:
                pdfmetrics.registerFont(TTFont('HVP', regular))
                if bold and Path(bold).is_file():
                    pdfmetrics.registerFont(TTFont('HVPB', bold))
                    bold_name = 'HVPB'
                else:
                    bold_name = 'HVP'
                pdfmetrics.registerFontFamily('HVP', normal='HVP', bold=bold_name)
                return 'HVP', bold_name
            except Exception:
                continue
    if 'HYGothic-Medium' not in existing:
        pdfmetrics.registerFont(UnicodeCIDFont('HYGothic-Medium'))
    return 'HYGothic-Medium', 'HYGothic-Medium'


def number(val):
    try:
        x = float(val)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def txt(val, default='미수집'):
    if val is None: return default
    x = str(val).strip()
    return x if x and x.lower() not in ('nan', 'none', 'nat') else default


def fmt(val, digits=0, suffix=''):
    v = number(val)
    return f'{v:,.{digits}f}{suffix}' if v is not None else '—'


def pct(val):
    v = number(val)
    return f'{v:+.1f}%' if v is not None else '—'


def won(val):
    v = number(val)
    if v is None: return '평가 불가'
    if abs(v) >= 1e8: return f'{v/1e8:,.2f}억 원'
    if abs(v) >= 1e4: return f'{v/1e4:,.0f}만 원'
    return f'{v:,.0f}원'


def valid_url(url):
    try:
        p = urlsplit(str(url).strip())
        return p.scheme == 'https' and bool(p.hostname)
    except Exception:
        return False


def has_fundamentals(r):
    if not isinstance(r, dict): return False
    i = r.get('info') or {}
    keys = ('trailingPE','forwardPE','returnOnEquity','revenueGrowth','profitMargins','totalRevenue','marketCap')
    info_found = isinstance(i, dict) and any(number(i.get(k)) is not None for k in keys)
    quarters = bool(r.get('quarters'))
    target = isinstance(r.get('analyst_targets'), dict) and number(r['analyst_targets'].get('mean')) is not None
    return info_found or quarters or target


def styles(font, bold):
    s={}
    def add(k, **kw):
        s[k] = ParagraphStyle(name='HVP_'+k, fontName=kw.pop('fontName',font),
                              fontSize=kw.pop('fontSize',8.5), leading=kw.pop('leading',13),
                              textColor=kw.pop('textColor',DARK), spaceAfter=0, **kw)
    add('brand',fontName=bold,fontSize=11,leading=15,textColor=BASE)
    add('main',fontName=bold,fontSize=19,leading=26)
    add('section',fontName=bold,fontSize=13,leading=20)
    add('cardtitle',fontName=bold,fontSize=13.2,leading=18)
    add('micro',fontSize=8.0,leading=12,textColor=MUTED)
    add('normal',fontSize=8.6,leading=13.4)
    add('body',fontSize=8.9,leading=14)
    add('small',fontSize=8.6,leading=13.2)
    add('smallgreen',fontName=bold,fontSize=8.6,leading=12.5,textColor=BASE)
    add('label',fontName=bold,fontSize=8.0,leading=12,textColor=MUTED)
    add('number',fontName=bold,fontSize=13.1,leading=18)
    add('tablehead',fontName=bold,fontSize=7.3,leading=10,textColor=colors.white)
    add('tablecell',fontSize=7.4,leading=11)
    add('center',fontSize=8.3,leading=13.1,alignment=TA_CENTER)
    return s


# -*- coding: utf-8 -*-
"""Static print-first portfolio infographics with ReportLab only. No network calls."""
from reportlab.platypus import Flowable

SECTOR_HEX = (
    '#B7DFD0', '#D1EAD1', '#AEDBE7', '#E5D9B3', '#CFC6E8', '#C9E4B4',
    '#E7CCCA', '#C5D9E7', '#D8D8B1', '#B6D6CD', '#E1C9A7', '#BFD6EC',
    '#E2D4DA', '#B9DFE0', '#D8E3D0', '#E2DABE', '#C3CBE1', '#E1D7C5',
)


def _chart_data(df, research_map, quote_map):
    """Aggregate the *same ticker* over accounts; count positions separately.

    A missing value cannot enter a proportion. Returns are aggregated from
    complete position-level original-cost data, never averaged blindly.
    """
    cache = {}
    for _, row in df.iterrows():
        key = txt(row.get('티커'), txt(row.get('종목코드'), '미상'))
        item = cache.setdefault(key, {
            'ticker': key, 'name': txt(row.get('종목명'), txt(row.get('종목코드'), '미상')),
            'sector': txt(row.get('업종'), '미분류'), 'value': 0.0,
            'priced': 0, 'positions': 0, 'gain': 0.0, 'cost_complete': True,
            'customers': set(),
        })
        item['positions'] += 1
        item['customers'].add(txt(row.get('고객구분'), '미분류'))
        value = number(row.get('평가액(원)'))
        if value is not None and value > 0:
            item['value'] += value
            item['priced'] += 1
        else:
            item['cost_complete'] = False
        gain = number(row.get('손익(원)'))
        if gain is None:
            item['cost_complete'] = False
        else:
            item['gain'] += gain
    for item in cache.values():
        if item['cost_complete'] and item['priced'] == item['positions']:
            cost = item['value'] - item['gain']
            item['roi'] = 100 * item['gain']/cost if cost > 0 else None
        else:
            item['roi'] = None
    items = sorted(cache.values(), key=lambda x: (-x['value'], x['name']))
    total = sum(x['value'] for x in items)
    for x in items:
        x['weight'] = 100*x['value']/total if total > 0 else None
    sector_totals = {}
    client_totals = {}
    for _, row in df.iterrows():
        val = number(row.get('평가액(원)'))
        if val is None or val <= 0:
            continue
        sector = txt(row.get('업종'), '미분류')
        customer = txt(row.get('고객구분'), '미분류')
        sector_totals[sector] = sector_totals.get(sector, 0.0) + val
        client_totals[customer] = client_totals.get(customer, 0.0) + val
    sector_order = sorted(sector_totals, key=lambda x: (-sector_totals[x], x))
    sector_colors = {k:SECTOR_HEX[i % len(SECTOR_HEX)] for i,k in enumerate(sector_order)}
    return items, total, sector_totals, client_totals, sector_colors


def _short_label(canvas, label, font, size, max_width):
    s = str(label)
    if canvas.stringWidth(s, font, size) <= max_width: return s
    while len(s) > 1 and canvas.stringWidth(s+'…', font, size) > max_width:
        s = s[:-1]
    return s+'…' if s else '…'


def _squarify_values(items, x, y, width, height):
    """Squarified rectangle packing; x,y measured from bottom left."""
    good = [a for a in items if a['value'] > 0]
    if not good or width <= 0 or height <= 0: return []
    total = sum(a['value'] for a in good)
    scaled = [(a, a['value']/total*width*height) for a in good]
    placed = []
    def worst(area_list, side):
        if not area_list or side <= 0:return float('inf')
        areas = [a for _,a in area_list]
        big=max(areas);small=min(areas);s=sum(areas)
        return max(side*side*big/(s*s),s*s/(side*side*small)) if small>0 else float('inf')
    def layout(row, bx, by, w, h):
        a_sum=sum(a for _,a in row)
        if w < h:
            row_h=a_sum / w if w else 0
            cursor=bx
            for item,area in row:
                cell_w=area/row_h if row_h else 0
                placed.append((item, cursor, by+h-row_h, cell_w, row_h))
                cursor += cell_w
            return bx,by,w,max(0,h-row_h)
        row_w=a_sum / h if h else 0
        cursor=by+h
        for item,area in row:
            cell_h=area/row_w if row_w else 0
            cursor -= cell_h
            placed.append((item,bx,cursor,row_w,cell_h))
        return bx+row_w,by,max(0,w-row_w),h
    bx,by,w,h=x,y,width,height
    row=[]
    for item in scaled:
        if not row:
            row=[item];continue
        side=min(w,h)
        if worst(row+[item],side) <= worst(row,side):
            row.append(item)
        else:
            bx,by,w,h=layout(row,bx,by,w,h)
            row=[item]
    if row:layout(row,bx,by,w,h)
    return placed


class PortfolioTreemap(Flowable):
    def __init__(self, items, sector_colors, font, bold, width, height=490):
        super().__init__()
        self.items, self.sector_colors = items, sector_colors
        self.font, self.bold, self.width, self.height = font,bold,width,height
    def draw(self):
        c = self.canv
        c.setFillColor(colors.HexColor('#F5F9F7'))
        c.roundRect(0,0,self.width,self.height,10,stroke=0,fill=1)
        pad=10
        if not any(item['value']>0 for item in self.items):
            c.setFillColor(MUTED);c.setFont(self.font,11)
            c.drawCentredString(self.width/2,self.height/2,'평가 가능한 종목이 없어 트리맵을 표시하지 않았습니다.')
            return
        geoms=_squarify_values(self.items,pad,pad,self.width-pad*2,self.height-pad*2)
        for item,x,y,w,h in geoms:
            gap=1.5
            if w<4 or h<4:continue
            c.setFillColor(colors.HexColor(self.sector_colors.get(item['sector'], '#DEE7E2')))
            c.roundRect(x+gap,y+gap,max(0,w-2*gap),max(0,h-2*gap),3,fill=1,stroke=0)
            c.setFillColor(DARK)
            cw=w-11
            if w>=45 and h>=25:
                fontsize=8.3 if w>=68 else 7.2
                c.setFont(self.bold,fontsize)
                label=_short_label(c,item['name'],self.bold,fontsize,cw)
                c.drawCentredString(x+w/2,y+h/2+3 if h>=38 else y+h/2-2,label)
                if h>=38:
                    c.setFont(self.font,7.6)
                    pct_s=f"{item['weight']:.1f}%" if item['weight'] is not None else '—'
                    c.drawCentredString(x+w/2,y+h/2-10,pct_s)
        # Bounding box for print contrast
        c.setStrokeColor(LINE);c.setLineWidth(0.6)
        c.roundRect(0,0,self.width,self.height,10,stroke=1,fill=0)


class PortfolioHeatmap(Flowable):
    def __init__(self, items, font, bold, width, height=575, columns=8):
        super().__init__()
        self.items, self.font, self.bold = items,font,bold
        self.width,self.height,self.columns = width,height,columns
    @staticmethod
    def tile_style(roi):
        if roi is None:return '#EDF1EF', DARK
        if roi>=20:return '#29977C', colors.white
        if roi>=10:return '#66BEA3', DARK
        if roi>=3:return '#A7DDCA', DARK
        if roi>=0:return '#D9F2E9', DARK
        if roi>=-3:return '#F9E2DB', DARK
        if roi>=-10:return '#F2BAAA', DARK
        if roi>=-20:return '#E78D7D', DARK
        return '#C76559', colors.white
    def draw(self):
        c=self.canv
        w=self.width
        n=len(self.items)
        nrows=max(1,math.ceil(n/self.columns))
        # Height is per-page controlled (10 rows max).
        pad=2.2
        cell_w=(w-(self.columns-1)*pad)/self.columns
        cell_h=(self.height-(nrows-1)*pad)/nrows
        for i,item in enumerate(self.items):
            col=i%self.columns;row=i//self.columns
            x=col*(cell_w+pad)
            y=self.height-(row+1)*cell_h-row*pad
            bg,fg=self.tile_style(item['roi'])
            c.setFillColor(colors.HexColor(bg));c.roundRect(x,y,cell_w,cell_h,3,stroke=0,fill=1)
            c.setFillColor(fg)
            lbl_size=7.2 if nrows>=9 else 7.8
            c.setFont(self.bold,lbl_size)
            c.drawCentredString(x+cell_w/2,y+cell_h-14,
                _short_label(c,item['name'],self.bold,lbl_size,cell_w-8))
            c.setFont(self.font,6.5)
            c.drawCentredString(x+cell_w/2,y+cell_h-25,
                _short_label(c,item['ticker'].replace('.KS','').replace('.KQ',''),self.font,6.5,cell_w-8))
            c.setFont(self.bold,10.3)
            value='미확인' if item['roi'] is None else f"{item['roi']:+.1f}%"
            c.drawCentredString(x+cell_w/2,y+max(10,cell_h*0.26),value)


class DistributionBars(Flowable):
    def __init__(self, counts, total, colors_map, font, bold, width, height=360, max_items=10):
        super().__init__()
        self.counts,self.total,self.colors_map=counts,total,colors_map
        self.font,self.bold,self.width,self.height=font,bold,width,height
        self.max_items=max_items
    def draw(self):
        c=self.canv
        ordered=sorted(((k,v) for k,v in self.counts.items() if v>0), key=lambda kv:-kv[1])
        shown=ordered[:self.max_items]
        if len(ordered)>self.max_items:
            shown.append(('기타 업종',sum(v for _,v in ordered[self.max_items:])))
        if not shown:
            c.setFillColor(MUTED);c.setFont(self.font,10)
            c.drawString(5,self.height-26,'평가 가능한 데이터가 없어 표시하지 않습니다.');return
        # Max width absolute share of the portfolio, rather than normalizing to leader.
        n=len(shown)
        row_h=min(31,(self.height-12)/n)
        label_w=98
        value_w=88
        bar_max=self.width-label_w-value_w-15
        for i,(name,val) in enumerate(shown):
            y=self.height-27-i*row_h
            c.setFillColor(DARK);c.setFont(self.font,9)
            c.drawString(2,y+3,_short_label(c,name,self.font,9,label_w-6))
            c.setFillColor(colors.HexColor('#EAF0EC'))
            c.roundRect(label_w,y-2,bar_max,14,3,fill=1,stroke=0)
            share=100*val/self.total if self.total>0 else 0
            if share>0:
                c.setFillColor(colors.HexColor(self.colors_map.get(name,'#89C8B1')))
                c.roundRect(label_w,y-2,bar_max*min(100,share)/100,14,3,fill=1,stroke=0)
            c.setFillColor(DARK);c.setFont(self.bold,9)
            c.drawRightString(self.width-3,y+2,f'{share:.1f}%  ·  {won(val)}')


class HeatLegend(Flowable):
    def __init__(self,font,width):
        super().__init__();self.font,self.width,self.height=font,width,33
    def draw(self):
        c=self.canv
        colors_info=[('#C76559','-20%↓'),('#E78D7D','-20~-10%'),('#F2BAAA','-10~0%'),('#EDF1EF','미확인'),('#A7DDCA','0~10%'),('#66BEA3','10~20%'),('#29977C','+20%↑')]
        col_w=self.width/len(colors_info)
        for i,(color,title) in enumerate(colors_info):
            x=i*col_w
            c.setFillColor(colors.HexColor(color));c.roundRect(x+1,17,14,11,2,fill=1,stroke=0)
            c.setFillColor(MUTED);c.setFont(self.font,6.6)
            c.drawString(x+17,19,title)


class SectorLegend(Flowable):
    def __init__(self, totals, cmap, font, width):
        super().__init__()
        self.totals,self.cmap,self.font,self.width=totals,cmap,font,width
        self.height=44
    def draw(self):
        c=self.canv
        top=sorted(self.totals,key=lambda k:-self.totals[k])[:10]
        col_w=self.width/5
        for i,k in enumerate(top):
            x=(i%5)*col_w;y=27-(i//5)*20
            c.setFillColor(colors.HexColor(self.cmap.get(k,'#D5E4DC')))
            c.roundRect(x,y,11,11,2,stroke=0,fill=1)
            c.setFillColor(MUTED);c.setFont(self.font,8.0)
            c.drawString(x+16,y+1,_short_label(c,k,self.font,8.0,col_w-20))


def build_pdf(df:pd.DataFrame, mode:str, fx_source:str, demo:bool=False,
              research_map:dict|None=None, news_map:dict|None=None, quote_map:dict|None=None)->bytes:
    research_map=research_map or {}
    news_map=news_map or {}
    quote_map=quote_map or {}
    font,bold=register_font()
    st=styles(font,bold)
    buff=BytesIO()
    doc=SimpleDocTemplate(buff,pagesize=A4,
        leftMargin=17*mm,rightMargin=17*mm,topMargin=18*mm,bottomMargin=17*mm,
        title='HanaV Private Portfolio | 포트폴리오 점검 리포트',
        author='HanaV Private Portfolio')
    W=A4[0]-34*mm
    flow=[]
    now=datetime.now().strftime('%Y.%m.%d %H:%M')
    def p(value,style='normal',limit=None):
        value=txt(value,'—')
        if limit and len(value)>limit:value=value[:limit-1]+'…'
        return Paragraph(escape(value),st[style])
    def title(s):
        flow.append(p(s,'section')); flow.append(Spacer(1,3*mm))
    def sectiontable(headers, rowcells, widths, aligns=None):
        data=[[p(h,'tablehead') for h in headers]]
        data.extend([[p(x,'tablecell') for x in row] for row in rowcells])
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        rules=[('BACKGROUND',(0,0),(-1,0),BASE),
               ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,PANEL]),
               ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
               ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
               ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),4),
               ('LINEBELOW',(0,0),(-1,0),0.4,BASE),
               ('LINEBELOW',(0,1),(-1,-2),0.22,LINE)]
        for i in (aligns or []):rules.append(('ALIGN',(i,1),(i,-1),'RIGHT'))
        t.setStyle(TableStyle(rules))
        return t

    m=portfolio_summary(df)
    sorted_df=df.sort_values('평가액(원)',ascending=False,na_position='last')
    tickers=list(dict.fromkeys(df['티커'].astype(str))) if '티커' in df else []
    n=len(tickers)
    coverprice=sum(number((research_map.get(t) or {}).get('quote',{}).get('price')) is not None or number((quote_map.get(t) or {}).get('price')) is not None for t in tickers)
    coverdeep=sum(has_fundamentals(research_map.get(t)) for t in tickers)
    covernews=sum(bool(news_map.get(t)) for t in tickers)

    # Page 1: decision-useful, legible executive dashboard.
    flow.append(p('HANAV  /  PRIVATE PORTFOLIO','brand'))
    flow.append(Spacer(1,2*mm))
    flow.append(p('포트폴리오 정기 점검 리포트','main'))
    flow.append(p(f'작성 {now}    |    평가가격 {mode}    |    환산 {fx_source}','micro'))
    flow.append(Spacer(1,4*mm))
    if demo:
        demo_tbl=Table([[p('가상 65종목 시연 자료 · 실제 고객 자산 및 시장 분석 결과가 아닙니다.','smallgreen')]],colWidths=[W])
        demo_tbl.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#FFF4DC')),
            ('BOX',(0,0),(-1,-1),0.5,colors.HexColor('#F0DCB2')),
            ('LEFTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        flow.append(demo_tbl);flow.append(Spacer(1,5*mm))

    metrics=[('보유 포지션',f"{m['positions']:,}개"),('고객 구분',f"{m['clients']:,}개"),
             ('평가 가능한 금액',won(m['value_krw'])),('평가 비중 최대',fmt(m['largest_weight'],1,'%'))]
    metric_tbl=Table([[Table([[p(name,'label')],[p(value,'number')]],colWidths=[W/4-12],style=TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),PALE),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
        ('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)])) for name,value in metrics]],
        colWidths=[W/4]*4)
    metric_tbl.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),PALE),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),12),('BOTTOMPADDING',(0,0),(-1,-1),11),
        ('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),3),
        ('INNERGRID',(0,0),(-1,-1),0.5,colors.white)]))
    flow.append(metric_tbl)
    flow.append(Spacer(1,4*mm))
    if not m['complete_total']:
        flow.append(p('주의: 일부 종목의 시세 또는 환율이 없어, 표시 금액과 비중은 확인 가능한 종목 기준입니다.','small'))
        flow.append(Spacer(1,2*mm))
    title('이번 보고서의 확인 사항')
    findings=risk_findings(df)
    for line in findings[:5]:
        flow.append(p('•  '+line,'body'));flow.append(Spacer(1,2*mm))
    flow.append(Spacer(1,4*mm))
    title('데이터 준비 상태')
    coverage=[('주가 추세',coverprice),('실적·밸류에이션',coverdeep),('최근 뉴스',covernews)]
    cover_cells=[]
    for label,num in coverage:
        coverage_pct=100*num/n if n else 0
        cover_cells.append(Table([[p(label,'label')],[p(f'{num}/{n}종목  ({coverage_pct:.0f}%)','number')]],
            colWidths=[W/3-12],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),PANEL),
            ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
            ('TOPPADDING',(0,0),(-1,-1),1),('BOTTOMPADDING',(0,0),(-1,-1),1)])))
    cov=Table([cover_cells],colWidths=[W/3]*3)
    cov.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),PANEL),
        ('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),
        ('LEFTPADDING',(0,0),(-1,-1),10),('INNERGRID',(0,0),(-1,-1),1,colors.white)]))
    flow.append(cov);flow.append(Spacer(1,3*mm))
    flow.append(p('PDF는 이미 조회된 자료만 사용합니다. 실적·목표가·뉴스가 비어 있다면 앱의 보고서 탭에서 해당 종목을 먼저 수집해야 합니다.','micro'))
    flow.append(Spacer(1,6*mm))
    title('보고서 해석 시 유의사항')
    flow.append(p('보유 수익률은 입력한 매입단가·기준가에 따른 계산값이며 실현손익, 배당금, 거래비용을 반영하지 않습니다. 뉴스는 사실 검증 및 종목 관련성 확인이 필요하며, 공개 집계 목표가는 개별 기관의 발표일을 확인하지 않은 값입니다.','body'))

    # V1.0.9: infographic-first report. No long holdings table.
    holdings, valued_total, industry_totals, customer_totals, sector_palette = _chart_data(
        df, research_map, quote_map
    )
    assessed_count = sum(x['priced']==x['positions'] for x in holdings)
    missing_count = sum(x['priced']<x['positions'] for x in holdings)
    chart_limit_note = ('분석 비중은 원화 평가금액이 확인된 종목만 기준입니다. 전체 자산의 구성비가 아닐 수 있습니다.'
                        if not m['complete_total'] else
                        '입력한 평가가격 기준이며 ETF 내부 구성종목 중복은 전개되지 않습니다.')

    flow.append(PageBreak())
    flow.append(p('01  /  ASSET MAP','brand'))
    flow.append(Spacer(1,2*mm))
    flow.append(p('보유비중 트리맵','main'))
    flow.append(p(f'전체 {len(df)}개 포지션 · 고유 종목 {len(holdings)}개 · 평가금액 기준 · 색상은 입력 업종', 'micro'))
    flow.append(Spacer(1,4*mm))
    flow.append(PortfolioTreemap(holdings,sector_palette,font,bold,W,height=485))
    flow.append(Spacer(1,4*mm))
    flow.append(p('주요 업종 범례','smallgreen'))
    flow.append(Spacer(1,2*mm))
    flow.append(SectorLegend(industry_totals,sector_palette,font,W))
    flow.append(Spacer(1,2*mm))
    flow.append(p(f'사각형 면적 = 원화 평가액 비중 · 동일 종목은 고객별 보유를 합산 · 평가 누락 종목 {missing_count}개', 'micro'))
    flow.append(p(chart_limit_note,'micro'))

    # Matrix uses constant-size tiles: small positions are as legible as large ones.
    # Split after 80 unique tickers to prevent tiny, unreadable labels.
    chunk_size = 80
    chunks = [holdings[i:i+chunk_size] for i in range(0,len(holdings),chunk_size)] or [[]]
    for page_idx,chunk in enumerate(chunks):
        flow.append(PageBreak())
        flow.append(p('02  /  PERFORMANCE MATRIX','brand'))
        flow.append(Spacer(1,2*mm))
        flow.append(p('전 종목 수익률 히트맵','main'))
        flow.append(p(f'전 종목 {len(holdings)}개 · {page_idx+1}/{len(chunks)}페이지 · 각 칸 크기는 동일 · 평가금액 내림차순', 'micro'))
        flow.append(Spacer(1,4*mm))
        flow.append(HeatLegend(font,W))
        flow.append(Spacer(1,1*mm))
        # 8 columns, max. 10 rows on one A4 page.
        flow.append(PortfolioHeatmap(chunk,font,bold,W,height=580,columns=8))
        flow.append(Spacer(1,3*mm))
        flow.append(p('색상 및 수치는 입력 매입단가 대비 평가수익률입니다. 배당·수수료·실현손익은 포함하지 않습니다.','micro'))
        flow.append(p('매입단가·평가액이 미확인된 종목은 회색으로 표시합니다. 고유 종목 기준이며 고객 간 합산 수익률을 사용합니다.','micro'))

    flow.append(PageBreak())
    flow.append(p('03  /  ASSET ALLOCATION','brand'))
    flow.append(Spacer(1,2*mm))
    flow.append(p('업종 · 고객별 자산 구성','main'))
    flow.append(p('전체 평가 가능 금액 기준. 각 막대 길이는 전체 평가자산 중 해당 항목 비율을 의미합니다.','micro'))
    flow.append(Spacer(1,5*mm))
    title('업종별 투자 비중')
    flow.append(DistributionBars(industry_totals,valued_total,sector_palette,font,bold,W,height=353,max_items=10))
    flow.append(Spacer(1,6*mm))
    title('고객별 투자 비중')
    # Larger family trees paginate automatically rather than silently omitting customers.
    top_clients=dict(sorted(customer_totals.items(),key=lambda x:-x[1])[:10])
    remaining=sum(v for k,v in customer_totals.items() if k not in top_clients)
    if remaining>0:top_clients['기타 고객']=remaining
    client_palette={k:SECTOR_HEX[i % len(SECTOR_HEX)] for i,k in enumerate(top_clients)}
    flow.append(DistributionBars(top_clients,valued_total,client_palette,font,bold,W,height=165,max_items=11))
    flow.append(Spacer(1,3*mm))
    flow.append(p(chart_limit_note,'micro'))
    flow.append(p('업종은 사용자가 입력한 분류이며 ETF의 기초 종목을 업종별로 재분류하지 않았습니다.','micro'))
    flow.append(PageBreak())

    def current_quote(res,quote):
        candidate=res.get('quote') or {}
        return candidate if number(candidate.get('price')) is not None else quote

    def market_lines(q):
        price=number(q.get('price'))
        if price is None:
            return ['공개 가격추세 미수집 · 입력 기준가와 구분하여 사용합니다.']
        ch=[f"공개 종가 {fmt(price,2)} · {txt(q.get('date'))} · 1개월 {pct(q.get('ret_1m'))} / 3개월 {pct(q.get('ret_3m'))}"]
        ma20=number(q.get('ma20')); ma60=number(q.get('ma60')); hi=number(q.get('high52'))
        detail=[]
        if ma20 is not None and ma60 is not None:
            detail.append(f"20일선 {fmt(ma20,2)} / 60일선 {fmt(ma60,2)}")
        if hi and hi>0:
            detail.append(f"52주 종가 최고 대비 {pct((price/hi-1)*100)}")
        if detail:ch.append(' · '.join(detail))
        return ch

    def fundamental_lines(res,is_etf):
        if is_etf:return ['ETF: 일반 기업 PER·ROE 대신 기초지수 및 구성종목 확인이 필요합니다.']
        info=res.get('info') or {}; fin=[]
        pe=number(info.get('trailingPE'));roe=number(info.get('returnOnEquity')); growth=number(info.get('revenueGrowth'))
        if pe is not None:fin.append(f'PER {pe:,.1f}배')
        if roe is not None:fin.append(f'ROE {roe*100:,.1f}%')
        if growth is not None:fin.append(f'매출 성장률 {growth*100:+.1f}%')
        result=[]
        if fin:result.append(' · '.join(fin))
        quarters=res.get('quarters') or []
        if quarters:
            q=quarters[0]
            vals=[]
            for key in ('매출액','영업이익','당기순이익'):
                amt=number(q.get(key))
                if amt is not None: vals.append(f'{key} {amt/1e8:,.1f}억')
            if vals:result.append(f"최근 분기 {txt(q.get('기간'))} · {' / '.join(vals)} ({txt(res.get('financial_currency'))})")
        return result or ['재무·분기 실적 미수집 (수집 버튼 실행 또는 공급원 확인 필요)']

    def target_line(r,res,is_etf):
        if is_etf:return 'ETF: 단일 기업 목표주가 비교 제외'
        target=number(r.get('목표가'))
        if target and target>0:
            return f"사용자 입력 목표가 {fmt(target,2)} {txt(r.get('통화'),'')} · {txt(r.get('목표가출처'),'출처 없음')} · {txt(r.get('목표가기준일'),'일자 없음')} (검증 필요)"
        targets=res.get('analyst_targets') or {}
        avg=number(targets.get('mean'))
        if avg is not None:
            return f"공개 집계 목표가 {fmt(avg,2)} {txt(res.get('price_currency'),'')} · 기관 및 발표일 미확인 (참고용)"
        return '출처·발표일이 확인된 목표주가 없음'

    def insights(r,q,res,is_etf):
        notes=[]
        wt=number(r.get('평가액비중(%)'));price=number(q.get('price'))
        ma20=number(q.get('ma20'));ma60=number(q.get('ma60'))
        hi=number(q.get('high52'))
        g=number((res.get('info') or {}).get('revenueGrowth'))
        if wt is not None and wt>=10:
            notes.append(f'단일 포지션 비중 {wt:.1f}%: 업종 및 ETF 중복 노출 점검')
        if price is not None and ma20 is not None and ma60 is not None:
            notes.append('단기 20일선이 60일선 아래: 추세 변화 점검' if ma20 < ma60 else '20일선이 60일선 이상: 추세 유지 여부 점검')
        if price is not None and hi and hi>0 and price/hi-1<=-0.25:
            notes.append('52주 종가 최고 대비 25% 이상 하락: 변동성 원인 확인')
        if not is_etf and g is not None and g<0:
            notes.append('매출 성장률 음수: 실적 둔화의 지속 여부 확인')
        if is_etf:notes.append('ETF 구성종목 중복 및 기초지수 변화 확인')
        if not notes:
            notes.append('최근 기업 이벤트와 비중 변화를 정기 확인')
        return notes[:2]

    def card(serial,r):
        symbol=txt(r.get('티커'),'')
        res=research_map.get(symbol) or {}
        if not isinstance(res,dict):res={}
        q=current_quote(res,quote_map.get(symbol) or {})
        info=res.get('info') or {}
        articles=news_map.get(symbol) or []
        is_etf='ETF' in str(r.get('종목유형','')).upper()
        label=txt(r.get('종목명'),txt(r.get('종목코드'),'종목'))
        code=txt(r.get('종목코드'),'')
        header=Table([[p(f'{serial:02d}    {label}  ({code})','cardtitle'),p(txt(r.get('고객구분')),'smallgreen')]],
                     colWidths=[W-34*mm,34*mm])
        header.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
            ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),1)]))
        metrics=[('평가금액',won(r.get('평가액(원)'))),
                 ('보유 비중',fmt(r.get('평가액비중(%)'),1,'%')),
                 ('보유 수익률',pct(r.get('수익률(%)')))]
        metrics_cells=[Table([[p(name,'label')],[p(val,'normal')]],colWidths=[(W-16*mm)/3 - 5],
                       style=TableStyle([('BACKGROUND',(0,0),(-1,-1),PALE),
                       ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0),
                       ('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
                       for name,val in metrics]
        metrics_table=Table([metrics_cells],colWidths=[(W-16*mm)/3]*3)
        metrics_table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),PALE),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),0),
            ('INNERGRID',(0,0),(-1,-1),1,colors.white)]))
        content=[]
        content.append(header)
        content.append(Spacer(1,2*mm));content.append(metrics_table)
        content.append(Spacer(1,3.5*mm))
        def labeled(title,lines):
            title_p=p(title,'smallgreen')
            parts=[title_p]
            for textline in lines[:2]:
                parts.append(p(textline,'small',limit=125))
            row=Table([[parts[0],parts[1:]]],colWidths=[25*mm,W-25*mm-16*mm],hAlign='LEFT')
            row.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),
                ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),3),
                ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),2)]))
            content.append(row)
        labeled('시장 · 추세',market_lines(q))
        labeled('실적 · 지표',fundamental_lines(res,is_etf))
        labeled('목표주가',[target_line(r,res,is_etf)])
        # News: title + original link in the PDF, with safe verified HTTPS only.
        if articles:
            a=articles[0]
            headline=txt(a.get('제목'),'제목 없음')[:74]
            article_date=txt(a.get('발행일'),'일자 미확인')
            article_source=txt(a.get('언론사'),'언론사 미확인')
            news_s=f'{headline} · {article_source} / {article_date}'
            labeled('주요 뉴스',[news_s])
            url=a.get('원문','')
            if valid_url(url):
                # Only a validated HTTPS link is embedded. Text escapes separately.
                safe_href=escape(str(url).strip(), {'"':'&quot;'})
                content.append(Paragraph('　　<a href="'+safe_href+'" color="#14664C">기사 원문 열기 ↗</a>',st['micro']))
        else:
            labeled('주요 뉴스',['최근 뉴스 미수집 또는 결과 없음'])
        labeled('관리 포인트',insights(r,q,res,is_etf))
        # Provenance: user-supplied valuation vs market quote must remain distinct.
        val_src=txt(r.get('가격출처'),'출처 없음')
        quote_src=txt(q.get('source'),'미수집') if number(q.get('price')) is not None else '미수집'
        meta=f'평가가격: {val_src} / 공개 가격추세: {quote_src} · 수집시점: {txt(q.get("date")) if number(q.get("price")) is not None else "—"}'
        content.append(Spacer(1,1.5*mm))
        content.append(p(meta,'micro',limit=130))
        shell=Table([[content]],colWidths=[W],hAlign='LEFT')
        shell.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.white),
            ('BOX',(0,0),(-1,-1),0.7,LINE),
            ('LINEBEFORE',(0,0),(0,-1),2.5,BASE),
            ('TOPPADDING',(0,0),(-1,-1),13),('BOTTOMPADDING',(0,0),(-1,-1),13),
            ('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9)]))
        return shell

    flow.append(p('전 종목 심층 점검 카드','main'))
    flow.append(p('상세 정보는 수집 성공 항목만 반영합니다. 미수집은 전망 부재를 의미하지 않습니다.','micro'))
    flow.append(Spacer(1,5*mm))
    for idx,(_,r) in enumerate(sorted_df.iterrows(),1):
        # 2 per page as a reading-first format, rather than eight dense paragraphs.
        if idx>1 and idx%2==1:
            flow.append(PageBreak())
            flow.append(p('종목별 심층 점검','section'))
            flow.append(Spacer(1,4*mm))
        flow.append(KeepTogether([card(idx,r)]))
        if idx%2==1 and idx<len(sorted_df):flow.append(Spacer(1,8*mm))

    flow.append(Spacer(1,5*mm))
    title('데이터 이용 및 준법 안내')
    flow.append(p('이 문서는 내부 검토를 위한 분석 초안입니다. 외부 공개 시세·뉴스 데이터의 라이선스와 재배포 권한, 고객정보 처리 및 전산 보안, 투자자문·리서치 제공 절차에 대한 회사 승인이 필요합니다. 계좌정보 및 고객의 투자내역을 승인되지 않은 공개 클라우드에 업로드하지 마세요.','body'))
    flow.append(Spacer(1,2*mm))
    flow.append(p('목표주가는 입력값 또는 데이터 제공사의 집계값으로 개별 리서치 보고서의 투자 의견을 대체하지 않습니다. 실제 거래 및 상담 전에 원천 출처·기준일·환율을 확인해야 합니다.','body'))

    def page(canvas,doc_ref):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(.55)
        canvas.line(17*mm,15*mm, A4[0]-17*mm, 15*mm)
        canvas.setFont(font,7)
        canvas.setFillColor(MUTED)
        canvas.drawString(17*mm,10.8*mm,'HanaV Private Portfolio  |  내부 검토용')
        if demo:
            canvas.drawCentredString(A4[0]/2,10.8*mm,'가상 시연 자료')
        canvas.drawRightString(A4[0]-17*mm,10.8*mm,f'{doc_ref.page}')
        canvas.restoreState()
    doc.build(flow,onFirstPage=page,onLaterPages=page)
    return buff.getvalue()
