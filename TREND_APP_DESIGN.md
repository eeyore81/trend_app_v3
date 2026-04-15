# Trend App Design Document

## 1. 개요

`trend_app_v3.py`는 Google Trends 데이터를 주기적으로 조회하고, Telegram을 통해 키워드 모니터링 결과를 알림하는 Python 기반 트렌드 모니터링 애플리케이션입니다.

이 프로그램은 다음 기능을 제공합니다:
- `keywords.json`에 저장된 키워드 목록 감시
- Google Trends API(`pytrends`)를 이용한 트렌드 데이터 수집
- 텔레그램 메시지/이미지 발송
- 자동 요약 리포트 생성
- 명령어 기반 키워드 추가/삭제/상태 조회
- `+`로 연결된 두 단어 키워드 전용 정규화 및 앞단어 기준 점수 계산
- 로컬 캐시(`trend_cache.json`) 관리 및 키워드 삭제 시 캐시 정리

---

## 2. 주요 구성

### 2.1 설정 및 상수

`trend_app_v3.py` 상단에서 애플리케이션 설정을 정의합니다.

- `TELEGRAM_TOKEN`: Telegram 봇 토큰
- `CHAT_ID`: 기본 Telegram chat ID
- `DB_FILE`: 감시 키워드 저장 파일 (`keywords.json`)
- `TREND_TIMEFRAME`: Google Trends 데이터 조회 기간 (`today 3-m`)
- `GEO`: 국가 코드 (빈 문자열은 전세계)
- `RECENT_WINDOW_DAYS`: 최근 평균 계산에 사용할 일 수
- `THRESHOLD_ACCEL`: 급등/급락 감지 임계값
- `THRESHOLD_GAP`: 두 용어 간 간격 변화 감지 임계값
- `THRESHOLD_HIGH`: 고점 알람 임계값
- `CACHE_TTL_SECONDS`: 캐시 유효 기간
- `CACHE_FILE`: 캐시 파일 (`trend_cache.json`)
- `STARTED_CHATS_FILE`: 텔레그램 알림 대상 채팅 저장

### 2.2 라이브러리 및 환경

- `pytrends`를 통해 Google Trends API를 호출
- `requests`로 Telegram API 요청
- `matplotlib`로 트렌드 그래프 생성
- `pandas`로 시계열 데이터 처리
- `urllib3` 호환성 패치로 pytrends의 `method_whitelist` 지원

---

## 3. 데이터 관리

### 3.1 키워드 저장

- `load_keywords()`: `keywords.json`에서 키워드 집합을 로드
- `save_keywords(keywords)`: 키워드를 저장하고 캐시 정리를 호출
- `normalize_keyword(keyword)`: 특정 키워드 정규화 처리
  - 예: `medicube+cosrx` → `cosrx+medicube`

### 3.2 캐시 저장

- `load_cache()`: `trend_cache.json`에서 `last_scores`, `last_fetch_time`, `last_trend_data` 로드
- `save_cache()`: 현재 캐시 상태를 `trend_cache.json`에 저장
- `cleanup_cache_data()`: `keywords.json`에 없는 키워드에 대한 캐시 항목 제거

### 3.3 시작 채팅 저장

- `load_started_chats()`: `started_chats.json` 로드
- `save_started_chats()`: 저장된 채팅 ID 목록 기록

---

## 4. Trend 데이터 수집 및 평가

### 4.1 pytrends 요청

- `create_pytrends()`: 사용자 에이전트, 프록시, 인증 설정을 포함한 `TrendReq` 객체 생성
- `fetch_keyword_score(keyword)`: 캐시 유효 시 저장된 점수 재사용, 아니면 pytrends로 최신 데이터 조회

### 4.2 추세 계산

- `parse_keyword_terms(keyword)`: 키워드 구문 분석
  - `and`, `*`, `,`, `+`, `|`를 구분자로 처리
- `get_primary_scoring_terms(keyword, terms)`: `+`로 연결된 두 단어 키워드의 경우 앞단어 기준 점수를 계산하기 위해 첫 번째 용어 선택
- `get_recent_average(data, terms, scoring_terms=None)`: 마지막 하루를 제외하고 최근 윈도우 평균 계산
- `compute_pairwise_gap(data, terms, window)`: 두 용어 간 최근 간격 및 이전 간격 비교
- `get_trend_metrics(data, terms)`: 고점, 스파이크, 증감, 기울기 등 종합 지표 계산
- `format_trend_reason(...)`: 트렌드 이유 메시지 문자열 생성

