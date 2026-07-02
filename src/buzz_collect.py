"""엔진 B: 네이버 블로그 검색 API로 소비 트렌드 관련 포스트를 수집한다.

뉴스는 유행의 후행 지표라, 소비 유행어는 블로그에서 더 빨리·소비자 언어로 드러난다.
제목+요약문만 사용하고 본문 크롤링은 하지 않는다.

실행: python -m src.buzz_collect
출력: data/raw_blog/blog_YYYYMMDD_HHMMSS.jsonl
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

# collect.py의 재사용 헬퍼/상수 (HTML 정리, rate limit 준수)
from src.collect import (
    MAX_DISPLAY,
    MAX_RETRIES,
    MAX_START,
    REQUEST_INTERVAL,
    RETRY_BACKOFF,
    clean_text,
    load_config,
)

BLOG_API_URL = "https://openapi.naver.com/v1/search/blog.json"

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")


def parse_postdate(postdate: str) -> datetime:
    """블로그 API의 postdate(YYYYMMDD)를 datetime으로 변환한다."""
    return datetime.strptime(postdate, "%Y%m%d")


def fetch_page(
    session: requests.Session, query: str, start: int, display: int, sort: str
) -> list[dict[str, Any]]:
    """블로그 검색 한 페이지를 가져온다. 실패 시 지수 백오프로 재시도."""
    params = {"query": query, "start": start, "display": display, "sort": sort}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(BLOG_API_URL, params=params, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            if resp.status_code in (401, 403):
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
    session: requests.Session, query: str, max_items: int, since: datetime, sort: str
) -> list[dict[str, Any]]:
    """단일 쿼리로 since 이후 블로그 포스트를 수집한다 (최신순이면 오래된 글에서 중단)."""
    items: list[dict[str, Any]] = []
    for start in range(1, min(max_items, MAX_START) + 1, MAX_DISPLAY):
        page = fetch_page(session, query, start, MAX_DISPLAY, sort)
        if not page:
            break
        reached_old = False
        for it in page:
            pub = parse_postdate(it["postdate"]) if it.get("postdate") else since
            if pub < since:
                reached_old = True
                if sort == "date":
                    break
                continue
            items.append(
                {
                    "query": query,
                    "title": clean_text(it["title"]),
                    "description": clean_text(it["description"]),
                    "link": it["link"],
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
        sys.exit("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없습니다. .env를 확인하세요.")

    cfg = load_config()["buzz"]
    since = datetime.now() - timedelta(weeks=cfg["weeks"])

    session = requests.Session()
    session.headers.update(
        {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    )

    seen_links: set[str] = set()
    posts: list[dict[str, Any]] = []
    for query in cfg["queries"]:
        rows = collect_query(session, query, cfg["max_per_query"], since, cfg["sort"])
        fresh = [r for r in rows if r["link"] not in seen_links]
        seen_links.update(r["link"] for r in fresh)
        posts.extend(fresh)
        print(f"[{query}] 수집 {len(rows)}건 → 중복 제거 후 신규 {len(fresh)}건")

    out_dir = Path("data/raw_blog")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"blog_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for post in posts:
            f.write(json.dumps(post, ensure_ascii=False) + "\n")
    print(f"총 {len(posts)}건 저장 → {out_path}")


if __name__ == "__main__":
    main()
