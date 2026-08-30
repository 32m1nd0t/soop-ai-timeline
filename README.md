# SOOP AI 타임라인

SOOP의 신규 공개 다시보기를 소규모로 모아 보고, 선택한 영상의 음성을 로컬에서 인식한 뒤 상세 타임라인을 검수하는 Windows 데스크톱 앱입니다.

> 이 프로젝트는 SOOP이 제작·승인·후원한 공식 앱이 아니며 SOOP과 제휴 관계가 없습니다. `SOOP` 명칭은 호환 대상 서비스를 식별하기 위해서만 사용합니다.

## 현재 구현된 흐름

1. 스트리머 아이디 또는 방송국 URL 등록
2. 앱 시작 시와 설정한 주기(끄기/30분/1시간/3시간/6시간)로 공개 `다시보기` 첫 페이지 확인 및 신규 영상 알림
3. 신규 VOD를 로컬 SQLite 목록에 중복 없이 추가
4. 자동 확인 목록과 별개로 다시보기 링크 한 건을 직접 넣어 즉시 고속 분석
5. 라이브 링크를 넣으면 연결 순간의 방송 경과시간부터 약 15초 단위 실시간 자막 작성
6. 라이브는 Gemini 없이 로컬 Whisper 타임스탬프 자막만 증분 저장하고, 연결이 끊기면 지수 백오프로 자동 재연결
7. 선택한 VOD의 편집 탭 열기
8. 사용자가 선택한 공개 VOD에서 웹 플레이어용 메타데이터를 한 번 조회
9. 여러 본편 파트의 **오디오 전용 HLS**만 순서대로 읽어 10분 단위 메모리 청크로 변환
10. 다음 청크 수신과 `faster-whisper` 배치 인식을 겹쳐서 고속 로컬 전사하고, 완료된 청크의 실시간 자막과 측정 기반 예상시간 표시
11. Gemini에 타임스탬프 자막만 전달해 45분 구간별 `계속/새 주제/복귀` 경계를 판정하고, 소통·게임·대회·합방·기획 콘텐츠처럼 방송의 큰 활동 단계가 바뀌는 지점에는 빈 줄을 넣으며, 직접 인용은 로컬 자막과 다시 대조한 뒤 최종 타임라인을 확정하고 전체 방송 제목형 요약은 분리된 요청 한 번으로만 생성
12. 10분 청크마다 전사 체크포인트와 45분 단위 Gemini 결과를 저장하고, 라이브는 새 자막만 증분 저장해 중단된 장시간 VOD·라이브·분석 대기열을 다음 실행에서 재개
13. 타임스탬프 더블클릭 시 공식 SOOP 임베드 플레이어를 열어 해당 재생 지점으로 이동하며, 짧은 선행 영상이나 5시간 단위로 여러 파트가 붙은 VOD는 전체 방송 시각을 목표 파트의 로컬 시각으로 변환해 정밀 이동하고 F11·Esc 전체화면 전환과 네이티브 플레이어 자동 복구 지원
14. `AI 문체 교정`과 영상별 `주제 다시 묶기`로 Whisper 재분석 없이 문체 또는 주제 밀도만 재생성
15. 결과 검수, 자동 저장, 검수 완료 시 AI 초안과 달라진 삭제·추가·문장·시간·병합·분리·제목 사례를 스트리머별로 학습하고 다음 Gemini 분석에 관련 사례만 반영, 5,000자 이하 댓글·대댓글 블록 분할 및 복사
16. 전체 댓글 블록 통합 찾기·현재 항목 변경·모두 변경·대소문자 구분·마지막 변경 되돌리기
17. 현재 재생시간 삽입, 현재 줄 ±5초·전체 시각 보정·이전 주제와 합치기, 타임스탬프 순서·중복·범위·긴 공백 검사
18. `텍스트만 추출` 버튼으로 Gemini 없이 FW STT 자막만 생성, 정확히 선택한 줄만 직접 인용·요약으로 변환, 자동 버전 기록·복원, 타임라인 TXT 입출력, 저장된 Whisper 자막 보기·TXT/SRT 내보내기, API 호출 예상치와 실제 호출·토큰 사용량 표시
19. 회전 오류 로그와 API 키·자막 원문을 제외한 진단 정보 복사/ZIP 저장
20. 실행 시 GitHub Release를 확인하고, SHA-256 검증을 통과한 설치 파일만 동의를 받아 자동 업데이트
21. 스트리머별 탭·영상 정렬·과거 영상 30개씩 추가 불러오기, 종료된 라이브와 완성된 다시보기 자동 연결, 전체 재분석 완료 시 기존 라이브 탭을 실제 다시보기 탭으로 전환하고 라이브 자막 기록은 버전 기록에 보존, 위치를 자유롭게 옮길 수 있는 독립 검수 플레이어와 비모달 저장 자막 창
22. 스트리머별 고유명사 단어 사전, 영상 목록을 유지하는 작업 기록 초기화, 캐시 보관 기간·영상별/전체 자막 삭제, 첫 실행 데이터 처리 안내
23. 다시보기 목록 더블클릭으로 작업 탭 열기, 영상별 자동 저장 메모, 목록 숨김·복원과 완료 상태 보존
24. 공개 19세 VOD·라이브 감지 시에만 임시 WebView2 로그인/성인 인증 창을 열고, 해당 콘텐츠에 한정한 메모리 세션으로 원래 FW 자막 추출 또는 AI 분석 자동 재시도 후 즉시 폐기

