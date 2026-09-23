# CODEX·ON

[English](README.md) · [한국어](README.ko.md)

**Codex 작업을 보면서, 사용량도 함께.** Codexon은 Codex 창 위에서 호출·응답 모델명과 사용량을 확인하는 Windows 앱입니다. 세션별 토큰과 API 환산 비용을 분석하고, 남은 사용 한도를 살펴볼 수 있습니다.

![샘플 세션으로 실행한 Codexon 오버레이](docs/media/overlay-sample.png)

- 작업 중 최근 호출의 모델명, 토큰, 캐시율, API 환산 비용을 확인합니다.
- 세션에서 개별 호출까지 살펴보고, 확인된 하위 작업의 사용량을 세션 합계에 포함합니다.
- 남은 사용량과 초기화 시각을 확인합니다. 대시보드를 닫아도 트레이와 작업표시줄 위젯에서 잔여량을 볼 수 있습니다.

비용은 **실제 청구액이 아닌 API 가격 기준 환산액**입니다. 모델 관찰은 요청·응답에 기록된 모델명을 비교합니다. 실시간 한도 조회는 설치된 Codex의 app-server와 계정 연결을 사용합니다.

## 시작하기

1. [Releases](https://github.com/jisoq/Codexon/releases)에서 Windows x64 ZIP과 SHA-256 파일을 받습니다.
2. 해시를 확인하고 **`Codexon` 폴더 전체**를 압축 해제합니다.
3. `Codexon.exe`를 실행합니다. Python을 별도로 설치할 필요는 없습니다.

Codex 기록이 아직 없다면 Codex를 먼저 사용한 뒤 대시보드를 갱신하세요. **설정 → 일반 → 언어**에서 한국어·English를 선택하면 다음 실행부터 적용됩니다. **설정 → 세션 오버레이**에서 오버레이를 켭니다. 요청·응답 모델 관찰은 선택 기능입니다. **설정 → 프록시**에서 켜고 Codex를 다시 시작해야 연결 변경이 적용됩니다.

업데이트·제거·연결 복구와 로컬 데이터 안내는 [사용 안내](docs/user-guide.ko.md)에, 빌드·검증은 [기여 안내](CONTRIBUTING.md)에 있습니다. [보안 신고](SECURITY.md) · [MIT 라이선스](LICENSE) · [종속 라이브러리 고지](THIRD-PARTY-NOTICES.md).
