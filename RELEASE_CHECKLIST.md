# 배포 체크리스트

## 자동 처리되는 항목

- `pyproject.toml`과 앱 버전 일치 테스트
- 전체 단위 테스트
- Windows EXE 빌드 및 패키지 스모크 테스트
- EXE 안의 CUDA 12 cuBLAS·cuDNN 9 패키지 포함 여부 검사
- EXE SHA-256이 포함된 `update.json` 생성
- `PRIVACY.md`, `THIRD_PARTY_NOTICES.md`와 빌드 환경의 제3자 라이선스 파일을 EXE에 포함
- `vX.Y.Z` 버전 태그 푸시 시 GitHub Release와 파일 업로드
- 실행 중인 앱에서 GitHub 최신 Release 감지

## 배포자가 결정하거나 준비해야 하는 항목

1. 공개 저장소의 소스 사용 허가 범위(예: MIT, GPL, 비공개 저작권 유지)를 결정하고 `LICENSE`를 추가한다. 현재는 별도 라이선스가 없으므로 재사용 허가를 부여하지 않은 상태다.
2. Windows SmartScreen 경고를 줄이려면 신뢰할 수 있는 코드 서명 인증서를 준비한다. 인증서가 Windows 인증서 저장소에 설치돼 있으면 `SOOP_TIMELINE_SIGN_CERT_THUMBPRINT` 환경 변수로 지문을 전달해 `build_exe.ps1`에서 자동 서명·검증할 수 있다.
3. API 키, 인증서 개인키, SOOP 로그인 정보는 저장소나 Release 파일에 포함하지 않는다.
4. 최초 Release 전 실제 공개 VOD 1개와 짧은 라이브에서 연결·재연결·검수 플레이어를 수동 확인한다.
5. NVIDIA GPU가 있는 Windows PC에서 `SOOPTimeline.exe --gpu-smoke-test`를 실행하고 `%LOCALAPPDATA%\SOOPTimeline\gpu-smoke-ok.txt` 생성을 확인한다. GitHub 호스팅 Windows 러너에는 GPU가 없으므로 이 항목은 자동화할 수 없다.
6. 첫 실행 데이터 처리 안내, `PRIVACY.md`, Gemini 무료/유료 사용량 안내가 배포 설명과 일치하는지 확인한다.
7. 이 앱은 SOOP 비공식 도구이며 내부 조회 엔드포인트를 사용한다. 불특정 다수에게 공개하기 전 SOOP에 현재 수집·분석 방식을 설명하고 허용 범위를 확인한다.
8. 최종 커밋에서 버전을 올린 뒤 `vX.Y.Z` 태그를 만들고, 태그 푸시 전에 로컬 EXE의 기본·GPU 스모크 테스트와 실제 VOD 이동을 확인한다.

GitHub Actions 워크플로 파일을 처음 푸시할 때 사용 중인 GitHub 인증 토큰에 `workflow` 권한이 없으면 푸시가 거절될 수 있다. 이 경우 GitHub CLI 인증 범위만 갱신한 뒤 다시 푸시한다.
