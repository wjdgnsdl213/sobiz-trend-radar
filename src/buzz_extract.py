"""엔진 B: 블로그 코퍼스에서 통계 기반으로 신조어/유행어 후보를 발굴한다.

방법:
  1) soynlp WordExtractor로 사전 없이 응집도 높은 '단어'를 추출 (신조어도 포착)
  2) 소금빵/맛집으 같은 조사·잘림 파편 제거(끝 파편 + 접두 파편 dedup)
  3) kiwipiepy .oov(사전 미등재)로 단어 유형 분류
     - 신조어: OOV 형태소 포함 (왁뿌, 탕후루, 빵지순례) ← 진짜 신조어
     - 합성어: 기존어 결합 (소금빵, 오션뷰, 흑돼지)
     - 일반어: 사전 단일어 (디저트, 두바이)
  4) 주 단위 급상승 스코어로 최근 뜨는 후보를 상위에 올림

실행: python -m src.buzz_extract
입력: data/raw_blog 최신 jsonl
출력: data/processed/buzz_candidates.csv
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from src.collect import load_config

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

OUT_PATH = Path("data/processed/buzz_candidates.csv")
NOUN_TAGS = {"NNG", "NNP"}
_HANGUL_RE = re.compile(r"[^가-힣A-Za-z0-9]+")


def latest_blog_file(raw_dir: str = "data/raw_blog") -> Path:
    files = sorted(Path(raw_dir).glob("blog_*.jsonl"))
    if not files:
        sys.exit("data/raw_blog에 수집 파일이 없습니다. 먼저 python -m src.buzz_collect 를 실행하세요.")
    return files[-1]


def normalize(text: str) -> str:
    """한글/영문/숫자 외 문자를 공백으로 치환해 soynlp 학습 문장을 정리한다."""
    return _HANGUL_RE.sub(" ", text).strip()


def extract_word_scores(sentences: list[str], cfg: dict[str, Any]) -> dict[str, float]:
    """soynlp WordExtractor로 단어 후보와 응집도(cohesion_forward)를 얻는다."""
    from soynlp.word import WordExtractor

    we = WordExtractor(min_frequency=cfg["min_frequency"])
    we.train(sentences)
    scores = we.extract()
    # cohesion_forward를 대표 점수로 사용
    return {w: s.cohesion_forward for w, s in scores.items()}


def build_pos_analyzer():
    """kiwipiepy 기반 판별기를 만든다.

    반환 analyze(word) -> (is_all_noun, word_type, last_form)
    - is_all_noun: 모든 토큰이 명사면 True (조사·어미 파편 1차 제거용)
    - word_type: OOV(사전 미등재) 기반 단어 유형
        · 신조어: OOV 토큰을 하나라도 포함 (kiwi가 모르는 신생어. 예: 왁뿌, 탕후루, 빵지순례)
        · 합성어: 2토큰 이상이며 모두 사전어 (기존어 조합. 예: 소금빵, 오션뷰, 흑돼지)
        · 일반어: 사전에 있는 단일어 (예: 디저트, 두바이)
    - last_form: 마지막 토큰 형태 (끝 파편 판별용)
    """
    from kiwipiepy import Kiwi

    kiwi = Kiwi()

    def analyze(word: str) -> tuple[bool, str, str]:
        tokens = kiwi.tokenize(word)
        if not tokens:
            return False, "일반어", ""
        is_all_noun = all(t.tag in NOUN_TAGS for t in tokens)
        has_oov = any(t.oov for t in tokens)
        if has_oov:
            word_type = "신조어"        # kiwi 사전에 없는 신생 형태소 포함
        elif len(tokens) >= 2:
            word_type = "합성어"        # 기존어 결합 (신조어 아님)
        else:
            word_type = "일반어"        # 사전 단일어
        return is_all_noun, word_type, tokens[-1].form

    return analyze


def tokenize_posts(df: pd.DataFrame, word_scores: dict[str, float]) -> pd.Series:
    """soynlp MaxScoreTokenizer로 각 포스트를 단어 단위로 쪼갠다 (조사 자연 분리)."""
    from soynlp.tokenizer import MaxScoreTokenizer

    tokenizer = MaxScoreTokenizer(scores=word_scores)
    # 포스트별 등장 단어 집합 (문서 빈도 계산용, 중복 카운트 방지)
    return df["norm_text"].map(lambda t: set(tokenizer.tokenize(t)))


def weekly_doc_freq(candidates: set[str], df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """후보 단어별 주차 문서 빈도(그 단어를 토큰으로 포함한 포스트 수)를 계산한다."""
    weeks = pd.to_datetime(df["pub_date"]).dt.isocalendar()
    df = df.assign(week=weeks["year"].astype(str) + "-W" + weeks["week"].astype(str).str.zfill(2))
    all_weeks = sorted(df["week"].unique())

    # 주차별로 각 후보 토큰이 등장한 포스트 수 집계
    counts: dict[str, dict[str, int]] = {c: {w: 0 for w in all_weeks} for c in candidates}
    for week, tokens in zip(df["week"], df["tokens"]):
        for tok in tokens & candidates:
            counts[tok][week] += 1

    rows = [
        {"candidate": c, "total_freq": sum(wk.values()), **wk} for c, wk in counts.items()
    ]
    return pd.DataFrame(rows), all_weeks


def main() -> None:
    cfg = load_config()["buzz"]
    stopwords = set(cfg["stopwords"])
    in_path = latest_blog_file()
    print(f"입력: {in_path}")

    df = pd.read_json(in_path, lines=True)
    df["norm_text"] = (df["title"].fillna("") + " " + df["description"].fillna("")).map(normalize)
    print(f"블로그 포스트 {len(df)}건 로드")

    # 1) soynlp 단어 후보 추출 + 토크나이저로 포스트를 단어 단위로 분해
    word_scores = extract_word_scores(df["norm_text"].tolist(), cfg)
    print(f"soynlp 단어 후보 {len(word_scores)}개 추출")
    df["tokens"] = tokenize_posts(df, word_scores)

    # 2) 길이·응집도·불용어 + 품사(모든 토큰 명사) + 끝 파편 필터
    analyze = build_pos_analyzer()
    trailing_stops = set(cfg.get("trailing_stopchars", []))
    word_type_map: dict[str, str] = {}
    candidates: set[str] = set()
    for w, coh in word_scores.items():
        if not (cfg["min_length"] <= len(w) <= cfg["max_length"]):
            continue
        if coh < cfg["min_cohesion"] or w in stopwords or w.isdigit():
            continue
        is_all_noun, word_type, last_form = analyze(w)
        if not is_all_noun:  # 조사·어미·용언 파편 제외
            continue
        # 끝이 조사/불완전 한 글자면 파편 (맛집으, 국내여, 베이커리카)
        if len(w) >= 2 and len(last_form) == 1 and last_form in trailing_stops:
            continue
        word_type_map[w] = word_type
        candidates.add(w)
    print(f"필터 통과 후보 {len(candidates)}개 (길이·응집도·불용어·품사·끝파편)")

    # 3) 주 단위 빈도 + 급상승 스코어
    freq_df, all_weeks = weekly_doc_freq(candidates, df)
    freq_df["cohesion"] = freq_df["candidate"].map(word_scores)

    # 접두 파편 제거: a가 더 긴 후보 b의 접두이고 a 빈도 대부분이 b 안에서 나오면 파편
    ratio = cfg.get("prefix_dedup_ratio", 0.7)
    freqmap = dict(zip(freq_df["candidate"], freq_df["total_freq"]))
    cand_list = list(freqmap)
    fragments: set[str] = set()
    for a in cand_list:
        fa = freqmap[a]
        for b in cand_list:
            if b != a and len(b) > len(a) and b.startswith(a) and freqmap[b] >= fa * ratio:
                fragments.add(a)
                break
    if fragments:
        freq_df = freq_df[~freq_df["candidate"].isin(fragments)].reset_index(drop=True)
        print(f"접두 파편 {len(fragments)}개 제거 → 후보 {len(freq_df)}개")

    if len(all_weeks) >= 2:
        target, baseline = all_weeks[-1], all_weeks[-1 - cfg.get("baseline_weeks", 3) : -1]
        this_week = freq_df[target]
        base_avg = freq_df[baseline].mean(axis=1) if baseline else 0
        freq_df["this_week_freq"] = this_week
        freq_df["baseline_avg"] = (base_avg if isinstance(base_avg, pd.Series) else 0)
        freq_df["trend_score"] = (this_week / (freq_df["baseline_avg"] + 1)).round(3)
    else:
        freq_df["trend_score"] = float("nan")

    # 4) OOV 기반 단어 유형: 신조어(OOV 포함) / 합성어(기존어 결합) / 일반어(사전 단일어)
    freq_df["word_type"] = freq_df["candidate"].map(word_type_map)
    freq_df["is_neologism"] = freq_df["word_type"] == "신조어"  # 하위호환(진짜 신조어만)

    freq_df = freq_df.sort_values("total_freq", ascending=False).reset_index(drop=True)
    cols = ["candidate", "total_freq", "cohesion", "word_type", "is_neologism", "trend_score"]
    freq_df[cols + all_weeks].to_csv(OUT_PATH, index=False, encoding="utf-8-sig")
    print(f"저장 → {OUT_PATH}\n")

    type_tag = {"신조어": "🆕신조어", "합성어": "🧩합성어", "일반어": "  일반어"}
    top_n = cfg["top_n"]
    print(f"■ 빈도 상위 {top_n} 후보 (유형: 🆕신조어=OOV / 🧩합성어=기존어결합 / 일반어):")
    for _, r in freq_df.head(top_n).iterrows():
        print(f"  {type_tag[r['word_type']]}  {r['candidate']:<12} 빈도 {int(r['total_freq']):>4}  응집 {r['cohesion']:.2f}")

    neo = freq_df[freq_df["is_neologism"]].sort_values("trend_score", ascending=False)
    print(f"\n■ 급상승 🆕신조어 후보 상위 {top_n} (OOV 포함, trend_score 기준):")
    for _, r in neo.head(top_n).iterrows():
        print(f"  {r['candidate']:<12} 빈도 {int(r['total_freq']):>4}  "
              f"이번주 {int(r['this_week_freq']):>3}  스코어 {r['trend_score']}")


if __name__ == "__main__":
    main()
