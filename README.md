# Codex Project Transfer

Windows에서 프로젝트 파일과 연결된 Codex 대화를 패키지로 만들고, 다른 PC의 새 폴더에 복원하는 **0.1.1 미리보기 버전**입니다. Python 표준 라이브러리와 Tkinter로 구현했습니다.

## 실행

- 빌드된 프로그램: `dist/CodexProjectTransfer/CodexProjectTransfer.exe` — Python 설치 불필요. `_internal` 폴더를 함께 유지하세요.
- 다른 PC에는 `dist/CodexProjectTransfer-Windows.zip`을 복사하고 전체 압축을 푼 뒤 실행하세요.
- 소스 실행: Python 3.12 이상 + Tkinter에서 `python app.py` 또는 `run.cmd`
- **대화 이어가기에는 별도로 설치된 Codex CLI와 대상 PC에서의 로그인이 필요합니다.**

## 다른 PC로 옮기기

1. 원본 PC에서 관련 Codex 작업을 멈추고 프로젝트 폴더와 Codex 저장소를 선택합니다.
2. **프로젝트 분석**으로 연결된 대화를 확인하고, 인수인계 메모에 현재 목표와 다음 작업을 기록합니다.
3. **이동 패키지 만들기**를 누르고 프로젝트 바깥에 `.codextransfer` 파일을 저장합니다.
4. 패키지와 실행 파일을 대상 PC로 복사합니다.
5. 대상 PC에서 **복원 · 이어가기** 탭의 패키지와 존재하지 않는 새 복원 폴더를 지정합니다.
6. **검증 후 복원**을 실행합니다. `project/`, `codex/`, `reference/`가 생성됩니다.
7. **이 저장소에 로그인**한 다음, 대화를 선택하고 **선택 대화 이어가기**를 누릅니다.

앱은 해당 프로세스에만 `CODEX_HOME=<복원폴더>/codex`를 지정하고 `codex resume <대화ID> -C <복원폴더>/project`를 실행합니다. 일반 Codex 저장소와 로그인 정보를 덮어쓰지 않습니다. 나중에 다시 실행할 때는 **복원된 작업 열기**에서 복원 폴더를 선택합니다. 복원 후 폴더를 이동하면 절대 경로가 달라지므로 패키지를 새 위치에 다시 복원하세요.

## 저장과 기억의 차이

| 종류 | 위치/의미 | 이번 버전 |
|---|---|---|
| 프로젝트 파일 | 사용자가 선택한 작업 디렉터리 | 포함 |
| 대화 원본 | Codex 저장소의 `sessions`, `archived_sessions` JSONL | 프로젝트 경로에 연결된 대화 포함 |
| 채팅 인덱스 | `state_*.sqlite` | 선택 대화 행과 스키마만 복사 |
| 대화 표시 데이터 | `thread_history_*.sqlite` | 선택 대화만 패키지에 보존; 복원 시 캐시 재생성 |
| 프로젝트 지침 | `AGENTS.md`, 프로젝트 내부 `.codex` | 파일로 포함 |
| 전역 지침 | Codex 저장소의 `AGENTS.md` | `reference/global-AGENTS.md`에 참고용 보존 |
| 인수인계 메모 | 사용자가 직접 입력 | `reference/HANDOFF.md` |
| 전역 메모리·설정·플러그인 | 버전별 DB, 전역 `config.toml`, plugins 등 | 미포함 |
| 로그인 정보 | `auth.json`, 운영체제 자격 증명 | 미포함; 대상 PC에서 로그인 |

대화 기록을 옮기는 것은 모델의 영구 기억을 복사하는 것과 다릅니다. 재개 시 Codex가 저장된 대화와 요약을 바탕으로 문맥을 구성합니다. 작업 지침은 파일로 다시 읽힙니다. 인수인계 메모와 참고용 전역 지침은 자동 주입하지 않으므로 필요하면 이어간 대화에서 해당 파일을 읽도록 요청하세요.

## 구현상 제한

- **Codex 데스크톱 앱의 기존 채팅 목록 자동 통합은 지원하지 않습니다.** 독립 저장소와 CLI 재개 방식입니다.
- 내부 저장 형식은 공식 이식 규격이 아닙니다. 같은 Codex 버전을 사용하고 중요한 작업은 원본을 유지하세요. 다른 PC의 실제 로그인·모델 응답까지 자동 검증하지는 않았습니다.
- 일반 Git 체크아웃만 지원합니다. `.git` 파일을 사용하는 worktree는 내보내기를 거부합니다. 다른 작업 경로의 대화/하위 에이전트, 외부 첨부파일은 자동 추적하지 않습니다.
- `cwd` 메타데이터와 인덱스 경로만 변경합니다. 대화 본문, 도구 인수, 설정의 절대 경로는 일괄 치환하지 않습니다. 외부 실행 도구와 플러그인은 대상 PC에서 다시 설정해야 합니다.
- 원본이 작업 중이면 파일 간 일관성을 보장할 수 없습니다. 파일 변경 감지는 일부 경합만 검출합니다. 내보내기 동안 관련 작업과 파일 편집을 멈추세요.
- 기본 제외: `node_modules`, 가상환경, 캐시, 링크/junction, `.env` 계열(예제 제외), 일부 개인 키 파일. 제외 목록은 패키지 `manifest.json`에 기록됩니다. 빌드 산출물 전체를 자동 제외하지 않으므로 대규모 프로젝트는 패키지가 커질 수 있습니다.
- 패키지는 **암호화되지 않은 ZIP**입니다. 이름 기반 비밀 파일 제외는 코드·대화 속 비밀정보를 탐지하지 않습니다. 신뢰하는 경로로 전달하세요.
- 가져오기는 SHA-256, 경로 탈출, Windows 예약 이름, 중복 경로, 링크, 용량을 검사합니다. SHA-256은 전자서명이나 출처 인증이 아닙니다.
- 포함된 프로젝트 설정과 스크립트는 파일 그대로 복원됩니다. 신뢰하는 패키지만 재개하세요.

## 개발 및 검증

```powershell
python -m unittest -v test_transfer
python -m unittest -v test_codex_integration.CodexIntegration
./build.ps1 -Python 'C:\path\to\python.exe'
```

Windows용 공식 CPython으로 EXE를 빌드하세요. `build.ps1`은 프로젝트의 `.venv`에 PyInstaller를 설치합니다. 서명되지 않은 개발 빌드입니다.

주요 파일: `transfer_core.py` (이동 엔진), `app.py` (한국어 GUI), `test_transfer.py` (왕복·필터·손상·경로 테스트).

## 조사 근거

- [공식 Codex 설정 문서](https://learn.chatgpt.com/docs/config-file/config-advanced): `CODEX_HOME`, 기본 `.codex`, 설정·인증·프로젝트 지침.
- [공식 Codex CLI 문서](https://learn.chatgpt.com/docs/codex/cli): 저장된 대화의 `codex resume` 재개.
- 2026-09-18 이 PC의 저장소에서 파일 이름·SQLite 스키마·JSONL 레코드 구조를 읽기 전용으로 확인했습니다. 내부 저장 구조 관련 설명은 이 관찰에 기반하며 공식 호환성 보장이 아닙니다.

## 0.1.1 수정

Windows 확장 경로 접두사 때문에 정상 대화를 외부 경로로 오인하던 문제를 수정했습니다. 오래된 인덱스 경로는 저장소 내부에서 대화 ID가 일치하는 원본 파일을 찾으면 복구합니다. 경로 회귀 테스트를 포함해 14개 테스트를 통과했습니다.
