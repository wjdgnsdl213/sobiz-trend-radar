# news-keyword-poc

소상공인 관련 뉴스에서 트렌드/지역/음식 키워드를 추출·분석하는 PoC(가능성 검증) 프로젝트.

## 두 엔진 구조

뉴스는 정책·사업환경 트렌드에는 1차 소스지만 소비 유행에는 후행 지표다. 그래서 목적이 다른 두 엔진으로 분리한다.

- **엔진 A — 정책/사업환경 트렌드** (뉴스): 임베딩 필터 + 형태소·KeyBERT 키워드. 최저임금·대출·상권 같은 사업환경 이슈에 강함
- **엔진 B — 소비 유행/신조어** (블로그): 통계 기반 신조어 추출(soynlp). "소금빵·빵지순례·겉바속촉"처럼 사전에 없는 유행어를 자력 발굴

## 아키텍처

두 엔진 모두 **수집 → 정제/추출 → 스코어링 → (검증/평가)** 흐름을 따르되, 소스 특성에 맞는 다른 기법을 쓴다.

```
                        네이버 오픈 API
        ┌──────────────────────┴──────────────────────┐
   뉴스 검색 API                              블로그 검색 API + DataLab
        │                                              │
┌───────┴──────── 엔진 A ────────┐        ┌──────────── 엔진 B ───────────┐
│                                │        │                               │
│  collect.py                    │        │  buzz_collect.py              │
│   └ 제목+요약, 링크 중복제거    │        │   └ 제목+요약, 링크 중복제거  │
│        ↓ data/raw/*.jsonl      │        │        ↓ data/raw_blog/*.jsonl│
│  filter.py                     │        │  buzz_extract.py              │
│   └ ko-sroberta 임베딩         │        │   └ soynlp 통계 단어추출      │
│     시드 centroid 코사인 컷    │        │     (사전 무의존, 응집도)     │
│     + near-duplicate 제거      │        │     kiwi 품사·신조어 판별     │
│        ↓ filtered.parquet      │        │     주 단위 급상승 스코어     │
│  extract.py                    │        │        ↓ buzz_candidates.csv  │
│   └ kiwi 명사 + KeyBERT 랭킹   │        │  buzz_validate.py             │
│        ↓ doc_keywords.parquet  │        │   └ DataLab 검색어트렌드로    │
│  trend.py                      │        │     후보 교차 검증(발굴→검증) │
│   └ 주 단위 급상승 스코어      │        │        ↓ buzz_validated.csv   │
│        ↓ trend_scores.csv      │        │                               │
│  evaluate.py                   │        └───────────────────────────────┘
│   └ 수동라벨 100건 precision/  │
│     recall + threshold 스윕    │
│        ↓ reports/poc_report.md │
└────────────────────────────────┘
```

- **공통 원칙**: 제목+요약문만 사용(본문 크롤링 금지), 모든 하이퍼파라미터는 `config.yaml`에서 관리, 각 단계는 독립 실행 가능하고 중간 산출물을 `data/`에 저장
- **이력 DB 계층(`db.py`)**: 양 엔진 산출물을 SQLite(`data/trends.db`)에 누적. raw는 링크 기준 최초 수집분 보존, 파생물은 실행일별 스냅샷 → 주간 반복 실행으로 트렌드 baseline이 시간에 따라 강화됨. 파이프라인 스크립트를 건드리지 않는 적재 계층
- **왜 두 기법인가**: 뉴스는 사전에 있는 정형 어휘라 임베딩 필터가 잘 맞지만, 소비 유행어는 사전에 없는 신조어라 형태소 분석이 놓친다 → soynlp 통계 추출로 우회하고 DataLab으로 실재 여부를 검증

## PoC가 증명해야 할 것

**엔진 A**
1. 무관한 뉴스(정치·스포츠·연예 등)를 **임베딩 유사도만으로** 자동 필터링할 수 있는가
2. 필터링된 뉴스에서 소상공인에게 의미 있는 **키워드와 급상승 트렌드**를 뽑을 수 있는가

**엔진 B**
3. 블로그에서 **형태소 사전에 의존하지 않고** 소비 유행어/신조어를 발굴할 수 있는가

## 성공 판정 기준 및 결과

