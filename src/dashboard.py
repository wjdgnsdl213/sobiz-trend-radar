"""대시보드: 이력 DB(data/trends.db)를 읽어 두 엔진 트렌드를 시각화한다.

실행: python -m streamlit run src/dashboard.py

- 개요: 누적 원본·스냅샷 현황
- 엔진 A: 실행일별 급상승 키워드(정책·사업환경)
- 엔진 B: 실행일별 신조어/유행 + DataLab 검증 결과
- 시계열: 실행 회차가 2회 이상 쌓이면 키워드별 추이

데이터가 없으면 python -m src.run_pipeline 안내만 표시한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

st.set_page_config(page_title="Sobiz Trend Radar", page_icon="📡", layout="wide")


@st.cache_data
def get_db_path() -> str:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f).get("db", {}).get("path", "data/trends.db")


@st.cache_data(ttl=300)
def query(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    """DB 조회 결과를 DataFrame으로. 테이블이 없거나 오류면 빈 DataFrame."""
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def run_dates(db_path: str, table: str) -> list[str]:
    df = query(db_path, f"SELECT DISTINCT run_date FROM {table} ORDER BY run_date DESC")
    return df["run_date"].tolist() if not df.empty else []


def overview(db_path: str) -> None:
    st.subheader("📊 누적 현황")
    news = query(db_path, "SELECT COUNT(*) n, MIN(first_seen) a, MAX(first_seen) b FROM news_articles")
    blog = query(db_path, "SELECT COUNT(*) n, MIN(first_seen) a, MAX(first_seen) b FROM blog_posts")
    runs = len(run_dates(db_path, "trend_snapshot"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("뉴스 원본 누적", f"{int(news['n'][0]):,}" if not news.empty else "0")
    c2.metric("블로그 원본 누적", f"{int(blog['n'][0]):,}" if not blog.empty else "0")
    c3.metric("파이프라인 실행", f"{runs}회")
    span = f"{news['a'][0]} ~ {news['b'][0]}" if not news.empty and news["a"][0] else "-"
    c4.metric("뉴스 수집일 범위", span)


def engine_a(db_path: str, run_date: str) -> None:
    st.subheader(f"📰 엔진 A — 정책·사업환경 급상승 키워드  ({run_date})")
    df = query(
        db_path,
        "SELECT keyword, this_week_freq, baseline_avg, trend_score "
        "FROM trend_snapshot WHERE run_date = ? ORDER BY trend_score DESC",
        (run_date,),
    )
    if df.empty:
        st.info("이 실행일의 트렌드 스냅샷이 없습니다.")
        return

    top_n = st.slider("표시 개수", 5, 40, 20, key="a_topn")
    top = df.head(top_n)
    st.bar_chart(top.set_index("keyword")["trend_score"], height=400)
    st.dataframe(
        df.rename(columns={
            "keyword": "키워드", "this_week_freq": "이번주 빈도",
            "baseline_avg": "직전 평균", "trend_score": "급상승 스코어",
        }),
        use_container_width=True, hide_index=True,
    )


def engine_b(db_path: str, run_date: str) -> None:
    st.subheader(f"🍞 엔진 B — 소비 유행/신조어  ({run_date})")
    df = query(
        db_path,
        "SELECT candidate, total_freq, cohesion, is_neologism, trend_score, "
        "datalab_max_ratio, datalab_confirmed FROM buzz_snapshot "
        "WHERE run_date = ? ORDER BY trend_score DESC",
        (run_date,),
    )
    if df.empty:
        st.info("이 실행일의 버즈 스냅샷이 없습니다.")
        return

    c1, c2 = st.columns(2)
    only_neo = c1.checkbox("🆕 신조어만 (사전 미등재)", value=False)
    only_confirmed = c2.checkbox("✅ DataLab 확인만", value=False)
    view = df.copy()
    if only_neo:
        view = view[view["is_neologism"] == 1]
    if only_confirmed:
        view = view[view["datalab_confirmed"] == 1]

    if view.empty:
        st.info("조건에 맞는 후보가 없습니다.")
        return

    top_n = st.slider("표시 개수", 5, 40, 20, key="b_topn")
    top = view.head(top_n)
    st.bar_chart(top.set_index("candidate")["trend_score"], height=400)

    show = view.assign(
        신조어=view["is_neologism"].map({1: "🆕", 0: ""}),
        DataLab확인=view["datalab_confirmed"].map({1: "✅", 0: ""}),
    ).rename(columns={
        "candidate": "후보", "total_freq": "빈도", "cohesion": "응집도",
        "trend_score": "급상승 스코어", "datalab_max_ratio": "DataLab 최고지수",
    })[["후보", "빈도", "응집도", "신조어", "급상승 스코어", "DataLab 최고지수", "DataLab확인"]]
    st.dataframe(show, use_container_width=True, hide_index=True)


def cross_region(db_path: str, run_date: str) -> None:
    st.subheader(f"🗺️ 지역 × 음식 크로스  ({run_date})")
    df = query(
        db_path,
        "SELECT region, term, cooccur, region_total, share_in_region "
        "FROM cross_region WHERE run_date = ? ORDER BY cooccur DESC",
        (run_date,),
    )
    if df.empty:
        st.info("이 실행일의 크로스 스냅샷이 없습니다. `python -m src.cross_region` 실행 후 DB 적재가 필요합니다.")
        return

    st.caption("어느 상권에서 어떤 메뉴/유행어가 함께 언급되는가 (블로그 공기 빈도). "
               "지역 내 비중이 높을수록 그 상권의 시그니처 신호.")

    regions = sorted(df["region"].unique())
    pick = st.multiselect("지역 선택 (미선택 시 전체)", regions, default=[])
    view = df[df["region"].isin(pick)] if pick else df

    mode = st.radio("정렬", ["공기 빈도순", "지역 특색순(비중)"], horizontal=True)
    view = view.sort_values(
        "cooccur" if mode.startswith("공기") else "share_in_region", ascending=False
    )

    top_n = st.slider("표시 개수", 5, 50, 25, key="cross_topn")
    top = view.head(top_n).copy()
    top["조합"] = top["region"] + " × " + top["term"]
    metric = "cooccur" if mode.startswith("공기") else "share_in_region"
    st.bar_chart(top.set_index("조합")[metric], height=400)

    st.dataframe(
        view.rename(columns={
            "region": "지역", "term": "음식/유행어", "cooccur": "공기 횟수",
            "region_total": "지역 문서수", "share_in_region": "지역 내 비중",
        }),
        use_container_width=True, hide_index=True,
    )


def timeseries(db_path: str) -> None:
    """실행 회차가 2회 이상이면 키워드/후보별 급상승 스코어 추이를 그린다."""
    st.subheader("📈 실행 회차별 추이")
    a_runs = run_dates(db_path, "trend_snapshot")
    if len(a_runs) < 2:
        st.info(
            "실행 회차가 1회뿐이라 추이 그래프는 아직 없습니다. "
            "주간 배치(`run_weekly.bat`)가 누적되면 이 탭에서 키워드별 추세를 볼 수 있습니다."
        )
        return

    engine = st.radio("엔진", ["A (키워드)", "B (신조어)"], horizontal=True)
    if engine.startswith("A"):
        df = query(db_path, "SELECT run_date, keyword AS name, trend_score FROM trend_snapshot")
    else:
        df = query(db_path, "SELECT run_date, candidate AS name, trend_score FROM buzz_snapshot")
    if df.empty:
        st.info("데이터가 없습니다.")
        return

    latest = df.sort_values("run_date")["run_date"].iloc[-1]
    default = df[df["run_date"] == latest].nlargest(5, "trend_score")["name"].tolist()
    names = st.multiselect("추적할 항목", sorted(df["name"].unique()), default=default)
    if names:
        pivot = (df[df["name"].isin(names)]
                 .pivot_table(index="run_date", columns="name", values="trend_score"))
        st.line_chart(pivot, height=400)


def main() -> None:
    db_path = get_db_path()
    st.title("📡 Sobiz Trend Radar")
    st.caption("소상공인 트렌드 레이더 — 엔진 A(뉴스·정책) + 엔진 B(블로그·유행)")

    if not Path(db_path).exists():
        st.warning(
            f"이력 DB가 없습니다 (`{db_path}`).\n\n"
            "먼저 파이프라인을 실행하세요:\n\n"
            "```\npython -m src.run_pipeline\n```"
        )
        return

    overview(db_path)
    st.divider()

    # 실행일 선택 (A/B 스냅샷의 실행일 합집합)
    all_runs = sorted(
        set(run_dates(db_path, "trend_snapshot")) | set(run_dates(db_path, "buzz_snapshot")),
        reverse=True,
    )
    if not all_runs:
        st.info("스냅샷이 아직 없습니다. `python -m src.run_pipeline`을 실행하세요.")
        return

    run_date = st.sidebar.selectbox("실행일 선택", all_runs)
    st.sidebar.caption(f"총 {len(all_runs)}개 실행일 누적")

    tab_a, tab_b, tab_x, tab_ts = st.tabs(
        ["엔진 A · 정책 키워드", "엔진 B · 소비 유행", "지역 × 음식", "추이"]
    )
    with tab_a:
        engine_a(db_path, run_date)
    with tab_b:
        engine_b(db_path, run_date)
    with tab_x:
        cross_region(db_path, run_date)
    with tab_ts:
        timeseries(db_path)


main()
