# Slack 견적서 자동화 봇

Slack에서 `/견적서` 명령어를 입력하면 PDF 견적서를 자동 생성하고 채널에 업로드합니다.

---

## Railway 배포 방법

1. [Railway](https://railway.app) 로그인 → **New Project** → **Deploy from GitHub repo**
2. 이 레포(`sin0724/quote-automation`) 선택
3. **Variables** 탭에서 아래 환경변수 추가
4. **Settings → Deploy** 탭에서 Start Command: `python app.py` 확인
5. Deploy 후 **Logs**에서 `Quote automation bot started` 확인

---

## 필요한 환경변수

| 변수명 | 필수 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | `xoxb-` 로 시작하는 봇 토큰 |
| `SLACK_APP_TOKEN` | ✅ | `xapp-` 로 시작하는 앱 레벨 토큰 (Socket Mode) |
| `COMPANY_NAME` | | 공급사명 |
| `COMPANY_REPRESENTATIVE` | | 대표자명 |
| `COMPANY_BUSINESS_NUMBER` | | 사업자등록번호 |
| `COMPANY_ADDRESS` | | 주소 |
| `COMPANY_CONTACT` | | 연락처 |
| `OUTPUT_DIR` | | PDF 임시 저장 경로 (기본값: `/tmp/output`) |

---

## Slack App 설정 방법

1. [api.slack.com/apps](https://api.slack.com/apps) → 앱 선택
2. **Socket Mode** 활성화 → App-Level Token 생성 (`connections:write` 권한)
3. **Slash Commands** → `/견적서` 커맨드 추가
4. **OAuth & Permissions** → Bot Token Scopes에 아래 추가:
   - `commands`
   - `chat:write`
   - `files:write`
5. 워크스페이스에 앱 설치

---

## 사용 예시

```
/견적서 거래처명: ABC병원
견적명: 대만 KOC 마케팅
담당자: 김민수
부가세: 포함
품목:
- 대만 KOC 체험단 10명 / 1 / 1500000
- Threads 바이럴 20건 / 1 / 800000
- 번역 및 운영 관리 / 1 / 300000
비고: 유효기간 7일
```

- `/견적서 테스트` — 테스트용 견적서 생성
- `/견적서 도움말` — 사용법 안내

---

## 문제 해결 체크리스트

- [ ] Railway Logs에 `Quote automation bot started` 가 보이는가?
- [ ] `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` 환경변수가 정확히 입력되었는가?
- [ ] Slack App의 Socket Mode가 **On** 상태인가?
- [ ] `/견적서` 커맨드가 Slack App에 등록되어 있는가?
- [ ] Bot Token Scopes에 `chat:write`, `files:write` 가 포함되어 있는가?
- [ ] 앱이 해당 Slack 채널에 추가되어 있는가?
