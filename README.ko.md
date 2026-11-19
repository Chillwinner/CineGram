<div align="center">

# CineGram

### [CineVec](https://github.com/Chillwinner/Cinevec) 공식 독립형 FastAPI Telegram 플러그인

**Telegram은 무한 클라우드 드라이브지만, 6개월 전 그룹에 올라온 10초 영상을 다시 찾기는 악몽입니다.**
CineGram은 Telegram 채널·그룹의 사진과 최대 4GB 영상을 **Zero-Retention** 방식으로 **CineVec**에 연동해 3D 상영관에서 바로 찾고 원본 `t.me` 메시지로 이동하게 해줍니다.

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · **한국어** · [Español](README.es.md)

[![CineVec Core](https://img.shields.io/badge/Core-CineVec%20%2Fapi%2Fv1-E06A3E?style=for-the-badge)](https://github.com/Chillwinner/Cinevec)
[![FastAPI](https://img.shields.io/badge/FastAPI-Lightweight%20Web%20UI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telethon](https://img.shields.io/badge/MTProto-Telethon%20(Up%20to%204GB)-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.telethon.dev/)

[주요 기능](#주요-기능) • [장점](#일반-수집기와의-차이점) • [본체 CineVec](#연동-본체-cinevec) • [확장 및 교체](#확장-및-컴포넌트-교체) • [빠른 배포](#빠른-배포)

</div>

---

## 주요 기능

**CineGram**은 오직 `/api/v1/*`와 Bearer API Token만으로 **[CineVec](https://github.com/Chillwinner/Cinevec)** 본체와 통신하는 독립 FastAPI 마이크로서비스입니다.

- **4가지 독립 모드(필수 강제 없음)**: Bot과 MTProto 사용자 계정 모두 선택 사항이며, 내장 웹 콘솔(`http://127.0.0.1:8018/`)에서 Web 전용 / Bot 전용 / MTProto 전용 / 듀얼 모드를 실시간 전환할 수 있습니다.
- **필터 규칙 + 웹 썸네일 선택 가져오기**: 그룹별 필터(유형·크기·길이·키워드·기간)와 웹 썸네일 브라우저로 원하는 메시지만 골라 가져올 수 있습니다.
- **시맨틱 & 태그/설명 검색**: 웹 콘솔 및 선택형 Bot(`/s`, `/m`, 이미지 검색)에서 바로 검색합니다.

---

## 일반 수집기와의 차이점

| 항목 | 일반 Telegram 백업 스크립트 | CineGram |
| :--- | :--- | :--- |
| **디스크 공간** | 대용량 MP4를 전부 로컬에 복사해 저장 공간 고갈. | **Zero-Retention + 이중 백프레셔**: 업로드 즉시 임시 파일 삭제, 남은 디스크 및 CineVec 전처리 대기열에 따라 자동 일시정지. |
| **다중 파일 앨범** | 첫 번째 사진에만 설명이 붙고 나머지는 `#태그` 유실. | **앨범 캡션 & `#태그` 자동 상속**: 같은 미디어 그룹의 모든 파일에 설명과 태그를 자동 전파. |
| **링크 만료 대응** | 채널 이름 변경·비공개 전환 시 깨진 링크 방치. | **정기 생존 점검**: 이름 변경·비공개 전환(`t.me/c/...` 자동 갱신)·삭제 메시지를 감지해 `source_invalid` 반영. |
| **중복 방지** | 백필을 여러 번 누르면 중복 등록. | **4단계 중복 제거**: 동시 실행 락 + 커서 이어받기 + 로컬 DB + CineVec `ref_id` 조회. |

---

## 연동 본체: CineVec

- **본체 저장소**: [https://github.com/Chillwinner/Cinevec](https://github.com/Chillwinner/Cinevec)
- CineVec **Settings → Plugin API Tokens**에서 발급한 토큰만 입력하면 즉시 연결됩니다.

---

## 확장 및 컴포넌트 교체

- **코드 수정 없이 변경**: Bot/MTProto 전환, Zero-Retention/S3 보관 여부, 필터 규칙, 점검 주기는 모두 웹 콘솔 또는 `.env`에서 즉시 변경됩니다.
- **Telegram 라이브러리나 백엔드 교체**: [`src/tg_plugin/models.py`](src/tg_plugin/models.py)의 `TelegramInspector` 프로토콜이나 [`src/tg_plugin/cinevec_client.py`](src/tg_plugin/cinevec_client.py)의 `CineVecClient`를 구현해 `create_plugin_app(..., inspector=..., cinevec=...)`에 주입하면 됩니다.

---

## 빠른 배포

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m tg_plugin serve
```

**http://127.0.0.1:8018/** 에서 CineVec API Token 및 선택형 Bot Token / MTProto(`python -m tg_plugin login`)를 설정하세요. 테스트: `pytest -q`.
