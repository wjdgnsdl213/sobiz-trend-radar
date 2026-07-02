"""대시보드: 이력 DB(data/trends.db)를 읽어 두 엔진 트렌드를 시각화한다.

실행: python -m streamlit run src/dashboard.py

- 개요: 누적 원본·스냅샷 현황
- 엔진 A: 실행일별 급상승 키워드(정책·사업환경)
- 엔진 B: 실행일별 신조어/유행 + DataLab 검증 결과
- 지역×음식: 상권별 메뉴/유행 공기 조합
- 추이: 실행 회차가 2회 이상 쌓이면 항목별 추이

데이터가 없으면 python -m src.run_pipeline 안내만 표시한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import yaml

st.set_page_config(page_title="Sobiz Trend Radar", page_icon="📡", layout="wide")

# 엔진별 테마 색 — 시각적으로 A/B/지역을 구분한다
COLOR_A = "#2563EB"   # 뉴스·정책 (파랑)
COLOR_B = "#EA580C"   # 소비 유행 (주황)
COLOR_X = "#059669"   # 지역×음식 (초록)


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


def hbar(data: pd.DataFrame, cat: str, val: str, val_title: str,
         color: str, fmt: str = ".1f", tooltip: list | None = None) -> alt.Chart:
    """가로 막대차트 — 값 내림차순 정렬 + 막대 끝 값 라벨. 한글 라벨이 잘 읽힌다."""
    n = len(data)
    height = max(180, n * 30)
    base = alt.Chart(data).encode(
        y=alt.Y(f"{cat}:N", sort="-x", title=None,
                axis=alt.Axis(labelLimit=260, labelFontSize=13)),
        x=alt.X(f"{val}:Q", title=val_title, axis=alt.Axis(labelFontSize=11)),
    )
    bars = base.mark_bar(cornerRadiusEnd=4, color=color, opacity=0.9)
    if tooltip:
        bars = bars.encode(tooltip=tooltip)
    labels = base.mark_text(align="left", dx=4, fontSize=12, color="#374151").encode(
        text=alt.Text(f"{val}:Q", format=fmt)
    )
    return (bars + labels).properties(height=height).configure_view(strokeWidth=0)


def overview(db_path: str) -> None:
    news = query(db_path, "SELECT COUNT(*) n, MIN(first_seen) a, MAX(first_seen) b FROM news_articles")
    blog = query(db_path, "SELECT COUNT(*) n, MIN(first_seen) a, MAX(first_seen) b FROM blog_posts")
    runs = len(run_dates(db_path, "trend_snapshot"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📰 뉴스 원본 누적", f"{int(news['n'][0]):,}" if not news.empty else "0")
    c2.metric("📝 블로그 원본 누적", f"{int(blog['n'][0]):,}" if not blog.empty else "0")
    c3.metric("🔁 파이프라인 실행", f"{runs}회")
    span = f"{news['a'][0]} ~ {news['b'][0]}" if not news.empty and news["a"][0] else "-"
    c4.metric("🗓️ 뉴스 수집일 범위", span)


def engine_a(db_path: str, run_date: str) -> None:
    st.markdown(f"#### 📰 정책·사업환경 급상승 키워드 &nbsp;`{run_date}`", unsafe_allow_html=True)
    st.caption("이번 주 빈도 ÷ (직전 3주 평균 + 1). 스코어가 높을수록 최근 급부상한 키워드.")
    df = query(
        db_path,
        "SELECT keyword, this_week_freq, baseline_avg, trend_score "
        "FROM trend_snapshot WHERE run_date = ? ORDER BY trend_score DESC",
        (run_date,),
    )
    if df.empty:
        st.info("이 실행일의 트렌드 스냅샷이 없습니다.")
        return

    top_n = st.slider("표시 개수", 5, 40, 15, key="a_topn")
    top = df.head(top_n)

    chart_col, table_col = st.columns([3, 2])
    with chart_col:
        st.altair_chart(
            hbar(top, "keyword", "trend_score", "급상승 스코어", COLOR_A,
                 tooltip=[alt.Tooltip("keyword", title="키워드"),
                          alt.Tooltip("trend_score", title="스코어", format=".2f"),
                          alt.Tooltip("this_week_freq", title="이번주 빈도")]),
            width="stretch",
        )
    with table_col:
        show = df.rename(columns={
            "keyword": "키워드", "this_week_freq": "이번주 빈도",
            "baseline_avg": "직전 평균", "trend_score": "급상승 스코어",
        })
        st.dataframe(
            show, width="stretch", hide_index=True, height=min(560, 60 + len(df) * 35),
            column_config={
                "급상승 스코어": st.column_config.ProgressColumn(
                    "급상승 스코어", format="%.1f",
                    min_value=0.0, max_value=float(df["trend_score"].max())),
                "직전 평균": st.column_config.NumberColumn(format="%.1f"),
            },
        )


def engine_b(db_path: str, run_date: str) -> None:
    st.markdown(f"#### 🍞 소비 유행 · 신조어 &nbsp;`{run_date}`", unsafe_allow_html=True)
    st.caption("soynlp 통계로 발굴한 유행어 후보. 🆕=사전 미등재 신조어, ✅=DataLab 검색량으로 확인됨.")
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

    c1, c2, _ = st.columns([1, 1, 2])
    only_neo = c1.checkbox("🆕 신조어만", value=False)
    only_confirmed = c2.checkbox("✅ DataLab 확인만", value=False)
    view = df.copy()
    if only_neo:
        view = view[view["is_neologism"] == 1]
    if only_confirmed:
        view = view[view["datalab_confirmed"] == 1]
    if view.empty:
        st.info("조건에 맞는 후보가 없습니다.")
        return

    top_n = st.slider("표시 개수", 5, 40, 15, key="b_topn")
    top = view.head(top_n)

    chart_col, table_col = st.columns([3, 2])
    with chart_col:
        st.altair_chart(
            hbar(top, "candidate", "trend_score", "급상승 스코어", COLOR_B,
                 tooltip=[alt.Tooltip("candidate", title="후보"),
                          alt.Tooltip("trend_score", title="스코어", format=".2f"),
                          alt.Tooltip("total_freq", title="빈도"),
                          alt.Tooltip("datalab_max_ratio", title="DataLab")]),
            width="stretch",
        )
    with table_col:
        show = view.assign(
            신조어=view["is_neologism"].map({1: "🆕", 0: ""}),
            확인=view["datalab_confirmed"].map({1: "✅", 0: ""}),
        ).rename(columns={
            "candidate": "후보", "total_freq": "빈도", "cohesion": "응집도",
            "trend_score": "급상승 스코어", "datalab_max_ratio": "DataLab 최고지수",
        })[["후보", "신조어", "확인", "빈도", "응집도", "급상승 스코어", "DataLab 최고지수"]]
        st.dataframe(
            show, width="stretch", hide_index=True, height=min(560, 60 + len(view) * 35),
            column_config={
                "급상승 스코어": st.column_config.ProgressColumn(
                    "급상승 스코어", format="%.1f",
                    min_value=0.0, max_value=float(view["trend_score"].max())),
                "DataLab 최고지수": st.column_config.ProgressColumn(
                    "DataLab 최고지수", format="%.0f", min_value=0.0, max_value=100.0),
                "응집도": st.column_config.NumberColumn(format="%.2f"),
            },
        )


def cross_region(db_path: str, run_date: str) -> None:
    st.markdown(f"#### 🗺️ 지역 × 음식 크로스 &nbsp;`{run_date}`", unsafe_allow_html=True)
    st.caption("어느 상권에서 어떤 메뉴/유행어가 함께 언급되는가. 지역 내 비중이 높을수록 그 상권의 시그니처 신호.")
    df = query(
        db_path,
        "SELECT region, term, cooccur, region_total, share_in_region "
        "FROM cross_region WHERE run_date = ? ORDER BY cooccur DESC",
        (run_date,),
    )
    if df.empty:
        st.info("이 실행일의 크로스 스냅샷이 없습니다. `python -m src.cross_region` 실행 후 DB 적재가 필요합니다.")
        return

    regions = sorted(df["region"].unique())
    c1, c2 = st.columns([2, 2])
    pick = c1.multiselect("지역 선택 (미선택 시 전체)", regions, default=[])
    mode = c2.radio("정렬 기준", ["공기 빈도순", "지역 특색순(비중)"], horizontal=True)

    view = df[df["region"].isin(pick)] if pick else df
    sort_col = "cooccur" if mode.startswith("공기") else "share_in_region"
    view = view.sort_values(sort_col, ascending=False)

    top_n = st.slider("표시 개수", 5, 50, 20, key="cross_topn")
    top = view.head(top_n).copy()
    top["조합"] = top["region"] + " × " + top["term"]

    chart_col, table_col = st.columns([3, 2])
    with chart_col:
        if mode.startswith("공기"):
            chart = hbar(top, "조합", "cooccur", "공기 횟수", COLOR_X, fmt="d",
                         tooltip=[alt.Tooltip("조합", title="조합"),
                                  alt.Tooltip("cooccur", title="공기 횟수"),
                                  alt.Tooltip("share_in_region", title="지역 내 비중", format=".1%")])
        else:
            chart = hbar(top, "조합", "share_in_region", "지역 내 비중", COLOR_X, fmt=".0%",
                         tooltip=[alt.Tooltip("조합", title="조합"),
                                  alt.Tooltip("share_in_region", title="지역 내 비중", format=".1%"),
                                  alt.Tooltip("cooccur", title="공기 횟수")])
        st.altair_chart(chart, width="stretch")
    with table_col:
        show = view.assign(**{"지역 내 비중%": (view["share_in_region"] * 100)}).rename(columns={
            "region": "지역", "term": "음식/유행어", "cooccur": "공기 횟수",
            "region_total": "지역 문서수",
        })[["지역", "음식/유행어", "공기 횟수", "지역 문서수", "지역 내 비중%"]]
        st.dataframe(
            show, width="stretch", hide_index=True, height=min(560, 60 + len(view) * 35),
            column_config={
                "공기 횟수": st.column_config.ProgressColumn(
                    "공기 횟수", format="%d",
                    min_value=0.0, max_value=float(df["cooccur"].max())),
                "지역 내 비중%": st.column_config.NumberColumn("지역 내 비중", format="%.1f%%"),
            },
        )


def timeseries(db_path: str) -> None:
    """실행 회차가 2회 이상이면 키워드/후보별 급상승 스코어 추이를 그린다."""
    st.markdown("#### 📈 실행 회차별 추이")
    a_runs = run_dates(db_path, "trend_snapshot")
    if len(a_runs) < 2:
        st.info(
            "실행 회차가 1회뿐이라 추이 그래프는 아직 없습니다. "
            "주간 배치(`run_weekly.bat`)가 누적되면 이 탭에서 항목별 추세를 볼 수 있습니다."
        )
        return

    engine = st.radio("엔진", ["A · 정책 키워드", "B · 신조어"], horizontal=True)
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
        line = (
            alt.Chart(df[df["name"].isin(names)])
            .mark_line(point=True, strokeWidth=2.5)
            .encode(
                x=alt.X("run_date:N", title="실행일"),
                y=alt.Y("trend_score:Q", title="급상승 스코어"),
                color=alt.Color("name:N", title="항목"),
                tooltip=["run_date", "name", alt.Tooltip("trend_score", format=".2f")],
            )
            .properties(height=420)
        )
        st.altair_chart(line, width="stretch")


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

    all_runs = sorted(
        set(run_dates(db_path, "trend_snapshot")) | set(run_dates(db_path, "buzz_snapshot")),
        reverse=True,
    )
    if not all_runs:
        st.info("스냅샷이 아직 없습니다. `python -m src.run_pipeline`을 실행하세요.")
        return

    st.sidebar.header("⚙️ 필터")
    run_date = st.sidebar.selectbox("실행일 선택", all_runs)
    st.sidebar.caption(f"총 {len(all_runs)}개 실행일 누적")

    tab_a, tab_b, tab_x, tab_ts = st.tabs(
        ["📰 엔진 A · 정책 키워드", "🍞 엔진 B · 소비 유행", "🗺️ 지역 × 음식", "📈 추이"]
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
