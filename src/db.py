"""이력 DB 계층: 각 단계 산출물을 SQLite에 누적 적재한다.

목적:
  - raw 기사/포스트를 링크 기준으로 누적(first_seen 보존) → 주간 반복 실행 시
    네이버 API의 '1,000건 상한 + 날짜범위 지정 불가' 한계를 시간이 해결한다.
  - 필터·트렌드·버즈 파생물은 실행일별 스냅샷으로 저장 → 이후 대시보드가 읽어간다.

설계 원칙: 기존 파이프라인 스크립트는 그대로 두고, 이 모듈이 산출물을 읽어 적재만 한다.

실행:
  python -m src.db --init      # 스키마 생성
  python -m src.db --ingest    # 최신 산출물 자동 탐지 후 적재(스키마 없으면 생성)
  python -m src.db --summary   # 누적 현황 출력
옵션:
  --run-date YYYY-MM-DD        # 스냅샷 실행일 지정(기본: 오늘)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.collect import load_config

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

SCHEMA = """
-- 뉴스 원본 (링크 유일, 최초 수집분 보존)
CREATE TABLE IF NOT EXISTS news_articles (
    link        TEXT PRIMARY KEY,
    query       TEXT,
    title       TEXT,
    description TEXT,
    pub_date    TEXT,   -- 기사 발행일 (ISO)
    first_seen  TEXT    -- DB 최초 적재 실행일
);

-- 블로그 원본 (링크 유일, 최초 수집분 보존)
CREATE TABLE IF NOT EXISTS blog_posts (
    link        TEXT PRIMARY KEY,
    query       TEXT,
    title       TEXT,
    description TEXT,
    pub_date    TEXT,
    first_seen  TEXT
);

-- 엔진 A 필터 결과 (실행일별 스냅샷)
CREATE TABLE IF NOT EXISTS filter_results (
    run_date   TEXT,
    link       TEXT,
    similarity REAL,
    relevant   INTEGER,
    is_dup     INTEGER,
    kept       INTEGER,
    PRIMARY KEY (run_date, link)
);

-- 엔진 A 키워드 급상승 스코어 (실행일별 스냅샷)
CREATE TABLE IF NOT EXISTS trend_snapshot (
    run_date       TEXT,
    keyword        TEXT,
    this_week_freq INTEGER,
    baseline_avg   REAL,
    trend_score    REAL,
    PRIMARY KEY (run_date, keyword)
);

-- 엔진 B 신조어 후보 + DataLab 검증 (실행일별 스냅샷)
CREATE TABLE IF NOT EXISTS buzz_snapshot (
    run_date          TEXT,
    candidate         TEXT,
    total_freq        INTEGER,
    cohesion          REAL,
    word_type         TEXT,
    is_neologism      INTEGER,
    trend_score       REAL,
    datalab_max_ratio REAL,
    datalab_confirmed INTEGER,
    PRIMARY KEY (run_date, candidate)
);

