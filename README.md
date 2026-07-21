# SOOP AI 타임라인

SOOP의 신규 공개 다시보기를 소규모로 모아 보고, 선택한 영상의 음성을 로컬에서 인식한 뒤 상세 타임라인을 검수하는 Windows 데스크톱 앱입니다.

## 현재 구현된 흐름

1. 스트리머 아이디 또는 방송국 URL 등록
2. 앱 시작 시와 3시간 간격으로 공개 `다시보기` 첫 페이지 확인
3. 신규 VOD를 로컬 SQLite 목록에 중복 없이 추가
4. 자동 확인 목록과 별개로 다시보기 링크 한 건을 직접 넣어 즉시 고속 분석
5. 라이브 링크를 넣으면 연결 순간의 방송 경과시간부터 약 15초 단위 실시간 자막 작성
6. 라이브 첫 1분 이후 약 3분 간격으로 Gemini 임시 타임라인을 갱신하되, 직전 주제를 기억해 같은 토크의 반복 요약을 방지하고 종료 시 전체 흐름을 최종 정리
7. 선택한 VOD의 편집 탭 열기
8. 사용자가 선택한 공개 VOD에서 웹 플레이어용 메타데이터를 한 번 조회
9. 여러 본편 파트의 **오디오 전용 HLS**만 순서대로 읽어 10분 단위 메모리 청크로 변환
10. 다음 청크 수신과 `faster-whisper` 배치 인식을 겹쳐서 고속 로컬 전사하고, 완료된 청크의 실시간 자막과 측정 기반 예상시간 표시
11. 타임스탬프 자막만 Gemini Flash에 전달해 45분 구간별 주제 경계를 찾고, 같은 중심 토크의 배경·사례·이유·반응을 첫 시작점 한 줄로 묶은 최종 타임라인 JSON 생성
12. 전사 결과를 로컬에 캐시하여 재분석 시 재사용
13. 타임스탬프 더블클릭 시 공식 SOOP 임베드 플레이어를 열어 해당 재생 지점으로 이동
14. `Gemini 문체 교정`으로 Whisper 재분석 없이 기존 항목을 건조한 제목형·메모체로 교정
15. 결과 검수, 자동 저장, 5,000자 이하 댓글·대댓글 블록 분할 및 복사
16. 전체 댓글 블록 통합 찾기·현재 항목 변경·모두 변경·대소문자 구분·마지막 변경 되돌리기
17. EXE 실행 시 설정된 업데이트 피드를 백그라운드에서 확인하고 새 버전이 있을 때만 다운로드 페이지 안내

다시보기 고속 분석에서는 영상 스트림을 요청하지 않으며, 오디오도 파일로 저장하지 않습니다. 라이브는 SOOP이 오디오 전용 주소를 제공하지 않아 최저 화질의 영상·오디오 결합 스트림을 실시간으로 수신하지만, 메모리에서 오디오 트랙만 해독하고 미디어 파일은 저장하지 않습니다. 검수 플레이어를 열었을 때는 SOOP 공식 임베드 페이지가 일반 브라우저와 동일하게 영상을 스트리밍합니다. 어느 방식이든 원본 오디오·영상은 Gemini에 업로드하지 않고 다음 형태의 텍스트 자막만 전달합니다.

```text
s000123 | 00:09:24 | 오늘 진짜 이상한 꿈을 꿨거든요
```

Gemini가 선택한 `segment_id`를 프로그램이 원래 시간과 다시 연결하므로 AI가 임의의 타임스탬프를 만들지 않도록 구성했습니다.

기존 결과의 말투만 바꿀 때는 편집 탭의 `Gemini 문체 교정`을 누릅니다. 현재 타임라인 텍스트만 Gemini에 전달하며, 항목 수·순서·타임스탬프는 프로그램이 교정 전후를 연결해 그대로 유지합니다.

텍스트 검수 중 `찾기·바꾸기`를 열면 여러 댓글 블록을 하나의 문서처럼 검색할 수 있습니다. `Ctrl+F`는 찾기, `Ctrl+H`는 바꾸기, `F3`과 `Shift+F3`은 다음·이전 결과로 이동합니다. 일괄변경 직후에는 `변경 되돌리기`로 한 번 복원할 수 있습니다.

## 실행