### 4.3 알림 조건

- `analyze_trend(keyword, is_first=False, chat_id=None)`: 다음 기준일 때 텔레그램 전송
  - 최초 감시 시작 시
  - 평균 변화가 `THRESHOLD_ACCEL` 이상일 때
  - 현재 점수가 `THRESHOLD_HIGH` 이상일 때
  - 두 용어 간 간격 변화가 `THRESHOLD_GAP` 이상일 때

---

## 5. Telegram 통신

### 5.1 메시지 전송

- `send_text_message(text, chat_id=None)`: 텔레그램 `sendMessage`
- `send_telegram_photo(buf, caption, chat_id=None, filename='graph.png')`: 텔레그램 `sendPhoto`
- `send_status_trend_graph()`, `send_status_trend_graphs()`: 개별/전체 키워드 그래프 전송
- `send_summary_trend_graphs()`: 요약 리포트 상위 키워드 그래프 전송

### 5.2 메시지/명령어 처리

- `handle_command(cmd, arg, chat_id)`: Telegram 명령어 처리
  - `start`/`시작`: 알림 수신 채팅 등록
  - `add`/`추가`: 키워드 추가
  - `del`/`삭제`: 키워드 제거
  - `list`/`목록`: 현재 감시 키워드 출력
  - `status`/`상태`: 상태 보고서 전송
  - `summary`/`요약`: 요약 보고서 전송
  - `test`/`테스트`: 테스트 데이터 전송
  - `help`/`도움`/`도움말`: 도움말 출력
- `process_telegram_update(update)`: Telegram `getUpdates` 메시지 파싱

---

## 6. 리포트 및 요약

### 6.1 요약 리포트

- `get_keyword_summary_metrics()`: 키워드별 점수, 변동, 이유 수집
- `build_summary_text()`: 상승/하락 TOP 3 및 경고 키워드 목록 문자열 생성
- `send_summary_report(chat_id=None)`: 요약 텍스트 + 요약 키워드 그래프 전송

### 6.2 상태 리포트

- `build_status_text()`: 현재 저장된 키워드 상태 및 캐시 여부 요약
- `send_status_report(chat_id=None)`: 상태 리포트와 모든 그래프 전송

---

## 7. 실행 흐름

### 7.1 CLI/워크플로우 진입점

- `main()`: CLI 인자 파싱
  - `--message`: Telegram 또는 GAS에서 전달된 명령어 텍스트
  - `--chat_id`: Telegram chat ID
- `main()`에서 `message`가 없으면 스케줄 호출로 판단하고 `send_scheduled_summary()` 실행
- 메시지가 있으면 명령어 파싱 후 `handle_command()` 실행

### 7.2 스케줄 리포트

- `send_scheduled_summary(chat_id=None)`: 모든 키워드에 대해 `fetch_keyword_score()` 실행 후 요약 리포트 전송

---

## 8. 파일 및 데이터 구조

- `trend_app_v3.py`: 애플리케이션 핵심 코드
- `keywords.json`: 감시 대상 키워드 목록
- `trend_cache.json`: `last_scores`, `last_fetch_time`, `last_trend_data` 캐시 저장
- `started_chats.json`: 알림 대상 Telegram 채팅 ID 저장
- `TREND_APP_DESIGN.md`: 본 설계 문서

---

## 9. 확장 및 개선 포인트

### 9.1 외부 저장소 활용

- `trend_cache.json` 대신 S3/GCS/BLOB 저장소를 사용하면 Git 커밋 없이 상태 유지 가능

### 9.2 명령어 확장

- `interval`, `threshold`, `geo` 등의 실시간 설정 변경 명령어 추가

### 9.3 트렌드 분석 고도화

- SMA/EMA, 시즌성 지표, 자연어 분석 기반 키워드 그룹화

### 9.4 Telegram 명령어 라우팅

- Telegram webhook 지원으로 폴링 대신 이벤트 방식 처리

---

## 10. 주석 및 설계 요약

이 프로그램은 Google Trends와 Telegram을 결합한 트렌드 모니터링 시스템으로, 현상적 경고와 요약 보고서를 자동화하는 데 중점을 둡니다. 로컬 파일 기반 데이터 저장과 캐시 정리를 통해 간단한 운영 환경에서 안정적으로 동작하도록 설계되었습니다.
