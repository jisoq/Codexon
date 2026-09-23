# 변경 범위별 검증

`python tools/verify_changes.py --plan`은 마지막 성공 기록과 현재 파일 내용을 비교해 선택 이유와 검사 목록을 보여 줍니다. `python tools/verify_changes.py`가 같은 선택을 실행합니다. 커밋·staged·unstaged·신규·삭제 파일을 포함하고, 미분류 코드나 공통 테스트 설정 변경은 전체 테스트로 확대합니다. 처음 실행해 기준점이 없을 때도 전체 테스트를 실행합니다. 특정 Git 상태를 기준으로 새 검증을 시작할 때만 `--base <ref>`를 지정합니다.

매 코드 업데이트는 기록 중복·원본 보호, 부분 비용·근거, 수집 장애, 프록시 설정 보호·복구의 핵심 검사와 변경 영역 검사를 실행합니다. 수정한 테스트도 포함합니다. 모든 결과는 `artifacts/verification/<시각>/`에 저장하며, 출력에는 선택 이유·결과·실패·소요 시간만 남깁니다. 통과 후 관련 파일 내용이 그대로라면 검사를 반복하지 않습니다.

사용자가 검증 중에도 마우스를 사용할 수 있어야 합니다. 실제 커서 위치 고정, 호버·툴팁 대기, 마우스 드래그, 데스크톱 전경 창 유지에 의존하는 자동 검사는 실행하거나 추가하지 않습니다. 커서와 독립적인 로직 검사와 화면 렌더링·캡처를 사용하며, 검사 통과를 위해 사용자에게 조작 중단을 요구하지 않습니다.

배포할 실행 파일은 `python tools/verify_changes.py --package-exe <Codexon.exe>`로 확인합니다. 실행 파일의 앱·프록시 버전과 Qt 런타임을 확인하고, 임시 Codex home에 부모·하위 세션을 만들어 `--smoke-depth core`로 실제 Qt Quick 화면·검색·설정 조작·세션 비용·트레이 복원을 검사합니다. 작은 표본의 경량 검사는 대량 기록의 스크롤·성능을 증명하지 않습니다. 관련 화면 변경은 실제 클릭과 캡처를 추가하고, 공통 UI 변경은 Codexon·오버레이의 상세 검사를 실행합니다.

프록시 코드·프로토콜·배포 경로를 바꾼 경우 관련 다섯 테스트 파일과, 두 실제 배포본의 격리 교체를 확인합니다. `python tools/verify_proxy_update.py --old-exe <이전 Codexon.exe> --new-exe <새 Codexon.exe> --output artifacts/<고유 경로>`를 사용합니다. 운영 포트 8768과 운영 Codex home·DB에는 테스트를 연결하지 않습니다. 새 프록시 버전의 호환성 변경은 이전 실행 파일·설정 복구도 확인합니다.

저장 형식·공통 기반·의존성 변경, 영향 범위 불명확, 누적된 여러 영역 변경에는 전체 회귀 검사를 실행합니다. 대규모 성능 검사는 빌드와 무거운 작업을 멈춘 상태에서 따로 실행합니다. 이전 실패가 남아 있다면 새 실패와 구분하되, 단순히 제외해 성공으로 보고하지 않습니다.

설치형 배포는 `tools/Build-Installer.ps1`로 만듭니다. `-Isolated`로 빌드한 QA 설치기는 별도 설치 ID·레지스트리·바로 가기를 사용하고 운영 프록시 전환과 본체 자동 실행을 생략합니다. `tools/verify_installation.py --installer <QA 설치기> --output <새 검사 폴더>`로 신규 설치·재설치·실행 경로 전환·사용 중 제거 보류·제거를 검증합니다. `tools/verify_recovery.py --executable <CodexonRecovery.exe> --output <새 검사 폴더>`는 복구 EXE만 분리해 실제 창과 버튼을 확인합니다. 표시되고 활성화된 버튼에 Tk 표준 `<<Invoke>>` 이벤트를 한 번 전달하고, 설정 복원·인증 보존·결과 화면 캡처를 검사합니다. 물리 커서는 움직이지 않습니다. 예약 점검은 격리 홈과 포트에서 실행하고, 검사 종료 후 해당 작업만 제거합니다.


Windows UI 검사는 `python tools/run_ui_checks.py -- python tools/verify_changes.py --full`로 실행합니다. 사용자에게 표시하지 않는 임시 Windows 데스크톱에서 실제 Qt 창과 키보드 처리를 검사하며, 사용자 데스크톱으로 전환하거나 물리 커서를 움직이지 않습니다. 작은 화면의 배치는 별도 프로세스에서 `QT_QPA_PLATFORM=offscreen`으로 `test_ui.py`, `test_model_ui.py`, `test_call_transport.py`, `test_quota_detail_card.py`를 실행해 확인합니다. 창 크기는 요청값과 실제값을 구분합니다.

QA 설치기 식별은 파일 이름이 아닌 EXE의 ProductName `Codexon QA`로 검사합니다. `tools/prepare_bad_runtime.py`로 만든 실패용 페이로드도 반드시 `-Isolated`로 컴파일하고, `verify_installation.py --broken-installer <실패용 QA 설치기>`에 전달합니다. 첫 설치 실패 후 제거, 업데이트 실행 검사 실패, 설치 기록 저장 실패 시 레지스트리·바로 가기·설치 기록 복원까지 확인합니다. 운영 Setup은 QA 도구로 실행할 수 없습니다.

`tools/verify_gui_handoff.py --executable <Codexon.exe> --output <새 폴더>`는 같은 빌드의 서로 다른 설치 경로 사이에서 이전 GUI 종료와 새 GUI 시작을 확인합니다. 공백이 포함된 두 홈, 별도 IPC 이름과 DB를 사용하고 자동 시작 설정은 건드리지 않습니다. 이전 버전과 새 버전의 프록시 교체는 `verify_proxy_update.py`로 별도로 확인합니다.

최종 배포 폴더에는 일반 `Codexon-Setup.exe`, 해당 체크섬, `tools/prepare_sources.py --output <배포 폴더>`로 만든 Qt/PySide6 소스 ZIP과 체크섬을 함께 준비합니다. QA 설치기는 포함하지 않습니다. manifest의 커밋·앱 버전·파일 해시를 최종 커밋과 대조하고, 검증 로그와 화면을 보존합니다.
