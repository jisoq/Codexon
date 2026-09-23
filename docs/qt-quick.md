# Qt Quick 전환

화면은 PySide6 6.11.2와 Qt Quick/QML로 렌더링합니다. `presentation.py`는 화면에 표시할 값·선택·활성 상태와 레이아웃을 가진 QObject 모델이며, QWidget을 생성하지 않습니다. `table_model.py`는 QAbstractTableModel을 통해 요청된 셀만 포맷합니다. `qml/`의 Qt Quick Controls가 실제 입력·스크롤·탭·표·대화상자를 담당합니다.

`dashboard.py`, `settings_page.py`, `quota_panel.py`, `observer_panel.py`는 기존의 표시 기준과 동작 순서를 화면 모델에 연결합니다. 집계와 가격 정책은 `analytics.py`, `analysis_engine.py`, `pricing.py`, `quota_cycles.py`에 유지됩니다. 수집·분석 프로세스와 한도 조회 스레드의 경계도 유지합니다.

`quick_runtime.py`의 QWidget은 Windows HWND 수명과 기존 트레이·작업표시줄 연결을 위한 호스트입니다. 호스트 안에는 QQuickWidget 하나가 있고, 기존 Widgets 화면을 숨겨서 실행하거나 캡처해 표시하지 않습니다. 차트와 세션 정보 패널은 QQuickPaintedItem에 기존 그리기 규칙을 이식했습니다. 오버레이 슬라이더와 접기 버튼, 작업표시줄 잔여량은 QML입니다.

QSettings의 조직·앱 이름과 키, SQLite 색인·한도 원장 형식, CLI 진입점은 유지합니다. Qt 6의 제품명 형태 화면 이름을 기존 Windows 장치 ID에 대응시켜 저장된 모니터 선택을 읽습니다. QML 장면은 네이티브 창과 Python 모델이 해제되기 전에 종료합니다.

검증에서는 기존 테스트의 가정을 그대로 유지하는 대신 실제 QML 장면에 키보드·마우스 입력을 전달합니다. `quick_qa.py`와 `tests/test_quick_ui.py`는 화면에 렌더링된 컨트롤, 열 너비 변경, 선택·스크롤 유지, 모델 관측 표기, 차단된 시그널 이후의 화면 동기화를 확인합니다. `--smoke`는 임시 설정과 별도 색인으로 주요 페이지, 검색, 표의 최신순, 트레이 복귀 및 QML 경고를 검사하고 JSON·PNG를 저장합니다.

Codexon의 PyInstaller 설정은 QML 파일과 해당 Qt 플러그인을 포함합니다. 업데이트는 실행 중인 프록시 파일을 덮어쓰지 않는 새 버전 폴더에 설치합니다. 기존 프록시 연결은 유지하고 GUI·다음 로그인 시 프록시 실행 경로를 갱신합니다. 새 패키지의 프록시 중계·보호 복구는 별도 포트와 가짜 상류 서버로 검증할 수 있습니다.

공통 `Ui*.qml`은 입력 높이, 여백, 선택·포커스 상태와 스크롤바를 통일합니다. 필터 행은 좁은 창에서 줄바꿈하며, 한도 표의 탭은 같은 표 영역에 붙어 있습니다. 표의 툴팁은 잘린 표시값만 850ms 뒤에 보여주고, 긴 설명과 관측 근거는 셀 선택 후 `셀 상세`으로 엽니다. 세션 표의 합계는 기존 집계 결과를 사용하며, 비용·토큰 표시 전환과 필터를 따릅니다. `tools/verify_design.py`는 서비스와 외부 요청 없이 긴 세션명·모델명·다수 행을 넣고 최소/기본 창 크기의 페이지, 설정, 펼친 한도 설명과 대화상자를 렌더링합니다.

빌드 시 PATH는 해당 Python 환경과 Windows 시스템 디렉터리로 제한합니다. 이름이 같은 다른 프로그램의 DLL이 섞이면 빌드를 실패시킵니다. Qt가 Windows ICU를 요구하는 경우 Poppler 등의 `icuuc.dll`을 대신 포함하면 QtCore 로딩이 실패할 수 있습니다. Codexon 실행 파일의 `--verify-runtime report.json`은 창이나 서비스를 시작하지 않고 Qt 모듈 로딩과 실제 DLL 경로를 기록합니다. QtQml이 사용하는 QtNetwork를 포함합니다.
