"""임베딩 기반 무관 뉴스 필터링.

시드 문장 centroid와 기사(제목+요약문) 임베딩의 코사인 유사도를 계산해
threshold 미만 기사를 걸러낸다.

실행:
  python -m src.filter              # 필터링 후 data/processed/filtered.parquet 저장
  python -m src.filter --tune       # threshold 튜닝용 유사도 분포·구간별 샘플 출력
  python -m src.filter --input data/raw/news_xxx.jsonl   # 입력 파일 직접 지정
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

THRESHOLD_GRID = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]  # 튜닝 리포트용 후보
SAMPLE_BANDS = [(0.25, 0.35), (0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 1.01)]


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def latest_raw_file(raw_dir: str = "data/raw") -> Path:
    """data/raw에서 가장 최근 수집 파일을 찾는다."""
    files = sorted(Path(raw_dir).glob("news_*.jsonl"))
    if not files:
        sys.exit("data/raw에 수집 파일이 없습니다. 먼저 python -m src.collect 를 실행하세요.")
    return files[-1]


def load_articles(path: Path) -> pd.DataFrame:
    df = pd.read_json(path, lines=True)
    # 수집 단계 중복이 남아 있을 경우를 대비한 안전망 (link가 하위 단계 조인 키)
    df = df.drop_duplicates("link").reset_index(drop=True)
    # 제목+요약문을 하나의 문장으로 합쳐 임베딩 입력으로 사용
    df["text"] = (df["title"].fillna("") + " " + df["description"].fillna("")).str.strip()
    return df


def load_seeds(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        seeds = [line.strip() for line in f if line.strip()]
    if not seeds:
        sys.exit(f"시드 문장이 비어 있습니다: {path}")
    return seeds


def compute_similarity(
    texts: list[str], model_name: str, seeds: list[str]
) -> tuple[np.ndarray, np.ndarray]:
    """시드 centroid와의 코사인 유사도, 그리고 문서 임베딩 행렬을 함께 반환한다."""
    from sentence_transformers import SentenceTransformer  # 로딩이 느려 지연 임포트

    model = SentenceTransformer(model_name)
    # 정규화된 임베딩끼리의 내적 = 코사인 유사도
    seed_emb = model.encode(seeds, normalize_embeddings=True)
    centroid = seed_emb.mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    doc_emb = model.encode(
        texts, normalize_embeddings=True, batch_size=64, show_progress_bar=True
    )
    return doc_emb @ centroid, doc_emb


def flag_near_duplicates(emb: np.ndarray, threshold: float) -> np.ndarray:
    """near-duplicate 마스크를 반환한다 (True=중복으로 제거 대상).

    같은 보도자료를 여러 매체가 받아쓴 신디케이션 기사가 코퍼스를 도배해
    키워드·트렌드를 왜곡하는 것을 막는다. 각 중복 군집에서 첫 문서만 남긴다.
    정규화된 임베딩이므로 내적이 곧 코사인 유사도.
    """
    n = len(emb)
    sim = emb @ emb.T
    is_dup = np.zeros(n, dtype=bool)
    for i in range(n):
        if is_dup[i]:
            continue
        # i 이후 문서 중 i와 임계값 이상 유사한 것을 중복 처리
        later = sim[i, i + 1 :] >= threshold
        is_dup[i + 1 :][later & ~is_dup[i + 1 :]] = True
    return is_dup


def print_tuning_report(df: pd.DataFrame, threshold: float) -> None:
    """유사도 분포와 경계 구간 샘플을 출력해 threshold 판단을 돕는다."""
    sim = df["similarity"]
    print(f"\n=== 유사도 분포 (전체 {len(df)}건) ===")
    print(f"min {sim.min():.3f} / 25% {sim.quantile(0.25):.3f} / median {sim.median():.3f} "
          f"/ 75% {sim.quantile(0.75):.3f} / max {sim.max():.3f}")

    print("\nthreshold별 통과 건수:")
    for t in THRESHOLD_GRID:
        n = int((sim >= t).sum())
        marker = " ← 현재 설정" if abs(t - threshold) < 1e-9 else ""
        print(f"  {t:.2f} → {n:5d}건 ({n / len(df) * 100:5.1f}%){marker}")

    print("\n구간별 샘플 제목 (경계 판단용):")
    for lo, hi in SAMPLE_BANDS:
        band = df[(sim >= lo) & (sim < hi)]
        print(f"\n[{lo:.2f} ~ {hi:.2f}) — {len(band)}건")
        # 구간 내 무작위 5건 (재현 가능하도록 시드 고정)
        for title in band["title"].sample(min(5, len(band)), random_state=42):
            print(f"  - {title[:70]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="임베딩 기반 관련성 필터링")
    parser.add_argument("--input", type=Path, default=None, help="입력 jsonl (기본: data/raw 최신 파일)")
    parser.add_argument("--tune", action="store_true", help="저장 없이 threshold 튜닝 리포트만 출력")
    args = parser.parse_args()

    cfg = load_config()["filter"]
    in_path = args.input or latest_raw_file()
    print(f"입력: {in_path}")

    df = load_articles(in_path)
    seeds = load_seeds(cfg["seed_path"])
    sims, doc_emb = compute_similarity(df["text"].tolist(), cfg["model"], seeds)
    df["similarity"] = sims

    threshold: float = cfg["threshold"]
    # relevant = 관련성 판정(필터 본연의 역할, 평가는 이 컬럼으로), kept = relevant & 중복 아님
    df["relevant"] = df["similarity"] >= threshold

    if args.tune:
        print_tuning_report(df, threshold)
        return

    # 관련 기사 집합 안에서만 near-duplicate 제거 (보도자료 신디케이션 대응)
    df["is_dup"] = False
    rel_pos = np.where(df["relevant"].to_numpy())[0]
    dup_local = flag_near_duplicates(doc_emb[rel_pos], cfg["dedup_threshold"])
    df.iloc[rel_pos[dup_local], df.columns.get_loc("is_dup")] = True
    df["kept"] = df["relevant"] & ~df["is_dup"]

    out_dir = Path("data/processed")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "filtered.parquet"
    df.drop(columns=["text"]).to_parquet(out_path, index=False)

    total = len(df)
    n_rel = int(df["relevant"].sum())
    n_dup = int(df["is_dup"].sum())
    kept = int(df["kept"].sum())
    print(f"\nthreshold {threshold}: {total}건 중 관련 {n_rel}건 → "
          f"중복 {n_dup}건 제거 → 최종 {kept}건 ({kept / total * 100:.1f}%)")
    print(f"저장 → {out_path}")

    # 필터 결과 눈으로 확인용 샘플
    print("\n통과 샘플 5건:")
    for title in df[df["kept"]]["title"].sample(min(5, kept), random_state=42):
        print(f"  - {title[:70]}")
    print("\n제외 샘플 5건:")
    for title in df[~df["kept"]]["title"].sample(min(5, total - kept), random_state=42):
        print(f"  - {title[:70]}")


if __name__ == "__main__":
    main()
