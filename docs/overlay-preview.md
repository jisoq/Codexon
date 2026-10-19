# 설치 없이 오버레이 확인하기

소스의 오버레이를 별도 창에서 실행합니다. 설치된 Codexon 교체나 종료는 필요하지 않습니다.
설정은 임시 폴더에 저장합니다. 실제 세션, 데이터베이스, 수집기, 훅은 변경하지 않습니다.

저장소의 `.venv`가 `requirements-release.lock`과 일치해야 합니다. 실행기는 환경을 검사하며 설치하거나 다운로드하지 않습니다.
환경이 없으면 실행기가 준비 명령을 안내합니다.

1. 저장소 루트에서 PowerShell을 엽니다.
2. 다음 명령으로 미리보기를 실행합니다.

```powershell
.\tools\Preview-Overlay.ps1
```

창에서 언어, 테마, 시나리오를 바꿀 수 있습니다. `multiagent`는 메인 작업과 하위 에이전트 여섯 개를 표시합니다.
각 호출은 실제 분석 엔진과 오버레이 요약기를 통과합니다. 표시된 기록과 금액은 합성 데이터입니다.
`active`, `idle`, `loading`, `error`는 상태를 확인합니다. `mismatch`, `unknown`, `cache-miss`, `slow`는 관측 사례를 표시합니다.
작은 창 옵션으로 좁은 화면도 확인합니다.
`multiagent`에서 `다음 호출`을 누르면 호출 주체를 순서대로 바꿉니다. 최근 호출 강조와 하위 에이전트 정렬을 확인합니다.

```powershell
.\tools\Preview-Overlay.ps1 -Theme light -Language en -Compact
.\tools\Preview-Overlay.ps1 -Scenario slow
```

미리보기 창을 닫으면 해당 오버레이도 종료됩니다.

실제 데이터는 기존 수집기의 저장된 결과를 읽기 전용으로 캡처합니다. 설치된 수집기를 새로 시작하지 않습니다.
캡처 시각을 창에 표시합니다. 작업 선택 상자에서 확인할 작업을 고릅니다.
새 데이터를 확인하려면 `원본 다시 읽기`를 누릅니다. 캡처한 데이터는 자동으로 갱신되지 않습니다.
첫 캡처는 기록 크기에 따라 시간이 걸립니다. 이 모드는 실행 중인 수집기의 지연을 측정하지 않습니다.

```powershell
.\tools\Preview-Overlay.ps1 -Snapshot
.\tools\Preview-Overlay.ps1 -Snapshot -Session YOUR_THREAD_ID
.\tools\Preview-Overlay.ps1 -SnapshotPath D:\path\usage-index.sqlite.codexon-collection.sqlite
```

기본 경로는 일반 데스크톱의 Codexon 수집 결과입니다. 다른 설치 경로는 `-SnapshotPath`로 지정합니다.

다음 명령은 숨겨진 데스크톱에서 실제 컨트롤을 검증합니다. 결과 폴더에 캡처와 `report.json`을 저장합니다.

```powershell
.\tools\Preview-Overlay.ps1 -Verify artifacts\overlay-preview
```

검증은 밝은 테마, 어두운 테마, 작은 창을 캡처합니다. 슬라이더 클릭과 드래그, 행 펼치기, 포인터 올리기를 확인합니다.
검증은 화면에 렌더링된 컨트롤 좌표를 사용합니다. 각 상태 변화는 기한 내 관찰 가능한 조건으로 기다립니다.
실패하면 명령은 오류 코드를 반환합니다. `report.json`에 실패 원인을 남깁니다.

# Source overlay preview

Run `tools/Preview-Overlay.ps1` from the repository root. The launcher requires `.venv` to match `requirements-release.lock` and performs no installation.
The preview owns its host window and uses disposable settings. It does not start a collector or replace the installed app.
The default scenario analyzes one parent and six synthetic children through `AnalysisEngine` and `OverlaySummaries`.
Choose a scenario, language or theme in the host. Close the host to close its overlay.

Use `-Snapshot` to capture the existing collector checkpoint through a read-only SQLite transaction.
The source analysis worker processes that captured snapshot. Select a task in the host and use capture again for newer data.
The timestamp remains visible. This static preview does not measure live collection latency and does not register another collector consumer.
Use `-SnapshotPath PATH` for a non-default checkpoint location and `-Session ID` to select a task initially.

Use `-Verify artifacts\overlay-preview` for hidden desktop verification and captures. `report.json` records the result; failures return a nonzero exit code.
Verification uses rendered control geometry and waits for observable conditions with deadlines.