다시보기 고속 분석에서는 영상 스트림을 요청하지 않으며, 오디오도 파일로 저장하지 않습니다. 라이브는 SOOP이 오디오 전용 주소를 제공하지 않아 최저 화질의 영상·오디오 결합 스트림을 실시간으로 수신하지만, 메모리에서 오디오 트랙만 해독하고 미디어 파일은 저장하지 않습니다. 검수 플레이어를 열었을 때는 SOOP 공식 임베드 페이지가 일반 브라우저와 동일하게 영상을 스트리밍합니다. 어느 방식이든 원본 오디오·영상은 Gemini에 업로드하지 않습니다. 라이브 자막은 로컬에만 저장되며, 다시보기 AI 타임라인 생성 시에만 다음 형태의 텍스트 자막을 Gemini에 전달합니다.

```text
s000123 | 00:09:24 | 오늘 진짜 이상한 꿈을 꿨거든요
```

AI가 선택한 `segment_id`를 프로그램이 원래 시간과 다시 연결하므로 AI가 임의의 타임스탬프를 만들지 않도록 구성했습니다. 직접 인용은 해당 시각 주변의 시간상 연속된 Whisper 자막과 로컬에서 전체 문장을 대조하며, 긴 침묵을 사이에 둔 문장을 이어 붙이거나 확인되지 않은 인용은 추가 Gemini 호출 없이 따옴표를 제거하고 실제 자막 문구로 되돌립니다. 스트리머 본인이 현재 방송을 끝내겠다는 종료 인사·방종 예고는 짧더라도 별도 항목으로 남기고, 요약 대신 실제 자막 발언을 직접 인용합니다.

기존 결과의 말투만 바꿀 때는 편집 탭의 `AI 문체 교정`을 누릅니다. 현재 타임라인 텍스트만 Gemini에 전달하며, 항목 수·순서·타임스탬프는 프로그램이 교정 전후를 연결해 그대로 유지합니다. `주제 다시 묶기`는 완료된 로컬 자막을 재사용하므로 Whisper를 다시 실행하지 않습니다.

`검수 완료`를 누르면 마지막 AI 생성본과 현재 완료본을 비교해 전체 제목 수정, 항목 삭제·추가, 문장 수정, 항목 병합·분리, 시간 수정 사례를 로컬 SQLite에 저장합니다. 이는 Gemini 모델 자체를 재학습하는 방식이 아니라, 다음 분석 때 같은 스트리머의 사례 중 현재 자막과 단어가 겹치는 관련 사례를 호출당 최대 5개 골라 프롬프트에 예시로 붙이는 방식입니다. 45분 구간 호출에는 현재 구간 관련 사례, 최종 병합 호출에는 후보 전체 관련 사례, 방송 제목 호출에는 제목 수정 사례만 들어갑니다. `AI 설정 > 검수 피드백 학습`에서 전송을 끄거나 저장된 사례와 비교용 초안을 초기화할 수 있습니다.

Gemini 최종 정리가 사용 한도나 일시 장애로 실패해도 완료된 구간별 결과는 삭제되지 않습니다. 편집 화면의 `최종 정리 재시도`를 누르면 45분 구간 호출을 반복하지 않고 마지막 정리 단계부터 이어집니다. 라이브 중에는 Gemini를 호출하지 않고 새 Whisper 자막만 `JSONL`에 추가합니다. 정상 종료 때 전체 JSON으로 한 번 합치며, 비정상 종료된 라이브도 마지막으로 완성된 누적 자막을 복구해 `저장 자막 다시 정리`를 나중에 실행할 수 있습니다.

