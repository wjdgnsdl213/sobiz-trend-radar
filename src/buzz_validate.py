"""엔진 B 검증: 네이버 DataLab 검색어트렌드 API로 buzz_extract 발굴 후보를 교차 검증한다.

방법:
  1) buzz_candidates.csv에서 상위 N개 후보 로드
  2) DataLab API (POST /v1/datalab/search) 5개씩 배치 호출
  3) 수집 기간 내 최고 상대지수와 spike 여부를 붙여 buzz_validated.csv 저장

DataLab 상대지수: 조회 기간 중 최고 검색량을 100으로 환산한 상대값
spike_threshold 이상이면 "DataLab 확인된 트렌드"로 표시

실행: python -m src.buzz_validate
입력: data/processed/buzz_candidates.csv
출력: data/processed/buzz_validated.csv
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

from src.collect import load_config, MAX_RETRIES, RETRY_BACKOFF

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
DATALAB_BATCH = 5    # API 제한: 요청당 최대 5개 키워드 그룹
DATALAB_INTERVAL = 0.5  # 배치 간 대기(초) — DataLab도 초당 10회 제한

OUT_PATH = Path("data/processed/buzz_validated.csv")


def _get_headers() -> dict[str, str]:
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 없습니다.")
    return {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "Content-Type": "application/json",
    }


def datalab_request(
    keyword_groups: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    time_unit: str,
) -> list[dict[str, Any]] | None:
    """DataLab 검색어트렌드 단일 배치 호출. 실패 시 재시도."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": time_unit,
        "keywordGroups": keyword_groups,
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                DATALAB_URL, headers=_get_headers(), json=body, timeout=15
            )
            if resp.status_code in (401, 403):
                sys.exit(
                    f"DataLab API 인증 오류 ({resp.status_code}): {resp.text}\n"
                    "네이버 개발자센터 앱에서 데이터랩 검색어트렌드 권한이 활성화되어 있는지 확인하세요."
                )
            if resp.status_code == 429:
                wait = RETRY_BACKOFF ** (attempt + 1)
                print(f"  rate limit — {wait:.0f}초 대기 후 재시도...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF ** (attempt + 1))
            else:
                print(f"  요청 실패: {exc}")
    return None


def query_datalab(
    words: list[str],
    start_date: str,
    end_date: str,
    time_unit: str,
) -> dict[str, list[dict[str, Any]]]:
    """단어 목록을 5개 배치로 DataLab 조회. {단어: [{period, ratio},...]} 반환."""
    results: dict[str, list[dict[str, Any]]] = {}
    batches = [words[i : i + DATALAB_BATCH] for i in range(0, len(words), DATALAB_BATCH)]

    for i, batch in enumerate(batches):
        print(f"  배치 {i + 1}/{len(batches)}: {batch}")
        groups = [{"groupName": w, "keywords": [w]} for w in batch]
        data = datalab_request(groups, start_date, end_date, time_unit)
        if data:
            for item in data:
                results[item["title"]] = item.get("data", [])
        if i < len(batches) - 1:
            time.sleep(DATALAB_INTERVAL)

    return results


def main() -> None:
    cfg_all = load_config()
    cfg = cfg_all.get("buzz_validate", {})
    top_n = cfg.get("top_n_to_check", 20)
    weeks = cfg.get("weeks", 8)
    time_unit = cfg.get("time_unit", "week")
    spike_threshold = cfg.get("spike_threshold", 10)

    candidates_path = Path("data/processed/buzz_candidates.csv")
    if not candidates_path.exists():
        sys.exit(
            "buzz_candidates.csv 없음. 먼저 python -m src.buzz_extract 를 실행하세요."
        )

    df = pd.read_csv(candidates_path)
    df = df.sort_values("trend_score", ascending=False, na_position="last")
    top_words = df["candidate"].head(top_n).tolist()
    print(f"DataLab 검증 대상: {len(top_words)}개")
    print(f"  {top_words}\n")

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(weeks=weeks)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")
    print(f"조회 기간: {start_date} ~ {end_date}  단위: {time_unit}\n")

    print("DataLab API 호출 중...")
    datalab_data = query_datalab(top_words, start_date, end_date, time_unit)

    # 결과 정리: 후보별 최고 상대지수 + spike 여부
    rows: list[dict[str, Any]] = []
    for word in top_words:
        data = datalab_data.get(word, [])
        ratios = [d["ratio"] for d in data] if data else []
        # 주별 최고 지수: 조회 기간 내 검색 관심도 피크
        max_ratio = max(ratios) if ratios else 0.0
        rows.append({
            "candidate": word,
            "datalab_max_ratio": round(max_ratio, 1),
            "datalab_confirmed": max_ratio >= spike_threshold,
            "datalab_periods": json.dumps(data, ensure_ascii=False),
        })

    validate_df = pd.DataFrame(rows)
    merged = (
        df[df["candidate"].isin(top_words)]
        .merge(validate_df, on="candidate", how="left")
        .sort_values("trend_score", ascending=False, na_position="last")
    )
    merged.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"\n저장 → {OUT_PATH}\n")

    confirmed_n = validate_df["datalab_confirmed"].sum()
    print(
        f"■ DataLab 교차 검증 결과  "
        f"(기준: 최고 상대지수 ≥ {spike_threshold} → 확인됨)\n"
        f"  검증 대상 {len(top_words)}개 중 DataLab 확인 {int(confirmed_n)}개\n"
    )

    print(f"  {'후보':<12} {'최고지수':>6}  {'확인':>4}  {'Engine B 스코어':>12}")
    print("  " + "-" * 46)
    for _, r in validate_df.iterrows():
        eb = df[df["candidate"] == r["candidate"]]["trend_score"].values
        eb_score = f"{eb[0]:.1f}" if len(eb) > 0 and pd.notna(eb[0]) else "-"
        mark = "✅" if r["datalab_confirmed"] else "  "
        print(
            f"  {mark} {r['candidate']:<10} {r['datalab_max_ratio']:>6.1f}  {eb_score:>12}"
        )


if __name__ == "__main__":
    main()
