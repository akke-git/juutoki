Windows x64용 juutoki입니다. 아래 Assets의 **juutoki-windows-x64.zip**은 GitHub 로그인 없이 다운로드할 수 있습니다.

1. ZIP 전체를 압축 해제합니다.
2. `juutoki` 폴더의 `juutoki.exe`를 실행합니다. `_internal` 폴더도 함께 보관하세요.
3. 서버가 시작되면 기본 브라우저가 자동으로 열립니다. 사용 중에는 콘솔 창을 유지하고, 종료할 때 Ctrl+C를 누릅니다.

Python이나 Codex 설치는 필요 없습니다. 기본 요청 간격은 30초이며, 완료한 작업의 전체 회차 목록을 복원해 다음 범위를 지정할 수 있습니다.

기존 배포용 DB(1~299화와 작업 기록)가 포함되어 있으며, 새 PC 첫 실행에만 복원합니다. 이미 저장된 사용자 DB는 덮어쓰지 않습니다. 사용자 데이터는 `%LOCALAPPDATA%\juutoki`에 저장됩니다.

GitHub Windows 빌드의 테스트와 EXE 자체 실행 검증을 통과한 결과물입니다.
