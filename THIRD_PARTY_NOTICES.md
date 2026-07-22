# 제3자 소프트웨어 고지

SOOP AI 타임라인은 아래 오픈소스 및 재배포 가능 구성요소를 사용합니다. 각 구성요소의 저작권은 해당 권리자에게 있으며, 이 앱의 배포가 해당 권리자의 보증·승인·제휴를 의미하지 않습니다.

빌드 과정은 런타임 Python 의존성을 재귀적으로 확인하여 배포 환경에 설치된 `LICENSE`, `COPYING`, `NOTICE`, `AUTHORS` 파일을 EXE 내부의 `third_party_licenses` 경로에 함께 수집합니다. 이 문서는 주요 구성요소를 안내하는 색인이며, 실제 적용 조건은 함께 포함된 각 라이선스 원문이 우선합니다.

## 주요 구성요소

| 구성요소 | 확인 버전 | 라이선스 | 프로젝트·라이선스 |
| --- | ---: | --- | --- |
| Python | 3.10+ | PSF License | <https://docs.python.org/3/license.html> |
| PySide6, Qt, Shiboken6 | 6.11.1 | LGPL-3.0-only 또는 GPL/상용 라이선스 | <https://doc.qt.io/qt-6/licensing.html> |
| PyAV | 17.1.0 | BSD-3-Clause | <https://pyav.org/docs/stable/development/license.html> |
| FFmpeg 라이브러리 | PyAV 휠 포함 버전 | LGPL/GPL 및 구성요소별 라이선스 | <https://ffmpeg.org/legal.html> |
| faster-whisper | 1.2.1 | MIT | <https://github.com/SYSTRAN/faster-whisper> |
| CTranslate2 | 4.8.1 | MIT | <https://github.com/OpenNMT/CTranslate2> |
| ONNX Runtime | 1.23.2 | MIT 및 별도 제3자 고지 | <https://github.com/microsoft/onnxruntime> |
| Hugging Face tokenizers | 0.23.1 | Apache-2.0 | <https://github.com/huggingface/tokenizers> |
| NumPy | 2.2.6 | BSD-3-Clause 및 번들 구성요소별 라이선스 | <https://numpy.org/doc/stable/license.html> |
| Google Gen AI SDK | 1.75.0 | Apache-2.0 | <https://github.com/googleapis/python-genai> |
| keyring | 25.7.0 | MIT | <https://github.com/jaraco/keyring> |
| qtwebview2 | 0.4.1 | MPL-2.0 | <https://pypi.org/project/qtwebview2/> |
| pythonnet | 3.1.0 | MIT | <https://github.com/pythonnet/pythonnet> |
| Microsoft WebView2 Loader | qtwebview2 포함 버전 | Microsoft 라이선스 | <https://www.nuget.org/packages/Microsoft.Web.WebView2> |
| PyInstaller 부트로더 | 6.21.0 | GPL-2.0-or-later와 부트로더 예외 | <https://pyinstaller.org/en/stable/license.html> |
| NVIDIA cuBLAS | 12.6.4.1 | NVIDIA Proprietary Software | <https://docs.nvidia.com/cuda/eula/index.html> |
| NVIDIA cuDNN | 9.6.0.74 | NVIDIA Proprietary Software | <https://docs.nvidia.com/deeplearning/cudnn/latest/reference/eula.html> |

## 기타 런타임 의존성

애플리케이션은 위 구성요소의 전이 의존성으로 Apache-2.0, BSD, MIT, MPL-2.0, PSF 등의 라이선스를 사용하는 패키지를 포함할 수 있습니다. 정확한 패키지명·버전과 원문은 각 빌드에 자동 포함되는 `third_party_components.txt` 및 `third_party_licenses` 파일을 확인하세요.

Qt/PySide6와 FFmpeg/PyAV, NVIDIA 런타임을 포함한 재배포 조건은 배포 형태와 사용 방식에 따라 추가 의무가 생길 수 있습니다. 상업 배포 또는 라이선스 해석이 필요한 경우 각 권리자의 최신 조건을 확인하고 전문가의 검토를 받으세요.
