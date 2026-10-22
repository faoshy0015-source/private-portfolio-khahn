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

st.set_page_config(page_title='HanaV Private Portfolio',page_icon='💠',layout='wide',initial_sidebar_state='expanded')
st.markdown('''<style>
:root{--hana:#00b779;--hana-soft:#daf5ea;--dark:#101b19;--line:#29463b}
[data-testid="stAppViewContainer"]{background:linear-gradient(115deg,#081711 0%,#0E1F1A 45%,#111D1C 100%);color:#E5F5ED}
[data-testid="stSidebar"]{background:#0b1d16}
.block-container{max-width:1480px;padding-top:1.2rem}
h1,h2,h3{letter-spacing:-.04rem}h1{color:#F4FFFA}
.kicker{font-size:12px;letter-spacing:.16em;color:#5AD1A0;font-weight:800}
.hero{background:linear-gradient(110deg,#0C3324,#122B26 63%,#10211D);border:1px solid #2D634B;border-left:5px solid #0DC489;border-radius:16px;padding:19px 22px;margin:0 0 19px}
.hero h1{font-size:2.1rem;margin:.15rem 0 .4rem;line-height:1.18}
.hero p{color:#A6BFB3;margin:0;font-size:14px}
.info{background:#102A20;border:1px solid #2A5C49;border-radius:12px;padding:14px 17px}
.stButton>button[kind="primary"]{background:#06986D!important;border:1px solid #45DFAD!important;color:#fff!important}
small{color:#AAC7B7} [data-testid="stMetric"]{background:#112D23;border:1px solid #245540;padding:13px;border-radius:13px}
[data-testid="stMetricLabel"]{color:#A4C4B4}[data-testid="stMetricValue"]{color:#E9FFF3}
.stTabs [data-baseweb="tab"]{font-weight:650} .stAlert{border-radius:10px}
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
st.session_state.setdefault('upload_rev',0)
st.session_state.setdefault('editor_rev',0)
st.session_state.setdefault('quotes',{})
st.session_state.setdefault('news_map',{})
st.session_state.setdefault('fx_val',1350.0)  # 가상 65종목 시연 전용; 실제 업로드 시 0으로 초기화
st.session_state.setdefault('fx_source','가상 환율' if st.session_state['is_demo'] else '입력 없음')

with st.sidebar:
    st.markdown('## 💠 HanaV Private')
    st.caption('Portfolio Intelligence | V1.0.2')
    if st.button('로그아웃',use_container_width=True):
        # Completely clear current browser session's customer-position data.
        st.session_state.clear()
        st.rerun()
    st.divider()
    st.markdown('#### 1. 보유현황 불러오기')
    uploaded=st.file_uploader('고객별 보유종목 파일',type=['xlsx','csv'],key=f"input_upload_{st.session_state['upload_rev']}",help='계좌번호/주민등록번호 없이 고객별칭, 종목코드, 수량을 입력하세요.')
    if uploaded is not None:
        b=uploaded.getvalue();fingerprint=hashlib.sha256(b).hexdigest()
        if fingerprint!=st.session_state['uploaded_hash']:
            try:
                if uploaded.name.lower().endswith('.csv'):
                    try:d=pd.read_csv(BytesIO(b),dtype={'종목코드':str})
                    except UnicodeDecodeError:d=pd.read_csv(BytesIO(b),encoding='cp949',dtype={'종목코드':str})
                else:d=pd.read_excel(BytesIO(b),sheet_name=0,dtype={'종목코드':str})
                normalized,_=normalize_portfolio(d)
                st.session_state['portfolio_raw']=normalized[COLUMNS].copy()
                st.session_state['is_demo']=False
                st.session_state['uploaded_hash']=fingerprint
                st.session_state['quotes']={}
                st.session_state['editor_rev']+=1
                st.session_state['quote_errors']=[]
                st.session_state['news_map']={}
                st.session_state['fx_val']=0.0
                st.session_state['fx_source']='입력 없음'
                st.session_state.pop('pdf_bytes',None)
                st.success(f'{len(normalized)}개 포지션을 불러왔습니다.')
            except Exception as e:st.error(f'파일을 읽을 수 없습니다: {e}')
    st.caption('파일은 현재 접속 세션에서만 보유하며 별도 DB에 자동 저장하지 않습니다.')
    if st.button('가상 65종목 데모 다시 열기',use_container_width=True):
        st.session_state['portfolio_raw']=load_demo()
        st.session_state['is_demo']=True
        st.session_state['uploaded_hash']=''
        st.session_state['upload_rev']+=1
        st.session_state['editor_rev']+=1
        st.session_state['quote_errors']=[]
        st.session_state['quotes']={}
        st.session_state['news_map']={}
        st.session_state['fx_val']=1350.0
        st.session_state['fx_source']='가상 환율'
        st.session_state.pop('pdf_bytes',None)
        st.rerun()
    st.divider()
    st.markdown('#### 2. 시세 / 환율')
    price_mode=st.radio('가격 기준',['엑셀 입력 기준가','Yahoo 공개 종가 조회'],horizontal=False,help='Yahoo는 지연 시세 및 접근제한이 있을 수 있습니다.')
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
    st.caption('기준가와 통화가 일치해야 합니다. 해외주식은 USD, 국내주식은 KRW.')
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

names=['전체 포트폴리오']+sorted(normalized['고객구분'].unique().tolist())
left,right=st.columns([1.6,4])
with left:scope=st.selectbox('분석 범위',names)
with right:
    st.caption(f"분석대상 {len(normalized)}개 포지션 · 고객구분 {normalized['고객구분'].nunique()}개 · 자동저장 없음")
sub=normalized if scope==names[0] else normalized[normalized['고객구분']==scope].copy()
use_quotes=st.session_state['quotes'] if price_mode=='Yahoo 공개 종가 조회' else {}
fx=fx_input if fx_input>0 else None
positions=enrich_positions(sub,use_quotes,fx)
summary=portfolio_summary(positions)
warns=warnings[:]
if price_mode=='엑셀 입력 기준가':warns.append('가격은 입력 기준가입니다. 입력일이 확인되지 않으므로 실시간 가격으로 간주하면 안 됩니다.')
for w in warns[:5]:st.caption('ⓘ '+w)
if price_mode=='Yahoo 공개 종가 조회' and st.session_state.get('quotes'):
    backup=positions['가격출처'].eq('파일 입력 기준가').sum()
    if backup:
        st.warning(f'{backup}개 종목은 온라인 시세가 없어 엑셀 입력 기준가를 사용 중입니다. 날짜가 섞인 평가액이므로 재확인해 주세요.')


tab_dashboard,tab_holdings,tab_research,tab_news,tab_report=st.tabs(['📊 대시보드','📋 보유종목 관리','🔎 종목별 심층분석','📰 뉴스 모니터링','📑 보고서'])

with tab_dashboard:
    c1,c2,c3,c4=st.columns(4)
    c1.metric('보유 포지션',f"{summary['positions']:,}개")
    value_string=(f"{summary['value_krw']/1e8:,.2f}억원" if pd.notna(summary['value_krw']) else '평가 불가')
    c2.metric('평가 가능한 자산 합계',value_string,help='일부 가격·환율 누락 시 전체 총액이 아닙니다.')
    c3.metric('가격/환율 미반영',f"{summary['unconverted']}개")
    c4.metric('최대 보유비중',f"{summary['largest_weight']:.1f}%" if pd.notna(summary['largest_weight']) else '—')
    if not summary['complete_total']:st.error('**불완전한 평가금액**: 미조회 포지션이 있으므로 위 합계와 비중은 일부 자산 기준입니다.')
    g1,g2=st.columns([1.25,1])
    with g1:
        st.subheader('평가액 상위 15개')
        chart_df=positions.dropna(subset=['평가액(원)']).nlargest(15,'평가액(원)').copy()
        if not chart_df.empty:
            chart_df['표기']=chart_df.apply(lambda r:(r['종목명'] or r['종목코드'])[:20],axis=1)
            fig=px.bar(chart_df.sort_values('평가액(원)'),x='평가액(원)',y='표기',orientation='h',color_discrete_sequence=['#13B985'],height=425)
            fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font_color='#DDF6EA',margin=dict(l=0,r=8,t=5,b=5),yaxis_title='',xaxis_title='KRW')
            st.plotly_chart(fig,use_container_width=True)
        else:st.info('평가액을 표시할 수 있는 기준가가 없습니다.')
    with g2:
        st.subheader('자산 분포')
        sector_df=positions.dropna(subset=['평가액(원)']).copy()
        sector_df['업종']=sector_df['업종'].replace('', '미분류')
        if not sector_df.empty:
            sums=sector_df.groupby('업종')['평가액(원)'].sum().reset_index()
            fig=px.pie(sums,names='업종',values='평가액(원)',hole=.58,color_discrete_sequence=['#0EB780','#54D7B8','#337B6E','#C2A777','#7EC4D5','#397F67','#ADDDC7'])
            fig.update_layout(paper_bgcolor='rgba(0,0,0,0)',font_color='#DDF6EA',height=425,margin=dict(t=8,l=8,b=8,r=8))
            st.plotly_chart(fig,use_container_width=True)
        else:st.info('업종분포 계산에 필요한 평가액이 없습니다.')
    st.subheader('VVIP 모니터링 포인트')
    for msg in risk_findings(positions):st.markdown('• '+msg)
    st.caption('업종 분류는 입력된 데이터 기준이며 ETF 기초자산 중복은 V1에서 미계산합니다. 투자권고·매매신호가 아닙니다.')

with tab_holdings:
    st.subheader('보유종목 입력 · 수정')
    st.caption('아래 표는 원본 전체 고객군의 데이터입니다. 편집 후 자동으로 재분석됩니다. 계좌번호와 실명은 넣지 마세요.')
    with st.expander('엑셀 양식 안내 / 입력예시',expanded=False):
        st.write('필수: 종목코드, 보유수량. 권장: 고객구분, 종목명, 시장(KOSPI/KOSDAQ/US), 통화(KRW/USD), 매입단가, 기준가, 업종, 종목유형(주식/ETF). 목표가는 출처와 기준일을 함께 입력하세요.')
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
    st.subheader('현재 분석 결과 전체표')
    display=['고객구분','종목코드','종목명','통화','보유수량','매입단가','현재가','가격기준일','평가액(원)','평가액비중(%)','수익률(%)','모니터링']
    st.dataframe(positions[display],use_container_width=True,height=470,hide_index=True,
                 column_config={'평가액(원)':st.column_config.NumberColumn(format='%,.0f'),'평가액비중(%)':st.column_config.NumberColumn(format='%.2f%%'),'수익률(%)':st.column_config.NumberColumn(format='%+.2f%%')})
    st.download_button('⬇ 분석 결과 CSV 다운로드',data=to_csv_bytes(positions.drop(columns=['환산율'])),file_name=f'hanav_portfolio_{datetime.now():%Y%m%d}.csv',mime='text/csv')

with tab_research:
    st.subheader('종목별 심층 분석')
    pos_options=[f"{i} | {r['고객구분']} · {str(r['종목명']) or r['종목코드']} ({r['종목코드']})" for i,r in positions.iterrows()]
    picked=st.selectbox('분석할 종목 선택',list(range(len(pos_options))),format_func=lambda i:pos_options[i])
    row=positions.iloc[picked]
    met=st.columns(4)
    met[0].metric('현재가 / 입력 기준가',f"{row['현재가']:,.2f} {row['통화']}" if pd.notna(row['현재가']) else '가격 없음')
    met[1].metric('보유 수익률',f"{row['수익률(%)']:+.2f}%" if pd.notna(row['수익률(%)']) else '단가 미입력')
    met[2].metric('포트폴리오 비중',f"{row['평가액비중(%)']:.2f}%" if pd.notna(row['평가액비중(%)']) else '산출 불가')
    met[3].metric('52주 종가 고점 대비',f"{row['52주고점대비(%)']:+.1f}%" if pd.notna(row['52주고점대비(%)']) else '이력 없음')
    d1,d2=st.columns([1.5,1])
    with d1:
        st.markdown('#### 1년간 가격 추이')
        q=use_quotes.get(row['티커'],{})
        hist=q.get('history',pd.Series(dtype=float))
        if isinstance(hist,pd.Series) and not hist.empty:
            fig=go.Figure()
            fig.add_trace(go.Scatter(x=hist.index,y=hist.values,mode='lines',name='종가',line=dict(color='#13C58C',width=2)))
            for days,name,color in [(20,'MA20','#EDC878'),(60,'MA60','#85B5FB'),(120,'MA120','#B79AEF')]:
                if len(hist)>=days:fig.add_trace(go.Scatter(x=hist.index,y=hist.rolling(days).mean(),name=name,line=dict(color=color,width=1.2)))
            fig.update_layout(height=420,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='rgba(0,0,0,0)',font_color='#DDF6EA',margin=dict(l=0,r=0,t=0,b=0))
            st.plotly_chart(fig,use_container_width=True)
        else:st.info('시세 갱신을 누르면 공개 데이터가 제공되는 종목은 이동평균 차트를 확인할 수 있습니다.')
    with d2:
        st.markdown('#### 모니터링 의견')
        for note in build_individual_note(row):st.markdown('• '+note)
        st.caption(f"가격출처: {row['가격출처'] or '없음'} · 기준: {row['가격기준일'] or '없음'}")
    st.markdown('#### 투자판단을 위한 검증자료')
    if pd.notna(row['목표가']) and row['목표가']>0:
        st.info(f"입력 목표가: {row['목표가']:,.2f} {row['통화']} / 출처: {row['목표가출처'] or '미입력'} / 발표일: {row['목표가기준일'] or '미입력'}")
    else:
        st.info('공식 목표주가·컨센서스는 **검증한 자료만** 입력하세요. V1은 외부 목표주가 자동수집을 하지 않습니다.')
    if st.button('이 종목 최신 뉴스 조회',key='selected_news'):
        items,msg=cached_news(str(row['티커']),str(row['종목명']))
        st.session_state['news_map'][str(row['티커'])]=items
        st.session_state['news_msg']=msg
    for article in st.session_state['news_map'].get(str(row['티커']),[]):
        st.markdown(f"**[{article['제목']}]({article['원문']})**")
        st.caption(f"{article['언론사']} · {article['발행일']}")
    if str(row['티커']) in st.session_state['news_map'] and not st.session_state['news_map'][str(row['티커'])]:
        st.warning(st.session_state.get('news_msg','조회 결과 없음'))
    st.caption('뉴스는 Google News RSS의 공개 검색결과입니다. 동일명 타기업 등 오탐이 가능하며 원문 확인이 필요합니다.')

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
    st.write('요약·위험 모니터링·전체 보유종목·상위 10개 종목의 근거 중심 분석을 PDF로 작성합니다.')
    st.caption('매매 추천, AI가 생성한 허구의 목표가, 근거 없는 전망은 포함하지 않습니다. 고객 대외 전달 전 내부 승인 필요.')
    if st.button('📄 PDF 보고서 생성',type='primary'):
        try:
            from pdf_report import build_pdf  # PDF 기능을 사용할 때만 의존성 로드
            st.session_state['pdf_bytes']=build_pdf(positions,price_mode,fx_source,st.session_state['is_demo'])
            st.session_state['pdf_scope']=scope
            st.success('보고서가 생성됐습니다.')
        except ModuleNotFoundError as e:
            if e.name=='reportlab':
                st.error(r'PDF 생성에 필요한 reportlab 패키지가 설치되지 않았습니다. 앱 폴더에서 .\.venv\Scripts\python.exe -m pip install reportlab 을 실행하세요.')
            else:st.error(f'PDF 모듈 오류: {e}')
        except Exception as e:st.error(f'PDF 생성 오류: {e}')
    if 'pdf_bytes' in st.session_state:
        st.download_button('⬇ PDF 다운로드',data=st.session_state['pdf_bytes'],file_name=f'hanav_private_{datetime.now():%Y%m%d}.pdf',mime='application/pdf',use_container_width=True)
        st.caption(f"생성된 범위: {st.session_state.get('pdf_scope','')} · 범위/자료 수정 시 PDF를 다시 생성하세요.")
    st.download_button('⬇ 결과 데이터 CSV 다운로드',data=to_csv_bytes(positions),file_name=f'hanav_detail_{datetime.now():%Y%m%d}.csv',mime='text/csv')
    st.divider()
    st.markdown('#### 사내 검토 전 필수 확인')
    st.markdown('• 데이터 제공사의 라이선스 및 대외 재배포 가능 여부  \n• 고객정보 처리 및 전산 보안 승인 여부  \n• 목표가·증권사 의견 인용의 적법성 및 근거 출처  \n• 고객 제공자료의 준법 검토 및 버전 이력 관리')

st.divider()
st.caption('HanaV Private Portfolio V1.0.2 · 기존 HanaV Trading과 별개인 내부 시험용 도구 · 주문/자동매매/개인정보 서버저장 미지원')
