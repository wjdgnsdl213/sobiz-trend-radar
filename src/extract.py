"""KeyBERT 기반 키워드 추출.

kiwipiepy로 명사 후보(연속 명사는 n-gram으로 결합)를 뽑고,
KeyBERT로 문서-키워드 임베딩 유사도 랭킹을 매긴다.

실행: python -m src.extract
입력: data/processed/filtered.parquet (kept=True 행만 사용)
출력: data/processed/doc_keywords.parquet  (문서별 상위 키워드)
      data/processed/top_keywords.csv     (코퍼스 전체 집계)
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

IN_PATH = Path("data/processed/filtered.parquet")
DOC_OUT = Path("data/processed/doc_keywords.parquet")
TOP_OUT = Path("data/processed/top_keywords.csv")
NOUN_TAGS = {"NNG", "NNP"}  # 일반 명사, 고유 명사


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_noun_analyzer(
    stopwords: set[str], max_ngram: int
) -> Callable[[str], list[str]]:
    """kiwipiepy 기반 명사 n-gram 분석기를 만든다 (CountVectorizer용).

    연속으로 등장한 명사는 하나의 후보구("배달 수수료")로도 결합한다.
    불용어가 포함된 후보는 제외한다.
    """
    from kiwipiepy import Kiwi  # 로딩이 느려 지연 임포트

    kiwi = Kiwi()

    def analyzer(text: str) -> list[str]:
        tokens = kiwi.tokenize(text)
        # 연속 명사 구간(run)으로 묶는다
        runs: list[list[str]] = []
        current: list[str] = []
        for t in tokens:
            if t.tag in NOUN_TAGS and len(t.form) >= 2 and t.form not in stopwords:
                current.append(t.form)
            else:
                if current:
                    runs.append(current)
                current = []
        if current:
            runs.append(current)

        # 각 run에서 1~max_ngram 길이의 n-gram 후보 생성
        candidates: list[str] = []
        for run in runs:
            for n in range(1, max_ngram + 1):
                for i in range(len(run) - n + 1):
                    candidates.append(" ".join(run[i : i + n]))
        return candidates

    return analyzer


def extract_keywords(
    docs: list[str], cfg: dict[str, Any], stopwords: set[str]
) -> list[list[tuple[str, float]]]:
    """문서별로 KeyBERT 상위 키워드를 추출한다."""
    from keybert import KeyBERT
    from sentence_transformers import SentenceTransformer
    from sklearn.feature_extraction.text import CountVectorizer

    model = SentenceTransformer(cfg["model"])
    kw_model = KeyBERT(model=model)
    vectorizer = CountVectorizer(
        analyzer=build_noun_analyzer(stopwords, cfg["keyphrase_ngram_max"]),
        lowercase=False,
    )
    return kw_model.extract_keywords(
        docs, vectorizer=vectorizer, top_n=cfg["per_doc_top_n"]
    )


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"{IN_PATH}가 없습니다. 먼저 python -m src.filter 를 실행하세요.")

    cfg_all = load_config()
    cfg = cfg_all["extract"]
    cfg["model"] = cfg_all["filter"]["model"]  # 필터와 동일한 임베딩 모델 사용
    stopwords = set(cfg["stopwords"])

    df = pd.read_parquet(IN_PATH)
    df = df[df["kept"]].reset_index(drop=True)
    print(f"입력: {IN_PATH} — 필터 통과 {len(df)}건")

    docs = (df["title"].fillna("") + " " + df["description"].fillna("")).str.strip()
    keywords = extract_keywords(docs.tolist(), cfg, stopwords)

    # 문서별 키워드 저장 (trend.py 입력)
    df_out = df[["link", "pub_date", "query", "title", "similarity"]].copy()
    df_out["keywords"] = [json.dumps(kws, ensure_ascii=False) for kws in keywords]
    DOC_OUT.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_parquet(DOC_OUT, index=False)

    # 코퍼스 전체 집계: 키워드가 등장한 문서 수 + 유사도 점수 합
    doc_freq: Counter[str] = Counter()
    score_sum: defaultdict[str, float] = defaultdict(float)
    for kws in keywords:
        for kw, score in kws:
            doc_freq[kw] += 1
            score_sum[kw] += score

    top = pd.DataFrame(
        [
            {"keyword": k, "doc_freq": f, "avg_score": score_sum[k] / f}
            for k, f in doc_freq.items()
        ]
    ).sort_values(["doc_freq", "avg_score"], ascending=False)
    top.to_csv(TOP_OUT, index=False, encoding="utf-8-sig")

    print(f"저장 → {DOC_OUT}, {TOP_OUT}")
    print(f"\n상위 {cfg['top_n']}개 키워드 (문서 빈도 기준):")
    for _, row in top.head(cfg["top_n"]).iterrows():
        print(f"  {row['keyword']:<20} 문서 {row['doc_freq']:>3}건  평균 유사도 {row['avg_score']:.3f}")


if __name__ == "__main__":
    main()