| 엔진 | 항목 | 기준 | 결과 |
|---|---|---|---|
| A | 필터링 precision | 샘플 100건 대비 **85% 이상** | ✅ **92.1%** (recall 83.3%) |
| A | 키워드 품질 | 상위 20개 중 관련 **15개 이상** | ✅ **15개** |
| B | 급상승 신조어 상위 20 중 실제 유행어 | **5개 이상** | ✅ **10개 이상** |

상세 리포트: 엔진 A는 [`reports/poc_report.md`](reports/poc_report.md), 엔진 B는 [`reports/buzz_report.md`](reports/buzz_report.md).

## 파이프라인

**엔진 A (뉴스):**
```
collect → filter → extract → trend → evaluate
```

| 단계 | 실행 | 설명 |
|---|---|---|
| 수집 | `python -m src.collect` | 네이버 뉴스 검색 API, 최근 4주, 제목+요약문만 |
| 필터링 | `python -m src.filter` | 시드 centroid 코사인 유사도 컷 + near-duplicate 제거 |
| 키워드 추출 | `python -m src.extract` | kiwipiepy 명사 추출 + KeyBERT 랭킹 |
| 트렌드 | `python -m src.trend` | 주 단위 급상승 스코어: 이번 주 빈도 / (이전 3주 평균 + 1) |
| 평가 | `python -m src.evaluate` | 수동 라벨 100건 대비 precision/recall (threshold 스윕 포함) |

`python -m src.filter --tune` 로 threshold 튜닝 리포트를, `python -m src.evaluate --make-labels` 로 라벨링 시트를 만든다.

**엔진 B (블로그):**
```
buzz_collect → buzz_extract → buzz_validate
```

| 단계 | 실행 | 설명 |
|---|---|---|
| 수집 | `python -m src.buzz_collect` | 네이버 블로그 검색 API, 최근 4주, 제목+요약문만 |
| 신조어 발굴 | `python -m src.buzz_extract` | soynlp 통계 추출 + kiwipiepy 신조어 판별 + 급상승 스코어 |
| DataLab 검증 | `python -m src.buzz_validate` | 네이버 DataLab 검색어트렌드로 발굴 후보 교차 검증 |

**이력 DB (양 엔진 공통):**
```
db (적재 계층)
```

| 실행 | 설명 |
|---|---|
| `python -m src.db --ingest` | 최신 산출물을 SQLite(`data/trends.db`)에 누적 적재 |
| `python -m src.db --summary` | 누적 현황(원본 건수·수집일 범위·스냅샷 수) 출력 |

주간 반복 실행 시 raw 기사/포스트가 링크 기준으로 누적(최초 수집분 보존)되어, 네이버 API의 1,000건 상한·날짜범위 지정 불가 한계를 시간이 해결한다. 필터·트렌드·버즈 파생물은 실행일별 스냅샷으로 쌓여 대시보드 소스가 된다.

**전체 배치 (오케스트레이터):**

수집→처리→적재를 한 번에 실행한다. 각 단계는 기존 독립 모듈을 subprocess로 호출하므로 단계별 독립성은 유지된다(evaluate는 수동 라벨이 필요한 QA라 배치 제외).

```
python -m src.run_pipeline              # 엔진 A + B 전체 + DB 적재
python -m src.run_pipeline --only-a     # 엔진 A만
python -m src.run_pipeline --skip-collect  # 수집 건너뛰고 재처리
```

### 주간 자동 실행 (Windows 작업 스케줄러)

`run_weekly.bat`이 파이프라인을 실행하고 `logs/pipeline.log`에 로그를 남긴다. 주 1회 등록 예시(매주 월요일 04:00):

```powershell
schtasks /Create /SC WEEKLY /D MON /ST 04:00 /TN "sobiz-trend-radar-weekly" ^
  /TR "\"C:\Users\wjdgn\OneDrive\바탕 화면\sojingong\news_keyword\run_weekly.bat\""
```

등록 확인 `schtasks /Query /TN "sobiz-trend-radar-weekly"`, 즉시 테스트 `schtasks /Run /TN "sobiz-trend-radar-weekly"`, 삭제 `schtasks /Delete /TN "sobiz-trend-radar-weekly" /F`.

> 가상환경(.venv)을 쓰면 `run_weekly.bat`의 `python`을 `.venv\Scripts\python.exe`로 교체한다. 주 1회는 트렌드 스코어가 주 단위라 baseline 축적과 궁합이 맞고 API 부하도 낮다.