-- 지역×음식 크로스 분석 (실행일별 스냅샷)
CREATE TABLE IF NOT EXISTS cross_region (
    run_date        TEXT,
    region          TEXT,
    term            TEXT,
    cooccur         INTEGER,
    region_total    INTEGER,
    share_in_region REAL,
    PRIMARY KEY (run_date, region, term)
);
"""


def get_conn(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 기존 DB 마이그레이션: 누락 컬럼 추가 (스키마 변경 하위호환)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(buzz_snapshot)")]
    if "word_type" not in cols:
        conn.execute("ALTER TABLE buzz_snapshot ADD COLUMN word_type TEXT")
    conn.commit()


def _latest(dir_path: str, pattern: str) -> Path | None:
    files = sorted(Path(dir_path).glob(pattern))
    return files[-1] if files else None


def _file_date(path: Path) -> str:
    """파일명 타임스탬프(news_YYYYMMDD_HHMMSS)에서 수집일을 뽑는다. 실패 시 mtime."""
    try:
        stamp = path.stem.split("_")[1]  # YYYYMMDD
        return datetime.strptime(stamp, "%Y%m%d").strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def ingest_raw(conn: sqlite3.Connection, path: Path, table: str) -> int:
    """raw jsonl을 링크 기준으로 누적한다. 이미 있는 링크는 최초 수집분 보존(INSERT OR IGNORE)."""
    df = pd.read_json(path, lines=True)
    first_seen = _file_date(path)
    rows = [
        (r["link"], r.get("query"), r.get("title"), r.get("description"),
         str(r.get("pub_date")), first_seen)
        for _, r in df.iterrows()
    ]
    cur = conn.executemany(
        f"INSERT OR IGNORE INTO {table} "
        "(link, query, title, description, pub_date, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return cur.rowcount  # 새로 추가된(무시되지 않은) 건수


def _clear_run(conn: sqlite3.Connection, table: str, run_date: str) -> None:
    """스냅샷 재적재를 완전 멱등하게 만든다: 해당 실행일 행을 먼저 비운다.
    (INSERT OR REPLACE만 쓰면 이번에 사라진 행이 잔존하는 문제 방지)"""
    conn.execute(f"DELETE FROM {table} WHERE run_date = ?", (run_date,))


def ingest_filter(conn: sqlite3.Connection, path: Path, run_date: str) -> int:
    df = pd.read_parquet(path)
    _clear_run(conn, "filter_results", run_date)
    rows = [
        (run_date, r["link"], float(r["similarity"]), int(bool(r["relevant"])),
         int(bool(r["is_dup"])), int(bool(r["kept"])))
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO filter_results "
        "(run_date, link, similarity, relevant, is_dup, kept) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_trend(conn: sqlite3.Connection, path: Path, run_date: str) -> int:
    df = pd.read_csv(path)
    _clear_run(conn, "trend_snapshot", run_date)
    rows = [
        (run_date, r["keyword"], int(r["this_week_freq"]),
         float(r["baseline_avg"]), float(r["trend_score"]))
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO trend_snapshot "
        "(run_date, keyword, this_week_freq, baseline_avg, trend_score) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_buzz(conn: sqlite3.Connection, path: Path, run_date: str,
                validated_path: Path | None = None) -> int:
    """전체 buzz 후보를 적재하고, DataLab 검증(상위 일부)이 있으면 병합한다.

    검증본은 상위 N개만 담기므로, 전체 후보(신조어 포함)를 base로 두고
    DataLab 컬럼만 left-join 해야 신조어가 대시보드에서 누락되지 않는다.
    """
    df = pd.read_csv(path)
    _clear_run(conn, "buzz_snapshot", run_date)
    if validated_path is not None and validated_path.exists():
        v = pd.read_csv(validated_path)
        if "datalab_max_ratio" in v.columns:
            df = df.merge(
                v[["candidate", "datalab_max_ratio", "datalab_confirmed"]],
                on="candidate", how="left",
            )
    has_datalab = "datalab_max_ratio" in df.columns
    has_wtype = "word_type" in df.columns
    rows = []
    for _, r in df.iterrows():
        rows.append((
            run_date, r["candidate"], int(r["total_freq"]), float(r["cohesion"]),
            r["word_type"] if has_wtype else None,
            int(bool(r["is_neologism"])),
            None if pd.isna(r["trend_score"]) else float(r["trend_score"]),
            float(r["datalab_max_ratio"]) if has_datalab and pd.notna(r["datalab_max_ratio"]) else None,
            int(bool(r["datalab_confirmed"])) if has_datalab and pd.notna(r["datalab_confirmed"]) else None,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO buzz_snapshot "
        "(run_date, candidate, total_freq, cohesion, word_type, is_neologism, trend_score, "
        "datalab_max_ratio, datalab_confirmed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_cross(conn: sqlite3.Connection, path: Path, run_date: str) -> int:
    df = pd.read_csv(path)
    _clear_run(conn, "cross_region", run_date)
    if df.empty:
        return 0
    rows = [
        (run_date, r["region"], r["term"], int(r["cooccur"]),
         int(r["region_total"]), float(r["share_in_region"]))
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO cross_region "
        "(run_date, region, term, cooccur, region_total, share_in_region) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_all(conn: sqlite3.Connection, run_date: str) -> None:
    """존재하는 최신 산출물을 자동 탐지해 적재한다. 없는 단계는 건너뛴다."""
    news = _latest("data/raw", "news_*.jsonl")
    blog = _latest("data/raw_blog", "blog_*.jsonl")
    filt = Path("data/processed/filtered.parquet")
    trend = Path("data/processed/trend_scores.csv")
    buzz_val = Path("data/processed/buzz_validated.csv")
    buzz_cand = Path("data/processed/buzz_candidates.csv")
    cross = Path("data/processed/cross_region.csv")

    if news:
        n = ingest_raw(conn, news, "news_articles")
        print(f"  뉴스 원본 {news.name}: 신규 {n}건 적재")
    if blog:
        n = ingest_raw(conn, blog, "blog_posts")
        print(f"  블로그 원본 {blog.name}: 신규 {n}건 적재")
    if filt.exists():
        n = ingest_filter(conn, filt, run_date)
        print(f"  필터 결과: {n}건 스냅샷({run_date})")
    if trend.exists():
        n = ingest_trend(conn, trend, run_date)
        print(f"  트렌드 스코어: {n}건 스냅샷({run_date})")
    if buzz_cand.exists():
        n = ingest_buzz(conn, buzz_cand, run_date,
                        buzz_val if buzz_val.exists() else None)
        tag = "DataLab 병합" if buzz_val.exists() else "미검증"
        print(f"  버즈 후보 전체({tag}): {n}건 스냅샷({run_date})")
    if cross.exists():
        n = ingest_cross(conn, cross, run_date)
        print(f"  지역×음식 크로스: {n}건 스냅샷({run_date})")


def summary(conn: sqlite3.Connection) -> None:
    def scalar(q: str):
        return conn.execute(q).fetchone()[0]

    print("■ 이력 DB 누적 현황")
    print(f"  뉴스 원본        {scalar('SELECT COUNT(*) FROM news_articles'):>7,} 건  "
          f"(수집일 {scalar('SELECT COUNT(DISTINCT first_seen) FROM news_articles')}종)")
    print(f"  블로그 원본      {scalar('SELECT COUNT(*) FROM blog_posts'):>7,} 건  "
          f"(수집일 {scalar('SELECT COUNT(DISTINCT first_seen) FROM blog_posts')}종)")
    print(f"  필터 스냅샷      {scalar('SELECT COUNT(*) FROM filter_results'):>7,} 행  "
          f"(실행 {scalar('SELECT COUNT(DISTINCT run_date) FROM filter_results')}회)")
    print(f"  트렌드 스냅샷    {scalar('SELECT COUNT(*) FROM trend_snapshot'):>7,} 행  "
          f"(실행 {scalar('SELECT COUNT(DISTINCT run_date) FROM trend_snapshot')}회)")
    print(f"  버즈 스냅샷      {scalar('SELECT COUNT(*) FROM buzz_snapshot'):>7,} 행  "
          f"(실행 {scalar('SELECT COUNT(DISTINCT run_date) FROM buzz_snapshot')}회)")
    print(f"  크로스 스냅샷    {scalar('SELECT COUNT(*) FROM cross_region'):>7,} 행  "
          f"(실행 {scalar('SELECT COUNT(DISTINCT run_date) FROM cross_region')}회)")

    news_span = conn.execute(
        "SELECT MIN(first_seen), MAX(first_seen) FROM news_articles").fetchone()
    if news_span[0]:
        print(f"  뉴스 수집일 범위 {news_span[0]} ~ {news_span[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="이력 DB 적재/조회")
    parser.add_argument("--init", action="store_true", help="스키마만 생성")
    parser.add_argument("--ingest", action="store_true", help="최신 산출물 적재")
    parser.add_argument("--summary", action="store_true", help="누적 현황 출력")
    parser.add_argument("--run-date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="스냅샷 실행일 (기본: 오늘)")
    args = parser.parse_args()

    db_path = load_config().get("db", {}).get("path", "data/trends.db")
    conn = get_conn(db_path)
    print(f"DB: {db_path}")

    # 아무 옵션도 없으면 ingest를 기본 동작으로
    if not (args.init or args.ingest or args.summary):
        args.ingest = True

    init_db(conn)  # 항상 스키마 보장(멱등)
    if args.init and not (args.ingest or args.summary):
        print("스키마 생성 완료")
    if args.ingest:
        print(f"적재 실행일: {args.run_date}")
        ingest_all(conn, args.run_date)
        print()
    if args.ingest or args.summary:
        summary(conn)

    conn.close()


if __name__ == "__main__":
    main()