텍스트 검수 중 `찾기·바꾸기`를 열면 여러 댓글 블록을 하나의 문서처럼 검색할 수 있습니다. `Ctrl+F`는 찾기, `Ctrl+H`는 바꾸기, `F3`과 `Shift+F3`은 다음·이전 결과로 이동합니다. 일괄변경 직후에는 `변경 되돌리기`로 한 번 복원할 수 있습니다.

검수 플레이어에서는 `Ctrl+Shift+T`로 현재 재생시간을 편집 위치에 넣고, `Ctrl+Space`로 재생·일시정지, `Alt+왼쪽/오른쪽`으로 10초 이동할 수 있습니다. `F11` 또는 플레이어 버튼으로 전체화면을 전환하고 `Esc`로 원래 창 크기로 돌아옵니다. 플레이어 창을 닫으면 재생 중인 WebView2도 함께 완전히 종료되며, 다시 열 때는 새 플레이어 창과 새 페이지를 만듭니다. `타임라인 검사`는 중복·역순·영상 범위 밖 시간과 30분 이상 비어 있는 구간을 알려줍니다.

## 실행

PowerShell에서 다음 명령을 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python main.py
```

## Windows 설치 프로그램 빌드

```powershell
.\build_installer.ps1
```

빌드에는 Inno Setup 6가 필요합니다. 완료되면 CPU 기본 설치본 `dist\SOOPTimeline-Setup.exe`, 선택 설치용 `dist\SOOPTimeline-GPU-Addon.exe`, 비상용 GPU 포함 휴대용 `dist\SOOPTimeline.exe`가 생성됩니다. 기본 앱은 사용자별 `%LOCALAPPDATA%\Programs\SOOPTimeline`에 폴더형으로 설치되므로 관리자 권한이 필요하지 않습니다. GPU 애드온은 CUDA DLL만 `{설치 폴더}\gpu-runtime`에 한 번 설치하며 일반 앱 업데이트가 이 폴더를 덮어쓰지 않습니다. Whisper 모델은 설치 파일에 포함하지 않으며 첫 분석 때 선택한 모델만 사용자 캐시에 내려받습니다. 검수 플레이어에는 Microsoft Edge WebView2 Runtime이 필요하며, Windows 11에는 기본 포함되고 일부 Windows 10 환경에서는 별도 설치가 필요할 수 있습니다.

배포 EXE에는 [데이터 처리 안내](PRIVACY.md), [제3자 소프트웨어 고지](THIRD_PARTY_NOTICES.md), 빌드 환경에서 확인된 런타임 의존성의 라이선스 파일을 함께 포함합니다.

빌드할 때 기본 설치본·GPU 애드온·휴대용 EXE 각각의 SHA-256이 들어간 `dist\update.json`도 생성됩니다. 기본 앱은 [32m1nd0t/soop-ai-timeline](https://github.com/32m1nd0t/soop-ai-timeline)의 최신 GitHub Release를 확인합니다. 다른 배포 채널을 쓰려면 빌드 전에 다음 환경 변수를 지정합니다.

```powershell
$env:SOOP_TIMELINE_UPDATE_MANIFEST_URL = "https://example.com/update.json"
$env:SOOP_TIMELINE_INSTALLER_URL = "https://example.com/SOOPTimeline-Setup.exe"
$env:SOOP_TIMELINE_GPU_ADDON_URL = "https://example.com/SOOPTimeline-GPU-Addon.exe"
$env:SOOP_TIMELINE_PORTABLE_URL = "https://example.com/SOOPTimeline.exe"
$env:SOOP_TIMELINE_RELEASE_NOTES = "변경 내용"
.\build_installer.ps1
```

생성된 `update.json`을 첫 번째 환경 변수로 지정한 고정 HTTPS 주소에 업로드하면 그 주소가 EXE 안에 포함됩니다. `AI 설정 > 앱 업데이트`의 주소 칸은 특정 PC에서 배포 주소를 재정의할 때만 사용합니다. 자동 업데이트는 HTTPS 설치 파일과 64자리 SHA-256이 모두 있을 때만 활성화됩니다. 파일은 `%LOCALAPPDATA%\SOOPTimeline\updates`에 임시 다운로드하고 해시 검증을 통과한 뒤에만 실행합니다. 다운로드·검증·설치 시작이 실패하면 현재 앱은 그대로 유지됩니다.

## GitHub Release 배포

앱 버전을 `soop_timeline/__init__.py`와 `pyproject.toml`에서 함께 올리고 커밋합니다.

```powershell
$env:SOOP_TIMELINE_UPDATE_MANIFEST_URL = "https://api.github.com/repos/32m1nd0t/soop-ai-timeline/releases/latest"
.\build_installer.ps1
```

`.github/workflows/release.yml`이 같은 버전의 태그를 감지해 테스트, CPU 폴더형 앱·GPU 애드온·휴대용 EXE 빌드, 휴대용·설치본 스모크 테스트, GitHub Release 첨부를 자동 수행합니다.

```powershell
git tag v0.8.2
git push origin v0.8.2
```

기존 휴대용 EXE도 다음 실행 시 더 높은 버전을 발견하면 설치 프로그램을 받아 설치형으로 전환할 수 있습니다. 이후에는 같은 설치 위치를 갱신하고 재실행합니다. 분석 DB·캐시는 설치 폴더가 아니라 `%LOCALAPPDATA%\SOOPTimeline`에 있으므로 앱 업데이트나 재설치로 삭제되지 않습니다. 공개 저장소이므로 앱에 GitHub 토큰을 포함할 필요가 없습니다.

앱의 `AI 설정`에서 다음 값을 입력합니다.

- 타임라인 AI: `Google Gemini`
- Gemini API 키: Windows 자격 증명 관리자에 보관
- 기본 모델: `gemini-flash-lite-latest`(항상 최신 Flash-Lite 사용)이며 모델명 직접 변경 가능
- Gemini 연결 테스트: 실제 소량의 구조화 출력 요청으로 키·모델 권한 확인
- 타임라인 밀도: 기본 `큰 주제 위주`(같은 중심 토크의 세부 내용 병합), `기본`, `촘촘하게` 선택 가능
- Whisper 모델: 기본 `large-v3-turbo`(속도 우선), 선택 가능 `large-v3`(정확도 우선)
- 연산 장치: 기본 `자동`(CUDA 런타임이 준비되면 GPU, 아니면 CPU int8)

FW 자막 추출 작업은 모델 메모리와 오디오 디코더 중복 사용을 피하기 위해 한 번에 1개씩 실행하며, 추가 요청은 대기열에서 순서대로 처리합니다.

Whisper 모델은 첫 분석 때 한 번 내려받고 이후 로컬 캐시를 사용합니다. 기본 설치본에는 대용량 CUDA 파일을 넣지 않습니다. NVIDIA GPU PC에서는 Release의 `SOOPTimeline-GPU-Addon.exe`를 한 번 설치하면 CUDA 12 cuBLAS와 cuDNN 9를 사용할 수 있습니다. 런타임이 없으면 `자동` 설정에서 CPU `int8`로 대체하며, AI 설정에 GPU 애드온 다운로드 버튼을 표시합니다. `NVIDIA GPU`를 명시적으로 선택하면 필요한 런타임이 없을 때 CPU로 몰래 전환하지 않고 오류를 표시합니다.

휴대용 Release EXE에는 CUDA 런타임을 계속 포함합니다. 설치본은 GPU 애드온 설치 후 다음 명령으로 외부 GPU 경로까지 확인할 수 있습니다. 성공하면 `%LOCALAPPDATA%\SOOPTimeline\gpu-smoke-ok.txt`가 생성됩니다.

```powershell
.\dist\SOOPTimeline\SOOPTimeline.exe --gpu-smoke-test
```

현재 Windows GPU 런타임 버전은 다음 명령으로 설치할 수 있습니다.

```powershell
.\.venv\Scripts\python -m pip install -e ".[gpu-windows]"
```

## 저장 위치

- 데이터베이스: `%LOCALAPPDATA%\SOOPTimeline\timeline.db`
- 전사 캐시: `%LOCALAPPDATA%\SOOPTimeline\analysis\<VOD 번호>\transcript.json`
- 중간 전사 체크포인트: `%LOCALAPPDATA%\SOOPTimeline\analysis\<VOD 번호>\transcript.partial.json`
- Gemini 구간 체크포인트: `%LOCALAPPDATA%\SOOPTimeline\analysis\<VOD 번호>\timeline.partial.json`
- 진행 중인 라이브 증분 자막: `%LOCALAPPDATA%\SOOPTimeline\analysis\<라이브 세션 번호>\live-transcript.jsonl`
- 종료된 라이브 누적 자막: `%LOCALAPPDATA%\SOOPTimeline\analysis\<라이브 세션 번호>\live-transcript.json`
- 앱 종료 중 놓친 라이브 구간 기록: `%LOCALAPPDATA%\SOOPTimeline\analysis\<라이브 세션 번호>\live-reconnect.jsonl`
- 검수 플레이어 프로필: `%LOCALAPPDATA%\SOOPTimeline\webview2`
- 오류 로그: `%LOCALAPPDATA%\SOOPTimeline\logs\soop-timeline.log`

테스트에서는 `SOOP_TIMELINE_DATA_DIR` 환경 변수로 저장 위치를 바꿀 수 있습니다.

## 현재 경계와 주의사항

- 이 앱은 비공식 도구이며 SOOP의 승인·제휴를 의미하지 않습니다. 공개 배포 전에는 내부 조회 엔드포인트와 자동 분석 방식의 허용 범위를 SOOP에 별도로 확인하는 것이 안전합니다.
- 공개 VOD 재생 페이지가 사용하는 내부 조회 엔드포인트에서 오디오 전용 재생목록을 확인합니다. 이는 공식 개발자 API가 아니므로 SOOP의 변경으로 언제든 동작이 중단될 수 있고, 사용 전 별도 허용 여부를 확인하는 것이 안전합니다.
- 신규 영상 확인과 일반 FW/AI 분석은 항상 비로그인 요청이며 사용자의 일반 브라우저 쿠키를 읽지 않습니다. 공개 19세 VOD·라이브가 실제로 차단된 경우에만 격리된 임시 WebView2 프로필에서 사용자가 직접 로그인하고 성인 인증을 완료합니다. 가져온 SOOP 세션 쿠키는 해당 VOD 또는 라이브 작업에만 메모리에서 허용하고, 다른 콘텐츠에는 보내지 않으며 작업 종료 즉시 폐기합니다. 임시 로그인 창의 브라우저 쿠키와 프로필도 완료 후 삭제합니다. 비공개·유료·구매 제한 VOD와 숨김 파트는 로그인 여부와 관계없이 거부합니다.
- 다시보기 AI 분석에는 영상 파일·영상 스트림·시스템 출력음 캡처를 사용하지 않습니다. 라이브 자막 추출은 최저 화질 결합 스트림을 받아 오디오만 해독하므로 영상 데이터도 전송 구간에는 포함되지만 저장하거나 영상으로 처리하지 않습니다. 검수 재생은 공식 임베드 페이지의 일반 스트리밍이며 앱이 별도 영상 파일을 만들지는 않지만, WebView2가 통상적인 브라우저 캐시를 사용할 수 있습니다.
- 사용자가 선택한 VOD만 한 번씩 분석하며 대량 수집이나 무제한 병렬 요청을 하지 않습니다.
- 다시보기 AI 타임라인 생성이나 사용자가 요청한 재정리 작업에서는 Gemini에 자막 텍스트와 프롬프트가 전송되므로 Google의 데이터 처리 약관·보관 정책·요금 및 무료 사용량 한도를 확인해야 합니다. 라이브 자막 추출 중에는 Gemini로 전송하지 않습니다.
- 첫 실행 안내와 [PRIVACY.md](PRIVACY.md)에 로컬 저장 항목과 Gemini 전송 범위를 정리했습니다. 영상 제목·자막·단어 사전은 비신뢰 데이터 경계 안에 넣고 자막 속 명령문은 따르지 않도록 시스템 지시를 적용합니다.
- 공식 SOOP 댓글 API 권한은 아직 연결되어 있지 않습니다. 편집 탭의 `SOOP에 작성`은 대신 검수 플레이어와 같은 WebView2 프로필에서 사용자가 직접 로그인한 세션으로 댓글창을 조작해 첫 블록을 댓글, 나머지를 대댓글로 등록합니다. 비밀번호는 SOOP 로그인 페이지에서만 입력하고 앱은 저장하지 않으며, 세션 쿠키만 재사용합니다.
- 로그인 세션을 이용한 자동 등록은 공식 API가 아니므로 SOOP 이용약관에 저촉될 수 있고 SOOP의 페이지 변경으로 언제든 중단될 수 있습니다. 사용은 본인 책임이며, 등록 전 확인 창과 첫 사용 안내를 거칩니다. 자동 등록이 댓글창을 찾지 못하면 `댓글 영역 구조 저장(진단)`으로 구조를 남길 수 있습니다.
- 생성된 내용은 항상 사용자가 검수한 뒤 등록하며, 블록별 `이 블록 복사`로 직접 붙여넣는 방식도 그대로 사용할 수 있습니다.

## 테스트

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

주제 경계 프롬프트를 바꿀 때는 `evaluation/topic_boundary_cases.json`에 검수 사례를 추가하고, 모델 결과의 시작 초를 predictions JSON에 넣어 다음 회귀 평가를 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python tools\evaluate_topic_boundaries.py
```
