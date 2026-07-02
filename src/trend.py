"""주 단위 급상승 키워드 스코어.

급상승 스코어 = (이번 주 문서 빈도) / (이전 N주 평균 빈도 + 1)
이번 주 빈도가 최소 기준(min_weekly_freq) 미만인 키워드는 제외한다.

실행: python -m src.trend
입력: data/processed/doc_keywords.parquet
출력: data/processed/trend_scores.csv
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

IN_PATH = Path("data/processed/doc_keywords.parquet")
OUT_PATH = Path("data/processed/trend_scores.csv")


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def explode_keywords(df: pd.DataFrame) -> pd.DataFrame:
    """문서별 키워드 리스트를 (주차, 키워드) 행으로 펼친다."""
    rows: list[dict[str, Any]] = []
    weeks = pd.to_datetime(df["pub_date"]).dt.isocalendar()
    week_labels = weeks["year"].astype(str) + "-W" + weeks["week"].astype(str).str.zfill(2)
    for week, kw_json in zip(week_labels, df["keywords"]):
        for kw, _score in json.loads(kw_json):
            rows.append({"week": week, "keyword": kw})
    return pd.DataFrame(rows)


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"{IN_PATH}가 없습니다. 먼저 python -m src.extract 를 실행하세요.")

    cfg = load_config()["trend"]
    df = pd.read_parquet(IN_PATH)
    long = explode_keywords(df)

    # 주차 × 키워드 문서 빈도 피벗
    pivot = long.pivot_table(
        index="keyword", columns="week", aggfunc="size", fill_value=0
    )
    weeks = sorted(pivot.columns)
    print(f"주차별 문서 수: ", end="")
    week_docs = pd.to_datetime(df["pub_date"]).dt.isocalendar()
    labels = week_docs["year"].astype(str) + "-W" + week_docs["week"].astype(str).str.zfill(2)
    print(dict(labels.value_counts().sort_index()))

    if len(weeks) < 2:
        sys.exit("주차가 2개 미만이라 트렌드 계산이 불가능합니다. 수집 기간을 늘리세요.")

    target_week = weeks[-1]
    baseline_weeks = weeks[-1 - cfg["baseline_weeks"] : -1]  # 직전 N주 (부족하면 있는 만큼)
    print(f"대상 주: {target_week} / 기준 주: {baseline_weeks}")

    this_week = pivot[target_week]
    baseline_avg = pivot[baseline_weeks].mean(axis=1)
    result = pd.DataFrame(
        {
            "keyword": pivot.index,
            "this_week_freq": this_week.values,
            "baseline_avg": baseline_avg.round(2).values,
            # 급상승 스코어: 이번 주 빈도 / (이전 N주 평균 + 1)
            "trend_score": (this_week / (baseline_avg + 1)).round(3).values,
        }
    )
    # 최소 빈도 필터: 이번 주 min_weekly_freq회 미만 제외
    result = result[result["this_week_freq"] >= cfg["min_weekly_freq"]]
    result = result.sort_values("trend_score", ascending=False).reset_index(drop=True)

    result.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"저장 → {OUT_PATH} ({len(result)}개 키워드)")

    print("\n급상승 상위 20개:")
    for _, row in result.head(20).iterrows():
        print(
            f"  {row['keyword']:<20} 이번주 {row['this_week_freq']:>3}건 "
            f"/ 직전{len(baseline_weeks)}주 평균 {row['baseline_avg']:>5.1f}건 "
            f"→ 스코어 {row['trend_score']:.2f}"
        )


if __name__ == "__main__":
    main()
