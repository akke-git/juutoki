# GitHub Actions로 Windows 실행 파일 만들기

저장소: [akke-git/juutoki](https://github.com/akke-git/juutoki) (공개).
소스 파일과 승인된 초기 DB가 저장소에 직접 포함되어 있습니다.

## 소스 받기와 변경 올리기

```sh
git clone https://github.com/akke-git/juutoki.git
cd juutoki
```

ZIP 압축 해제는 필요 없습니다. 소스를 수정하고 `main`에 커밋해 push하면
관련 파일 변경 시 Windows 빌드가 시작됩니다. 워크플로는 체크아웃한 소스를 바로 사용합니다.

## 실행 파일 받기

1. 저장소의 **Actions → Build Windows executable**을 엽니다.
2. 필요하면 **Run workflow → main → Run workflow**를 누릅니다.
3. 초록색으로 성공한 실행의 **Artifacts → juutoki-windows-x64**를 다운로드합니다.
4. 받은 ZIP 전체를 압축 해제하고 `juutoki.exe`를 실행합니다.

GitHub는 테스트, Chromium 설치, PyInstaller 빌드, 실제 EXE의 오프라인 Chromium 실행 확인을
완료한 경우에만 결과물을 올립니다. Windows 실행 파일은 폴더형 배포이며 `_internal`을 함께 보관해야 합니다.
별도 Python·Codex 설치는 필요 없습니다. 설치 마법사나 코드 서명은 아직 포함하지 않습니다.

## DB와 이어받기

- `seed/newt.sqlite3`는 SQLite backup으로 만든 일관된 스냅샷입니다.
- `seed/manifest.json`에 스냅샷 시각, 회차 수, 범위, 해시가 있습니다.
- 새 PC 첫 실행 시 `%LOCALAPPDATA%\juutoki\newt.sqlite3`에 초기 DB를 복사합니다.
- 기존 DB가 있으면 번들 DB로 덮어쓰지 않습니다. 업데이트해도 기존 수집 내용이 유지됩니다.
- TXT 합본은 DB에서 다시 생성합니다. 새 환경에서 과거 작업의 다운로드도 사용할 수 있습니다.
- **저장된 작품**에서 전체 TXT를 받거나, 기존 **작업 기록**의 **미완료 재시도**로 이어받을 수 있습니다.
- 인증 요구가 해소되지는 않습니다. 현재 DB의 마지막 실패 회차는 원본 사이트의 제한에 영향을 받습니다.

현재 PC에서 seed를 새로 만들려면:

```sh
.venv/bin/python scripts/create_seed.py
```

## 로컬 실행기 테스트

```sh
.venv/bin/python launcher.py
```

실행기는 기본 브라우저를 열고 로컬 서버를 유지합니다. 콘솔에서 Ctrl+C로 종료합니다.
Fedora에서도 소스 실행은 가능하지만 이 워크플로가 Fedora RPM을 만드는 것은 아닙니다.

## 검증 범위

Linux에서의 검증만으로 Windows EXE 성공을 주장할 수 없습니다. 실제 Actions 실행이 성공한 뒤
Artifacts가 생성됐는지 확인해야 합니다. GitHub 계정 연결이나 업로드가 안 됐다면 설정 준비까지만 완료된 상태입니다.