PowerShell에서 다음 명령을 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python main.py
```

## Windows EXE 빌드

```powershell
.\build_exe.ps1
```

빌드가 끝나면 별도의 Python 명령 없이 실행할 수 있는 단일 파일이 `dist\SOOPTimeline.exe`에 생성됩니다. Whisper 모델은 EXE에 포함하지 않으며 첫 분석 때 선택한 모델만 사용자 캐시에 내려받습니다.

빌드할 때 `dist\update.json`도 함께 생성됩니다. 기본 앱은 [32m1nd0t/soop-ai-timeline](https://github.com/32m1nd0t/soop-ai-timeline)의 최신 GitHub Release를 확인합니다. 다른 배포 채널을 쓰려면 빌드 전에 다음 환경 변수를 지정합니다.

```powershell
$env:SOOP_TIMELINE_UPDATE_MANIFEST_URL = "https://example.com/update.json"
$env:SOOP_TIMELINE_DOWNLOAD_URL = "https://example.com/SOOPTimeline.exe"
$env:SOOP_TIMELINE_RELEASE_NOTES = "변경 내용"
.\build_exe.ps1
```

생성된 `update.json`을 첫 번째 환경 변수로 지정한 고정 HTTPS 주소에 업로드하면 그 주소가 EXE 안에 포함됩니다. `AI 설정 > 앱 업데이트`의 주소 칸은 특정 PC에서 배포 주소를 재정의할 때만 사용합니다. 앱은 EXE를 임의로 내려받거나 설치하지 않습니다.

## GitHub Release 배포

앱 버전을 `soop_timeline/__init__.py`와 `pyproject.toml`에서 함께 올리고 커밋한 뒤 EXE를 빌드합니다.

```powershell
$env:SOOP_TIMELINE_UPDATE_MANIFEST_URL = "https://api.github.com/repos/32m1nd0t/soop-ai-timeline/releases/latest"
$env:SOOP_TIMELINE_DOWNLOAD_URL = "https://github.com/32m1nd0t/soop-ai-timeline/releases/latest"
.\build_exe.ps1
```

같은 버전의 태그로 GitHub Release를 만들고 빌드 결과를 첨부합니다.

```powershell
git tag v0.3.0
git push origin v0.3.0
gh release create v0.3.0 .\dist\SOOPTimeline.exe .\dist\update.json --generate-notes
```

기존 EXE는 다음 실행 시 GitHub의 `releases/latest` API에서 더 높은 버전을 발견하면 다운로드 페이지를 안내합니다. 공개 저장소이므로 앱에 GitHub 토큰을 포함할 필요가 없습니다.

앱의 `AI 설정`에서 다음 값을 입력합니다.

- Gemini API 키: Windows 자격 증명 관리자에 암호화 보관
- Gemini 모델: 기본 `gemini-3.5-flash`
- 타임라인 밀도: 기본 `큰 주제 위주`(같은 중심 토크의 세부 내용 병합), `기본`, `촘촘하게` 선택 가능
- Whisper 모델: 기본 `large-v3-turbo`(속도 우선), 선택 가능 `large-v3`(정확도 우선)
- 연산 장치: 기본 `자동`(CUDA 런타임이 준비되면 GPU, 아니면 CPU int8)

Whisper 모델은 첫 분석 때 한 번 내려받고 이후 로컬 캐시를 사용합니다. GPU 실행에는 CUDA 12용 cuBLAS와 cuDNN 9 런타임이 추가로 필요합니다. 런타임이 없으면 `자동` 설정에서 CPU `int8`로 대체되므로 긴 영상은 느릴 수 있습니다. `NVIDIA GPU`를 명시적으로 선택하면 필요한 런타임이 없을 때 CPU로 몰래 전환하지 않고 오류를 표시합니다.

현재 Windows GPU 런타임 버전은 다음 명령으로 설치할 수 있습니다.

```powershell
.\.venv\Scripts\python -m pip install -e ".[gpu-windows]"
```

## 저장 위치

- 데이터베이스: `%LOCALAPPDATA%\SOOPTimeline\timeline.db`
- 전사 캐시: `%LOCALAPPDATA%\SOOPTimeline\analysis\<VOD 번호>\transcript.json`
- 라이브 복구용 누적 자막: `%LOCALAPPDATA%\SOOPTimeline\analysis\<라이브 세션 번호>\live-transcript.json`
- 검수 플레이어 프로필: `%LOCALAPPDATA%\SOOPTimeline\webview2`

테스트에서는 `SOOP_TIMELINE_DATA_DIR` 환경 변수로 저장 위치를 바꿀 수 있습니다.

## 현재 경계와 주의사항

- 공개 VOD 재생 페이지가 사용하는 내부 조회 엔드포인트에서 오디오 전용 재생목록을 확인합니다. 이는 공식 개발자 API가 아니므로 SOOP의 변경으로 언제든 동작이 중단될 수 있고, 사용 전 별도 허용 여부를 확인하는 것이 안전합니다.
- 신규 영상 확인과 AI 분석은 사용자의 일반 브라우저 로그인 쿠키를 읽지 않으며 비공개·유료·연령 확인 VOD와 숨김 파트는 거부합니다. 검수 플레이어는 별도의 WebView2 프로필을 사용합니다.
- 다시보기 AI 분석에는 영상 파일·영상 스트림·시스템 출력음 캡처를 사용하지 않습니다. 라이브 분석은 최저 화질 결합 스트림을 받아 오디오만 해독하므로 영상 데이터도 전송 구간에는 포함되지만 저장하거나 영상으로 처리하지 않습니다. 검수 재생은 공식 임베드 페이지의 일반 스트리밍이며 앱이 별도 영상 파일을 만들지는 않지만, WebView2가 통상적인 브라우저 캐시를 사용할 수 있습니다.
- 사용자가 선택한 VOD만 한 번씩 분석하며 대량 수집이나 무제한 병렬 요청을 하지 않습니다.
- SOOP OAuth와 공식 댓글 API 권한을 받기 전까지 자동 댓글 등록은 비활성화되어 있습니다.
- 생성된 내용은 항상 사용자가 검수한 뒤 수동으로 복사해 등록합니다.

## 테스트

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```
