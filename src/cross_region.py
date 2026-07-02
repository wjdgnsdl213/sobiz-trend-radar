"""지역×음식 크로스 분석: 어느 상권에서 어떤 메뉴/유행어가 함께 뜨는가.

gazetteer(config.yaml의 cross.regions) 기반. NER 모델 없이 지역 사전과
버즈 후보(음식/유행어 어휘)의 공기(co-occurrence) 빈도를 블로그 원본에서 집계한다.
형태소 사전에 없는 신생 상권도 사전에 추가만 하면 즉시 잡힌다.

방법:
  1) 블로그(또는 뉴스) 원본 텍스트 로드
  2) 지역 사전 + 버즈 후보 어휘를 각 문서에서 부분문자열로 탐지
  3) 같은 문서에 등장한 (지역, 후보) 쌍의 문서 빈도 집계
  4) min_cooccur 이상 조합만 남겨 상위 출력

실행: python -m src.cross_region
입력: data/raw_blog 최신 jsonl + data/processed/buzz_candidates.csv
출력: data/processed/cross_region.csv
"""

from __future__ import annotations

import re
import sys
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

from src.collect import load_config

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

OUT_PATH = Path("data/processed/cross_region.csv")
_NORM_RE = re.compile(r"[^가-힣A-Za-z0-9]+")


def normalize(text: str) -> str:
    return _NORM_RE.sub(" ", text).strip()


def latest_raw(source: str) -> Path:
    """source에 맞는 최신 원본 jsonl을 찾는다."""
    if source == "news":
        files = sorted(Path("data/raw").glob("news_*.jsonl"))
        hint = "python -m src.collect"
    else:
        files = sorted(Path("data/raw_blog").glob("blog_*.jsonl"))
        hint = "python -m src.buzz_collect"
    if not files:
        sys.exit(f"{source} 원본이 없습니다. 먼저 {hint} 를 실행하세요.")
    return files[-1]


def load_terms(cfg: dict[str, Any]) -> list[str]:
    """음식/유행어 후보 어휘를 버즈 후보 CSV에서 로드한다.

    지역 특색을 드러내는 '무엇이 함께 뜨는가'만 남기기 위해 다음을 배제한다:
      - 지역명 자체 및 지역명을 부분포함한 복합어(부산핫플, 강남역 등)
      - 일반 카테고리/마케팅어(term_stopwords)
      - include_word_types 밖의 유형(기본: 일반어 제외, 합성어·신조어만 사용)
    """
    if cfg["term_source"] != "buzz":
        sys.exit(f"지원하지 않는 term_source: {cfg['term_source']}")
    path = Path("data/processed/buzz_candidates.csv")
    if not path.exists():
        sys.exit("buzz_candidates.csv 없음. 먼저 python -m src.buzz_extract 를 실행하세요.")

    df = pd.read_csv(path)
    include = set(cfg.get("include_word_types", ["신조어", "합성어"]))
    if "word_type" in df.columns:
        df = df[df["word_type"].isin(include)]

    regions = cfg["regions"]
    stopwords = set(cfg.get("term_stopwords", []))
    substr_stops = cfg.get("term_substring_stopwords", [])

    def is_region_related(t: str) -> bool:
        # 지역명을 부분포함하거나 지역명에 부분포함되는 복합어/파편 제외
        return any(r in t or t in r for r in regions)

    def has_generic_morpheme(t: str) -> bool:
        # 마케팅 상투 형태소를 포함한 복합어 제외 (신상카페, 카페추천 등)
        return any(s in t for s in substr_stops)

    terms = df["candidate"].astype(str).tolist()
    return [
        t for t in terms
        if t not in stopwords and not is_region_related(t) and not has_generic_morpheme(t)
    ]


def cooccur(texts: list[str], regions: list[str], terms: list[str],
            min_cooccur: int) -> pd.DataFrame:
    """문서별로 등장한 (지역, 후보) 쌍의 문서 빈도를 집계한다."""
    term_set = set(terms)
    counts: dict[tuple[str, str], int] = {}
    region_doc: dict[str, int] = {r: 0 for r in regions}

    for text in texts:
        # 부분문자열 탐지 — 지역명·후보어는 특정성이 높아 오탐이 낮다
        found_r = [r for r in regions if r in text]
        if not found_r:
            continue
        found_t = [t for t in term_set if t in text]
        for r in set(found_r):
            region_doc[r] += 1
        for r, t in product(set(found_r), set(found_t)):
            counts[(r, t)] = counts.get((r, t), 0) + 1

    rows = [
        {"region": r, "term": t, "cooccur": c, "region_total": region_doc[r]}
        for (r, t), c in counts.items()
        if c >= min_cooccur
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        # lift: 지역 내에서 이 후보가 함께 언급된 비율(상권 특색 신호)
        df["share_in_region"] = (df["cooccur"] / df["region_total"]).round(3)
        df = df.sort_values(["cooccur", "share_in_region"], ascending=False)
    return df.reset_index(drop=True)


def main() -> None:
    cfg = load_config()["cross"]
    regions = cfg["regions"]
    terms = load_terms(cfg)

    raw_path = latest_raw(cfg["source"])
    print(f"입력 원본: {raw_path}")
    df = pd.read_json(raw_path, lines=True)
    texts = (df["title"].fillna("") + " " + df["description"].fillna("")).map(normalize).tolist()
    print(f"문서 {len(texts)}건 · 지역 사전 {len(regions)}개 · 후보 어휘 {len(terms)}개")

    result = cooccur(texts, regions, terms, cfg["min_cooccur"])
    if result.empty:
        print(f"\n공기 {cfg['min_cooccur']}회 이상 (지역×후보) 조합이 없습니다. "
              "min_cooccur를 낮추거나 데이터를 더 모으세요.")
        OUT_PATH.write_text("region,term,cooccur,region_total,share_in_region\n", encoding="utf-8-sig")
        return

    result.to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"저장 → {OUT_PATH}  ({len(result)}개 조합)\n")

    top_n = cfg["top_n"]
    print(f"■ 지역×음식 상위 {top_n} 조합 (공기 빈도순):")
    for _, r in result.head(top_n).iterrows():
        print(f"  {r['region']:<8} × {r['term']:<12} 공기 {int(r['cooccur']):>3}회  "
              f"(지역 내 비중 {r['share_in_region']:.1%})")

    # 지역 특색(distinctiveness) 뷰: 그 지역에서 유독 함께 언급되는 조합
    # share_in_region이 높을수록 해당 상권의 시그니처 메뉴/키워드일 확률이 높다
    distinct = result.sort_values(
        ["share_in_region", "cooccur"], ascending=False
    ).head(top_n)
    print(f"\n■ 지역 특색 상위 {top_n} 조합 (지역 내 비중순 — 상권 시그니처 신호):")
    for _, r in distinct.iterrows():
        print(f"  {r['region']:<8} × {r['term']:<12} 비중 {r['share_in_region']:>5.1%}  "
              f"(공기 {int(r['cooccur'])}회)")


if __name__ == "__main__":
    main()
