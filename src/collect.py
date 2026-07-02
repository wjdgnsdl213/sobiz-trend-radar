"""네이버 뉴스 검색 API로 소상공인 관련 뉴스를 수집한다.

실행: python -m src.collect
출력: data/raw/news_YYYYMMDD_HHMMSS.jsonl (제목+요약문, 링크 기준 중복 제거)
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

API_URL = "https://openapi.naver.com/v1/search/news.json"
MAX_DISPLAY = 100        # 1회 호출 최대 건수 (API 제한)
MAX_START = 1000         # start 파라미터 상한 (API 제한)
REQUEST_INTERVAL = 0.11  # 초당 10회 제한 준수용 호출 간격(초)
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0      # 재시도 대기 시간 = RETRY_BACKOFF * 시도 횟수(초)

_TAG_RE = re.compile(r"<[^>]+>")


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_text(text: str) -> str:
    """API 응답에 섞인 HTML 태그(<b> 등)와 엔티티(&quot; 등)를 제거한다."""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def fetch_page(
    session: requests.Session, query: str, start: int, display: int, sort: str
) -> list[dict[str, Any]]:
    """검색 결과 한 페이지를 가져온다. 실패 시 지수 백오프로 재시도."""
    params = {"query": query, "start": start, "display": display, "sort": sort}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(API_URL, params=params, timeout=10)
            if resp.status_code == 429:
                # rate limit 초과: 대기 후 재시도
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            if resp.status_code in (401, 403):
                # 인증 오류는 재시도해도 소용없으므로 즉시 중단
                sys.exit(
                    f"API 인증 실패 (HTTP {resp.status_code}): {resp.text}\n"
                    "네이버 개발자센터에서 키와 '검색' API 사용 설정을 확인하세요."
                )
            resp.raise_for_status()
            return resp.json().get("items", [])
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            print(f"  요청 실패({exc}) — {attempt}회차 재시도 대기 중...")
            time.sleep(RETRY_BACKOFF * attempt)
    return []


def collect_query(
    session: requests.Session,
    query: str,
    max_items: int,
    since: datetime,
    sort: str,
) -> list[dict[str, Any]]:
    """단일 쿼리로 since 이후 기사를 수집한다.

    sort=date(최신순)일 때 기준 시점보다 오래된 기사가 나오면 페이징을 중단한다.
    """
    items: list[dict[str, Any]] = []
    for start in range(1, min(max_items, MAX_START) + 1, MAX_DISPLAY):
        page = fetch_page(session, query, start, MAX_DISPLAY, sort)
        if not page:
            break
        reached_old = False
        for it in page:
            pub = parsedate_to_datetime(it["pubDate"])
            if pub < since:
                reached_old = True
                if sort == "date":
                    break
                continue  # 정확도순이면 이후 기사가 더 최신일 수 있으므로 계속
            items.append(
                {
                    "query": query,
                    "title": clean_text(it["title"]),
                    "description": clean_text(it["description"]),
                    "link": it.get("originallink") or it["link"],
                    "pub_date": pub.isoformat(),
                }
            )
        if reached_old and sort == "date":
            break
        time.sleep(REQUEST_INTERVAL)  # 초당 10회 제한 준수
    return items


def main() -> None:
    load_dotenv()
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없습니다. "
            ".env.example을 .env로 복사한 뒤 키를 입력하세요."
        )

    cfg = load_config()["collect"]
    since = datetime.now(timezone.utc) - timedelta(weeks=cfg["weeks"])

    session = requests.Session()
    session.headers.update(
        {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    )

    # 링크 기준 중복 제거 — 쿼리 간 중복뿐 아니라 같은 쿼리 안의 중복도 제거
    # (최신순 페이징 중 새 기사가 올라오면 같은 기사가 두 페이지에 반환될 수 있음)
    seen_links: set[str] = set()
    articles: list[dict[str, Any]] = []
    for query in cfg["queries"]:
        rows = collect_query(session, query, cfg["max_per_query"], since, cfg["sort"])
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if row["link"] not in seen_links:
                seen_links.add(row["link"])
                fresh.append(row)
        articles.extend(fresh)
        print(f"[{query}] 수집 {len(rows)}건 → 중복 제거 후 신규 {len(fresh)}건")

    out_dir = Path("data/raw")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"news_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for article in articles:
            f.write(json.dumps(article, ensure_ascii=False) + "\n")
    print(f"총 {len(articles)}건 저장 → {out_path}")


if __name__ == "__main__":
    main()
