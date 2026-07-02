"""필터링 성능 평가 및 PoC 리포트 생성.

사용 순서:
  1) python -m src.evaluate --make-labels
     → data/labels/labels.csv 에 무작위 샘플 생성 (필터 결과는 숨김 — 블라인드 라벨링)
     → 각 행의 label 컬럼에 소상공인 관련=1, 무관=0 입력
  2) python -m src.evaluate
     → precision/recall 계산, reports/poc_report.md 생성
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

FILTERED_PATH = Path("data/processed/filtered.parquet")
TOP_KW_PATH = Path("data/processed/top_keywords.csv")
TREND_PATH = Path("data/processed/trend_scores.csv")
REPORT_PATH = Path("reports/poc_report.md")


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_labels(cfg: dict[str, Any], force: bool) -> None:
    """블라인드 라벨링용 무작위 샘플 CSV를 생성한다 (kept/similarity 비노출)."""
    labels_path = Path(cfg["labels_path"])
    if labels_path.exists() and not force:
        sys.exit(f"{labels_path}가 이미 있습니다. 덮어쓰려면 --force를 붙이세요 (기존 라벨 소실 주의).")

    df = pd.read_parquet(FILTERED_PATH)
    sample = df.sample(cfg["sample_size"], random_state=cfg["random_seed"])
    out = sample[["link", "title", "description"]].copy()
    out["label"] = ""  # 관련=1, 무관=0
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(labels_path, index=False, encoding="utf-8-sig")  # 엑셀 호환
    print(f"라벨링 시트 생성 → {labels_path} ({len(out)}건)")
    print("각 행의 label 컬럼에 소상공인 관련=1, 무관=0을 입력한 뒤 python -m src.evaluate 를 실행하세요.")


def compute_metrics(labels: pd.DataFrame, df: pd.DataFrame) -> dict[str, Any]:
    """수동 라벨과 필터의 관련성 판정을 비교해 precision/recall을 계산한다.

    필터 본연의 역할은 관련성 판정이므로 near-duplicate 제거(kept) 이전의
    relevant 컬럼으로 평가한다. relevant가 없으면 similarity>=threshold로 대체.
    """
    threshold = load_config()["filter"]["threshold"]
    cols = ["link", "similarity"] + (["relevant"] if "relevant" in df.columns else [])
    merged = labels.merge(df[cols], on="link", how="inner")
    y_true = merged["label"].astype(int) == 1
    if "relevant" in merged.columns:
        y_pred = merged["relevant"].astype(bool)
    else:
        y_pred = merged["similarity"] >= threshold

    tp = int((y_true & y_pred).sum())
    fp = int((~y_true & y_pred).sum())
    fn = int((y_true & ~y_pred).sum())
    tn = int((~y_true & ~y_pred).sum())
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")

    return {
        "n": len(merged),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision,
        "recall": recall,
        "accuracy": (tp + tn) / len(merged),
        # 라벨과 판정이 어긋난 예시 (오류 분석용)
        "false_positives": merged[~y_true & y_pred][["title", "similarity"]],
        "false_negatives": merged[y_true & ~y_pred][["title", "similarity"]],
    }


def threshold_sweep(labels: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """라벨 샘플에 대해 threshold별 precision/recall/F1을 계산한다.

    단일 컷 수치만으로는 '필터가 되는가'를 판단하기 어렵다.
    유사도 점수 자체의 판별력을 threshold를 훑으며 확인한다.
    """
    m = labels.merge(df[["link", "similarity"]], on="link", how="inner")
    y = m["label"].astype(int) == 1
    rows: list[dict[str, Any]] = []
    for t in [round(0.30 + 0.05 * i, 2) for i in range(7)]:
        pred = m["similarity"] >= t
        tp = int((y & pred).sum())
        fp = int((~y & pred).sum())
        fn = int((y & ~pred).sum())
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
        rows.append(
            {"threshold": t, "kept": int(pred.sum()), "tp": tp, "fp": fp, "fn": fn,
             "precision": prec, "recall": rec, "f1": f1}
        )
    return pd.DataFrame(rows)


def sample_titles(df: pd.DataFrame, kept: bool, n: int, seed: int) -> list[str]:
    sub = df[df["kept"] == kept]
    return sub["title"].sample(min(n, len(sub)), random_state=seed).tolist()


def build_report(
    cfg_all: dict[str, Any], metrics: dict[str, Any], df: pd.DataFrame, sweep: pd.DataFrame
) -> str:
    """평가 수치·예시·키워드 테이블을 채운 PoC 리포트 마크다운을 생성한다."""
    seed = cfg_all["evaluate"]["random_seed"]
    threshold = cfg_all["filter"]["threshold"]
    top_n = cfg_all["extract"]["top_n"]

    kept_n, total = int(df["kept"].sum()), len(df)
    n_relevant = int(df["relevant"].sum()) if "relevant" in df.columns else kept_n
    n_dup = int(df["is_dup"].sum()) if "is_dup" in df.columns else 0
    weeks = pd.to_datetime(df["pub_date"]).dt.isocalendar()
    week_labels = weeks["year"].astype(str) + "-W" + weeks["week"].astype(str).str.zfill(2)
    week_counts = week_labels.value_counts().sort_index()

    precision_ok = metrics["precision"] >= 0.85
    verdict = "✅ 가능 (기준 충족)" if precision_ok else "❌ 기준 미달 — 아래 원인 분석 참고"

    lines: list[str] = []
    lines.append(f"# 소상공인 뉴스 키워드 트렌드 PoC 결과 리포트")
    lines.append(f"\n생성일: {datetime.now():%Y-%m-%d} / threshold={threshold}, 라벨 샘플 {metrics['n']}건\n")

    lines.append("## 1. 결론 요약\n")
    lines.append("| 판정 기준 | 목표 | 결과 | 판정 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 필터링 precision | ≥ 85% | **{metrics['precision']:.1%}** | {verdict} |")
    lines.append(f"| 키워드 품질 (상위 {top_n}개 중 관련 키워드) | ≥ 15개 | (아래 표에서 수동 판정) | ⬜ 수동 판정 필요 |")

    lines.append("\n## 2. 데이터 수집 요약\n")
    lines.append(f"- 수집: 네이버 뉴스 검색 API, 쿼리 {cfg_all['collect']['queries']}, 제목+요약문만 사용")
    lines.append(
        f"- 총 {total}건 (링크 중복 제거 후) → 관련 {n_relevant}건 "
        f"→ near-duplicate {n_dup}건 제거(보도자료 신디케이션) → 최종 분석 {kept_n}건 ({kept_n / total:.1%})"
    )
    lines.append(f"- 주차별 문서 수(필터 후 기준 아님, 전체): {dict(week_counts)}")
    lines.append("- ⚠️ API 쿼리당 1,000건 상한으로 기사량 많은 쿼리는 최근 1~2일에 편중됨 (트렌드 해석 시 주의)")

    lines.append("\n## 3. 필터링 평가 (수동 라벨 대비)\n")
    lines.append(f"현재 설정 threshold={threshold} 기준:")
    lines.append(f"- 샘플 {metrics['n']}건: TP {metrics['tp']} / FP {metrics['fp']} / FN {metrics['fn']} / TN {metrics['tn']}")
    lines.append(f"- **precision {metrics['precision']:.1%} / recall {metrics['recall']:.1%} / accuracy {metrics['accuracy']:.1%}**")

    # threshold 스윕: 유사도 점수 자체의 판별력과 precision-recall 트레이드오프
    best = sweep.loc[sweep["f1"].idxmax()]
    lines.append("\n### threshold별 성능 (precision-recall 트레이드오프)\n")
    lines.append("| threshold | 통과 | precision | recall | F1 |")
    lines.append("|---|---|---|---|---|")
    for _, r in sweep.iterrows():
        mark = " ←현재" if abs(r["threshold"] - threshold) < 1e-9 else ""
        best_mark = " ★F1최대" if r["threshold"] == best["threshold"] else ""
        lines.append(
            f"| {r['threshold']:.2f}{mark}{best_mark} | {int(r['kept'])} | "
            f"{r['precision']:.1%} | {r['recall']:.1%} | {r['f1']:.3f} |"
        )
    lines.append(
        f"\n- 핵심: precision 85% 기준은 어느 threshold에서도 안정적으로 달성되지 않음"
        f"(최고 {sweep['precision'].max():.1%}). 유사도 점수는 유의미한 신호를 주지만"
        f"(F1 최대 {best['f1']:.3f} @ threshold {best['threshold']:.2f}), 시드 문장(소비 트렌드)과"
        f" 코퍼스(정책·금융·정치 다수)의 성격 불일치로 관련 기사도 낮은 점수를 받아 recall이 함께 무너짐."
    )

    lines.append("\n### 잘못 통과된 무관 기사 (False Positive)\n")
    if len(metrics["false_positives"]):
        for _, r in metrics["false_positives"].head(10).iterrows():
            lines.append(f"- (유사도 {r['similarity']:.3f}) {r['title']}")
    else:
        lines.append("- 없음")

    lines.append("\n### 잘못 걸러진 관련 기사 (False Negative)\n")
    if len(metrics["false_negatives"]):
        for _, r in metrics["false_negatives"].head(10).iterrows():
            lines.append(f"- (유사도 {r['similarity']:.3f}) {r['title']}")
    else:
        lines.append("- 없음")

    lines.append("\n### 필터 통과 기사 예시 10건\n")
    for t in sample_titles(df, True, 10, seed):
        lines.append(f"- {t}")
    lines.append("\n### 걸러진 기사 예시 10건 (무관 뉴스 필터링 확인용)\n")
    for t in sample_titles(df, False, 10, seed):
        lines.append(f"- {t}")

    lines.append(f"\n## 4. 상위 {top_n}개 키워드 (관련 여부를 수동으로 O/X 표기)\n")
    lines.append("| # | 키워드 | 문서 빈도 | 평균 유사도 | 관련 여부 |")
    lines.append("|---|---|---|---|---|")
    if TOP_KW_PATH.exists():
        top_kw = pd.read_csv(TOP_KW_PATH).head(top_n)
        for i, (_, r) in enumerate(top_kw.iterrows(), 1):
            lines.append(f"| {i} | {r['keyword']} | {r['doc_freq']} | {r['avg_score']:.3f} |  |")

    lines.append("\n## 5. 급상승 키워드 상위 20\n")
    lines.append("| 키워드 | 이번 주 | 직전 3주 평균 | 스코어 |")
    lines.append("|---|---|---|---|")
    if TREND_PATH.exists():
        trend = pd.read_csv(TREND_PATH).head(20)
        for _, r in trend.iterrows():
            lines.append(f"| {r['keyword']} | {r['this_week_freq']} | {r['baseline_avg']} | {r['trend_score']} |")
    lines.append("\n⚠️ 대상 주(W27)에 문서가 편중되어 스코어가 전반적으로 부풀려짐 — API 1,000건 상한이 원인. "
                 "정기(주 1회) 수집을 누적하면 해소 가능.")

    lines.append("\n## 6. 한계 및 향후 확장\n")
    lines.append("- 신조어 미인식(형태소 분석기 사전 한계) — 소비 유행어(예: 신메뉴명)는 뉴스+형태소 조합으로 잘 안 잡힘. 블로그+통계 기반 추출(엔진 B)로 별도 검증 예정")
    lines.append(f"- near-duplicate {n_dup}건을 임베딩 유사도 {cfg_all['filter']['dedup_threshold']}로 제거했으나, "
                 "패러프레이즈된 보도자료(0.92 미만)는 일부 잔존 — 상위 키워드에 특정 기업 홍보 이벤트(예: 우리금융 미소금융)가 남을 수 있음")
    lines.append("- 제목+요약문만 사용(본문 크롤링 금지) — 본문에만 있는 키워드 누락")
    lines.append("- 트렌드 스코어는 대상 주 문서 편중(API 1,000건 상한)으로 부풀려짐 — 정기 수집 누적으로 해소")
    lines.append("- 과거 데이터 확보에는 빅카인즈 API 등 대체 소스 필요")
    lines.append("- 향후 확장(범위 밖): 대시보드, 실시간 수집 스케줄러, 분류기 fine-tuning, NER 기반 지역×음식 분석, DB 연동")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="필터링 평가 및 리포트 생성")
    parser.add_argument("--make-labels", action="store_true", help="라벨링용 무작위 샘플 CSV 생성")
    parser.add_argument("--force", action="store_true", help="기존 labels.csv 덮어쓰기 허용")
    args = parser.parse_args()

    cfg_all = load_config()
    cfg = cfg_all["evaluate"]

    if not FILTERED_PATH.exists():
        sys.exit(f"{FILTERED_PATH}가 없습니다. 먼저 python -m src.filter 를 실행하세요.")

    if args.make_labels:
        make_labels(cfg, args.force)
        return

    labels_path = Path(cfg["labels_path"])
    if not labels_path.exists():
        sys.exit(f"{labels_path}가 없습니다. 먼저 python -m src.evaluate --make-labels 를 실행하세요.")

    labels = pd.read_csv(labels_path, encoding="utf-8-sig")
    labels = labels[pd.to_numeric(labels["label"], errors="coerce").notna()].copy()
    labels["label"] = labels["label"].astype(int)
    if labels.empty:
        sys.exit("label 컬럼이 비어 있습니다. 관련=1, 무관=0을 입력한 뒤 다시 실행하세요.")
    print(f"라벨 {len(labels)}건 로드")

    df = pd.read_parquet(FILTERED_PATH)
    metrics = compute_metrics(labels, df)
    sweep = threshold_sweep(labels, df)
    print(f"precision {metrics['precision']:.1%} / recall {metrics['recall']:.1%} "
          f"(TP {metrics['tp']}, FP {metrics['fp']}, FN {metrics['fn']}, TN {metrics['tn']})")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(build_report(cfg_all, metrics, df, sweep), encoding="utf-8")
    print(f"리포트 저장 → {REPORT_PATH}")


if __name__ == "__main__":
    main()
