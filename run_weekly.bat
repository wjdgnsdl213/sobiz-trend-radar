@echo off
REM 주간 배치: 전체 파이프라인(수집→처리→적재)을 실행하고 로그를 남긴다.
REM Windows 작업 스케줄러에 이 파일을 등록한다 (README '주간 자동 실행' 참고).
REM %~dp0 = 이 배치 파일이 있는 폴더 → 스케줄러가 어디서 호출하든 경로가 맞는다.

cd /d "%~dp0"
if not exist logs mkdir logs

echo ============================================================ >> "logs\pipeline.log"
echo [BATCH START] %date% %time% >> "logs\pipeline.log"
echo ============================================================ >> "logs\pipeline.log"

REM 시스템 python 사용. 가상환경을 쓴다면 아래를 .venv\Scripts\python.exe 로 교체.
python -m src.run_pipeline >> "logs\pipeline.log" 2>&1

echo [BATCH END] %date% %time% (exit=%errorlevel%) >> "logs\pipeline.log"
