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
            from pdf_report import build_pdf  # PDF 기능을 사용할 때만 의존성 로드
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
