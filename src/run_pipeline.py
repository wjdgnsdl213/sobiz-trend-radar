"""전체 파이프라인 오케스트레이터: 수집→처리→적재를 한 번에 실행한다.

주간 배치용. 각 단계는 기존처럼 독립 모듈(`python -m src.X`)을 subprocess로 호출하므로,
단계별 독립 실행 원칙을 그대로 유지한다. 마지막에 db.ingest로 이력 DB에 누적한다.

evaluate는 수동 라벨(사람 판단)이 필요한 QA 단계라 자동 배치에서 제외한다.

실행:
  python -m src.run_pipeline            # 엔진 A + B 전체 + DB 적재
  python -m src.run_pipeline --only-a   # 엔진 A만
  python -m src.run_pipeline --only-b   # 엔진 B만
  python -m src.run_pipeline --skip-collect  # 수집 건너뛰고 기존 원본으로 재처리

주 1회 자동 실행은 Windows 작업 스케줄러에 등록한다(README 참고).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime

# Windows 콘솔(cp949)에서 특수문자 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8")

# 엔진별 실행 순서 (모듈명, 표시명)
ENGINE_A = [
    ("collect", "뉴스 수집"),
    ("filter", "임베딩 필터 + 중복제거"),
    ("extract", "키워드 추출"),
    ("trend", "급상승 스코어"),
]
ENGINE_B = [
    ("buzz_collect", "블로그 수집"),
    ("buzz_extract", "신조어 발굴"),
    ("buzz_validate", "DataLab 교차 검증"),
    ("cross_region", "지역×음식 크로스 분석"),
]


def run_step(module: str, label: str) -> tuple[bool, float]:
    """단계 모듈을 subprocess로 실행한다. (성공여부, 소요초) 반환."""
    print(f"\n{'=' * 60}\n▶ {label}  (src.{module})\n{'=' * 60}")
    start = time.time()
    # 하위 프로세스 출력을 그대로 흘려보내 스케줄러 로그에 남긴다
    result = subprocess.run([sys.executable, "-m", f"src.{module}"])
    elapsed = time.time() - start
    ok = result.returncode == 0
    mark = "✅" if ok else "❌"
    print(f"{mark} {label} 완료 ({elapsed:.0f}초, exit={result.returncode})")
    return ok, elapsed


def run_engine(steps: list[tuple[str, str]], name: str,
               skip_collect: bool) -> list[tuple[str, bool, float]]:
    """한 엔진의 단계들을 순차 실행한다. 앞 단계 실패 시 그 엔진은 중단."""
    print(f"\n\n{'#' * 60}\n#  엔진 {name} 시작\n{'#' * 60}")
    log: list[tuple[str, bool, float]] = []
    for module, label in steps:
        if skip_collect and module in ("collect", "buzz_collect"):
            print(f"\n⏭  {label} 건너뜀 (--skip-collect)")
            continue
        ok, elapsed = run_step(module, label)
        log.append((label, ok, elapsed))
        if not ok:
            print(f"\n⚠ {label} 실패 — 엔진 {name} 나머지 단계를 중단합니다.")
            break
    return log


def main() -> None:
    parser = argparse.ArgumentParser(description="전체 파이프라인 배치 실행")
    parser.add_argument("--only-a", action="store_true", help="엔진 A만 실행")
    parser.add_argument("--only-b", action="store_true", help="엔진 B만 실행")
    parser.add_argument("--skip-collect", action="store_true",
                        help="수집 단계 건너뛰고 기존 원본으로 재처리")
    parser.add_argument("--no-ingest", action="store_true",
                        help="마지막 DB 적재 단계 건너뜀")
    args = parser.parse_args()

    run_a = not args.only_b
    run_b = not args.only_a
    run_date = datetime.now().strftime("%Y-%m-%d")

    print(f"파이프라인 배치 시작 — {run_date}  "
          f"(엔진 {'A' if run_a else ''}{'B' if run_b else ''})")
    t0 = time.time()
    full_log: list[tuple[str, bool, float]] = []

    if run_a:
        full_log += run_engine(ENGINE_A, "A", args.skip_collect)
    if run_b:
        full_log += run_engine(ENGINE_B, "B", args.skip_collect)

    # 산출물이 하나라도 갱신됐으면 DB에 누적 적재
    if not args.no_ingest:
        ok, elapsed = run_step("db", "이력 DB 적재")
        full_log.append(("이력 DB 적재", ok, elapsed))

    # 최종 요약
    total = time.time() - t0
    print(f"\n\n{'=' * 60}\n■ 배치 요약  (총 {total / 60:.1f}분)\n{'=' * 60}")
    for label, ok, elapsed in full_log:
        mark = "✅" if ok else "❌"
        print(f"  {mark}  {label:<24} {elapsed:>6.0f}초")

    failed = [l for l, ok, _ in full_log if not ok]
    if failed:
        print(f"\n⚠ 실패 단계 {len(failed)}개: {', '.join(failed)}")
        sys.exit(1)
    print("\n✅ 전체 파이프라인 정상 완료")


if __name__ == "__main__":
    main()
