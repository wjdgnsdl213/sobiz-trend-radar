# sobiz-trend-radar

소상공인 트렌드 레이더. 두 엔진으로 (A) 뉴스 기반 정책·사업환경 트렌드, (B) 블로그 기반 소비 유행/신조어를 잡는다.
PoC 검증을 마치고(엔진 A precision 92.1%, 엔진 B 신조어 10개+ 발굴) 현재는 **운영 도구화 단계**다.

## 단계 (로드맵)
1. ✅ **DB 연동 + 이력 관리**: `src/db.py`, 주간 수집분 누적으로 트렌드 baseline 강화
2. ✅ **수집 스케줄러**: `src/run_pipeline.py` 배치 + `run_weekly.bat` + Windows 작업 스케줄러(주 1회)
3. ✅ **대시보드(Streamlit)**: `src/dashboard.py`, 누적 DB를 읽어 급상승 키워드·신조어 시각화
4. ← NER 기반 지역×음식 크로스 분석
5. (필요 시) 분류기 fine-tuning — 임베딩 필터가 이미 목표 초과라 우선순위 최하

## 원칙
- 모든 하이퍼파라미터는 config.yaml에서 관리, 코드에 하드코딩 금지
- 데이터는 제목+요약문만 사용, 본문 크롤링 금지 (저작권)
- 단계별 스크립트는 독립 실행 가능해야 하며, 중간 산출물은 data/에 저장. DB는 산출물을 읽어 누적하는 적재 계층으로, 파이프라인 스크립트를 침범하지 않는다
- PoC 성공 기준(달성): 필터 precision ≥ 85%, 상위 20개 키워드 중 관련 ≥ 15개

## 실행 순서
- 엔진 A (뉴스, 정책·사업환경 트렌드): collect → filter → extract → trend → evaluate
- 엔진 B (블로그, 소비 유행/신조어): buzz_collect → buzz_extract → buzz_validate(DataLab 교차 검증)
- 적재: db (양 엔진 산출물을 SQLite 이력 DB에 누적)
- 전체 배치: run_pipeline (수집→처리→적재를 subprocess로 순차 실행, evaluate는 수동 QA라 제외)
- 두 엔진은 목적·소스가 다르다. 뉴스는 유행의 후행 지표라 소비 신조어는 엔진 B(soynlp 통계 추출)가 담당한다.

## 스타일
- 함수에 타입 힌트, 핵심 로직에 한국어 주석
- 외부 API 호출은 재시도 + rate limit 준수 (네이버 API: 초당 10회 제한)