## 설치 및 실행

```bash
# 1. 가상환경 생성 (Python 3.11+)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

# 2. 의존성 설치
pip install -r requirements.txt

# 3. API 키 설정
copy .env.example .env        # Windows (macOS/Linux: cp)
# .env 파일에 네이버 개발자센터에서 발급받은 키 입력

# 4. 수집 실행
python -m src.collect
```

하이퍼파라미터(검색 쿼리, threshold, 불용어, 기간 등)는 모두 `config.yaml`에서 관리한다.

## 프로젝트 구조

```
├── CLAUDE.md
├── README.md
├── .env.example               # NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
├── config.yaml                # 하이퍼파라미터 (db/collect/filter/extract/trend/evaluate/buzz/buzz_validate)
├── requirements.txt
├── data/
│   ├── raw/                   # 뉴스 수집 원본 (jsonl)
│   ├── raw_blog/              # 블로그 수집 원본 (jsonl)
│   ├── processed/             # 필터링·키워드·트렌드·신조어 산출물
│   ├── labels/                # 수동 라벨 100건 (csv)
│   └── trends.db              # 이력 DB (SQLite, 주간 누적 — gitignore)
├── src/
│   ├── collect.py             # [A] 네이버 뉴스 검색 API 수집
│   ├── filter.py              # [A] 임베딩 관련성 필터 + near-duplicate 제거
│   ├── extract.py             # [A] kiwipiepy + KeyBERT 키워드 추출
│   ├── trend.py               # [A] 주 단위 급상승 스코어
│   ├── evaluate.py            # [A] precision/recall 평가, threshold 스윕, 리포트 생성
│   ├── buzz_collect.py        # [B] 네이버 블로그 검색 API 수집
│   ├── buzz_extract.py        # [B] soynlp 신조어 추출 + kiwi 판별 + 급상승 스코어
│   ├── buzz_validate.py       # [B] DataLab 검색어트렌드 API로 발굴 후보 교차 검증
│   ├── db.py                  # [공통] 산출물을 SQLite 이력 DB에 누적 적재
│   └── run_pipeline.py        # [공통] 수집→처리→적재 전체 배치 오케스트레이터
├── run_weekly.bat             # 주간 자동 실행용 배치 (작업 스케줄러 등록)
├── seeds/
│   └── seed_sentences.txt     # [A] 소상공인 사업환경 시드 문장 20개
└── reports/
    ├── poc_report.md          # [A] 엔진 A 결과 리포트 (자동 생성)
    └── buzz_report.md         # [B] 엔진 B 결과 리포트
```

## 알려진 한계

- **엔진 A 신조어 미인식**: kiwipiepy 사전 한계로 뉴스+형태소 조합은 최신 신조어를 놓친다. → 소비 유행어는 엔진 B(블로그+통계 추출)가 담당한다.
- **엔진 B 광고/메타 노이즈**: 블로그는 체험단·협찬·업소정보("서이추환영", "운영시간") 노이즈가 많다. 불용어·품사 필터로 상당 부분 제거하나 상업적 편향은 남는다. 신조어 플래그는 참고용이며 최종 선별은 수동 검토가 필요하다.
- **제목+요약문만 사용**: 저작권 리스크로 본문 크롤링을 하지 않으므로, 본문에만 등장하는 키워드는 잡히지 않는다.
- **네이버 API 1,000건 상한**: 쿼리당 최대 1,000건까지만 페이징 가능하고 날짜 범위 지정이 불가능하다. 기사량이 많은 쿼리는 1,000건이 최근 1~2일치로 소진되어 트렌드 스코어가 대상 주로 편중된다. 과거 데이터가 필요하면 빅카인즈 등 대체 소스 검토가 필요하다(향후 확장 참고).

## 향후 확장 (PoC 범위 밖 — 구현하지 않음)

- 대시보드/웹 UI 시각화
- 실시간 수집 스케줄러
- 분류기 fine-tuning (임베딩 필터 대체/보강)
- NER 기반 지역×음식 크로스 분석
- DB 연동 및 이력 관리
- 수집 소스 다변화: 빅카인즈 API(날짜 범위 지정·과거 데이터 확보 가능, 이용 신청 필요), 언론사 RSS 상시 수집(스케줄러 필요, 과거분 백필 불가)
- 광고·체험단 포스팅 분류기로 엔진 B 상업적 편향 제거
