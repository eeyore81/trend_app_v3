import threading
import json
import logging
import os
import time
import random
import re
import requests
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import rc

# urllib3 v2 compatibility: pytrends may pass deprecated method_whitelist to Retry
try:
    from urllib3.util import Retry as Urllib3Retry
    _orig_retry_init = Urllib3Retry.__init__
    def _compat_retry_init(self, *args, **kwargs):
        if 'method_whitelist' in kwargs:
            kwargs['allowed_methods'] = kwargs.pop('method_whitelist')
        return _orig_retry_init(self, *args, **kwargs)
    Urllib3Retry.__init__ = _compat_retry_init
except Exception:
    pass

from pytrends.request import TrendReq

# --- 1. 환경 설정 (반드시 수정하세요) ---
TELEGRAM_TOKEN = "8601859792:AAGJxMassWN9inm_xBPfLHUTeMalpWgMJ-Q"
CHAT_ID = "1677003257"
DB_FILE = "keywords.json"
TREND_TIMEFRAME = 'today 3-m'  # 90일(약 3개월) 데이터
GEO = ''  # 전세계
RECENT_WINDOW_DAYS = 3  # 최근 3일 평균으로 급등 판단
THRESHOLD_ACCEL = 30  # 3일 평균 변화 기준
THRESHOLD_HIGH = 90   # 고점 알람 기준
CACHE_TTL_SECONDS = 900  # 상태 표시용 임시 캐시 유효 시간
DAILY_FETCH_INTERVAL = 86400  # 하루에 한 번만 새로 가져오기
TELEGRAM_POLL_INTERVAL = 5  # Telegram getUpdates 폴링 주기
CACHE_FILE = 'trend_cache.json'

USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
]

def get_request_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Referer': 'https://trends.google.com/',
    }

# 필요 시 프록시를 추가하세요. 기본은 빈 딕셔너리입니다.
PROXIES = {}

# 로그 파일 설정
logging.basicConfig(
    filename='trend_app.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# 맥 전용 한글 폰트 설정 (인텔/실리콘 공통)
rc('font', family='AppleGothic')
plt.rcParams['axes.unicode_minus'] = False 

# --- 2. 데이터 관리 함수 ---
def load_keywords():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_keywords(keywords):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(keywords), f, ensure_ascii=False)


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        for kw, score in cache.get('last_scores', {}).items():
            last_scores[kw] = int(score)
        for kw, ts in cache.get('last_fetch_time', {}).items():
            last_fetch_time[kw] = float(ts)
        for kw, payload in cache.get('last_trend_data', {}).items():
            dates = [pd.to_datetime(d) for d in payload.get('dates', [])]
            columns = payload.get('columns', [kw])
            values = payload.get('values', {})
            if dates and columns and values:
                if isinstance(values, dict):
                    data = {col: values.get(col, []) for col in columns}
                else:
                    data = {kw: values}
                df = pd.DataFrame(data, index=pd.DatetimeIndex(dates))
                last_trend_data[kw] = df
    except Exception as e:
        logger.warning(f"Failed to load cache: {e}")


def save_cache():
    try:
        serialized = {
            'last_scores': {kw: score for kw, score in last_scores.items()},
            'last_fetch_time': {kw: ts for kw, ts in last_fetch_time.items()},
            'last_trend_data': {}
        }
        for kw, df in last_trend_data.items():
            serialized['last_trend_data'][kw] = {
                'dates': [idx.isoformat() for idx in df.index],
                'columns': list(df.columns),
                'values': {col: [int(v) for v in df[col].tolist()] for col in df.columns}
            }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(serialized, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")

# --- 3. 그래프 생성 및 텔레그램 전송 ---
def send_trend_report(keyword, data, score, diff=None, is_first=False, chat_id=None):
    """그래프를 그려서 텔레그램으로 전송"""
    plt.figure(figsize=(10, 5))
    terms = list(data.columns)
    for term in terms:
        plt.plot(data.index, data[term], marker='o', linewidth=2, label=term)
    plt.fill_between(data.index, data[terms[-1]], color='#ff4500', alpha=0.1)
    
    plt.title(f"📈 키워드 모니터링: {keyword} (최근 90일)", fontsize=15, pad=20)
    plt.grid(True, axis='y', linestyle='--', alpha=0.7)
    plt.xlabel("기간 (최근 90일)")
    plt.ylabel("관심도 점수 (0-100)")
    plt.ylim(0, 100)
    plt.axhline(THRESHOLD_HIGH, color='red', linestyle='--', linewidth=1, alpha=0.7, label=f'고점 기준({THRESHOLD_HIGH})')
    plt.legend(loc='upper left')
    
    # 이미지 메모리 저장
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()

    # 메시지 구성
    if is_first:
        caption = f"✅ [감시 시작] '{keyword}'\n현재 트렌드 지수: {score}점"
    elif diff is not None:
        caption = f"🚀 [가속도 발생] '{keyword}' 급상승!\n현재: {score} (▲{diff}점 상승)"
    else:
        caption = f"🔥 [고점 유지] '{keyword}' 현재 지수: {score}"

    # 텔레그램 전송
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': ('graph.png', buf, 'image/png')}
    try:
        resp = requests.post(url, data={'chat_id': chat_id or CHAT_ID, 'caption': caption}, files=files, timeout=10)
        if resp.ok:
            logger.info(f"Telegram photo sent: keyword={keyword}, chat_id={chat_id or CHAT_ID}")
            print(f"✅ 텔레그램 전송 성공: keyword={keyword}, chat_id={chat_id or CHAT_ID}")
        else:
            logger.error(f"Telegram photo failed: status={resp.status_code}, response={resp.text}")
            print(f"❌ 텔레그램 전송 실패: status={resp.status_code}, response={resp.text}")
    except Exception as e:
        logger.exception(f"Telegram photo exception for {keyword}")
        print(f"❌ 전송 실패: {e}")


def build_status_text():
    keywords = load_keywords()
    if not keywords:
        return "📌 현재 감시 중인 키워드가 없습니다."

    parts = ["📌 현재 감시 상태"]
    for kw in sorted(keywords):
        score = last_scores.get(kw)
        if score is None:
            parts.append(f"- {kw}: 조회 안됨")
        else:
            note = ""
            if should_fetch_daily(kw):
                note = " (24시간 경과, 캐시)"
            elif not is_cache_fresh(kw):
                note = " (캐시된 데이터)"
            parts.append(f"- {kw}: {score}점{note}")
    parts.append(f"\n🔔 고점 기준: {THRESHOLD_HIGH}점")
    return "\n".join(parts)


def is_cache_fresh(keyword):
    if keyword not in last_fetch_time:
        return False
    return (time.time() - last_fetch_time[keyword]) < CACHE_TTL_SECONDS


def parse_keyword_terms(keyword):
    keyword = keyword.strip()
    if re.search(r'\b(and)\b', keyword, flags=re.IGNORECASE) or '*' in keyword:
        parts = re.split(r'\s*(?:\*|\band\b)\s*', keyword, flags=re.IGNORECASE)
        combined = ' '.join(part.strip() for part in parts if part.strip())
        return [combined] if combined else []

    parts = re.split(r'\s*(?:,|\+|\|)\s*', keyword)
    return [part.strip() for part in parts if part.strip()]


def get_recent_average(data, terms, window=RECENT_WINDOW_DAYS):
    valid_terms = [term for term in terms if term in data]
    if not valid_terms:
        return None, None

    current_avgs = []
    prev_avgs = []
    for term in valid_terms:
        values = data[term].dropna()
        if values.empty:
            continue

        if len(values) >= window:
            current_avgs.append(float(values.iloc[-window:].mean()))
        else:
            current_avgs.append(float(values.iloc[-1]))

        if len(values) >= 2 * window:
            prev_avgs.append(float(values.iloc[-2*window:-window].mean()))
        elif len(values) >= window + 1:
            prev_avgs.append(float(values.iloc[-(window + 1):-window].mean()))
        elif len(values) >= 2:
            prev_avgs.append(float(values.iloc[-2]))

    if not current_avgs:
        return None, None

    current_avg = max(current_avgs)
    prev_avg = max(prev_avgs) if prev_avgs else None
    return current_avg, prev_avg


def fetch_keyword_score(keyword):
    terms = parse_keyword_terms(keyword)
    try:
        if keyword in last_scores and keyword in last_trend_data and is_cache_fresh(keyword):
            logger.info(f"Using cached score for {keyword}")
            return last_scores[keyword]

        pytrends = create_pytrends()
        time.sleep(random.uniform(2.5, 5.0))

        retry_attempts = 3
        wait_seconds = 1.0
        data = None

        for attempt in range(1, retry_attempts + 1):
            try:
                pytrends.build_payload(terms, timeframe=TREND_TIMEFRAME, geo=GEO)
                data = pytrends.interest_over_time()
                break
            except Exception as err:
                err_text = str(err)
                if '429' in err_text or 'Too Many Requests' in err_text:
                    logger.warning(f"429 detected during status fetch: {keyword}, attempt {attempt}/{retry_attempts}, wait {wait_seconds:.1f}s")
                    print(f"⚠️ 429 감지(조회 시도): {keyword}, 시도 {attempt}/{retry_attempts}, 대기 {wait_seconds:.1f}s")
                    if attempt == retry_attempts:
                        send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {err_text}")
                        logger.error(f"Google Trend 429 after retries: {keyword}, error={err_text}")
                        return None
                    time.sleep(wait_seconds)
                    wait_seconds *= 2
                    continue
                logger.error(f"{keyword} status fetch error: {err_text}")
                print(f"⚠️ {keyword} 상태 조회 오류: {err_text}")
                return None

        if data is not None and not data.empty and all(term in data for term in terms):
            latest_values = [int(data[term].iloc[-1]) for term in terms]
            score = int(max(latest_values))
            last_scores[keyword] = score
            last_trend_data[keyword] = data
            last_fetch_time[keyword] = time.time()
            save_cache()
            logger.info(f"Fetched score for {keyword}: {score}")
            return score
        return None
    except Exception as e:
        error_text = str(e)
        logger.exception(f"{keyword} status fetch exception: {error_text}")
        print(f"⚠️ {keyword} 상태 조회 예외: {error_text}")
        if '429' in error_text or 'Too Many Requests' in error_text:
            send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {error_text}")
        return None


def should_fetch_daily(keyword):
    if keyword not in last_fetch_time:
        return True
    return (time.time() - last_fetch_time[keyword]) >= DAILY_FETCH_INTERVAL


def refresh_status_keywords():
    keywords = load_keywords()
    for kw in sorted(keywords):
        if should_fetch_daily(kw):
            analyze_trend(kw)


def send_status_trend_graph(keyword, data, score, chat_id=None):
    plt.figure(figsize=(10, 5))
    terms = list(data.columns)
    for term in terms:
        plt.plot(data.index, data[term], marker='o', linewidth=2, label=term)
    if len(terms) == 1:
        plt.fill_between(data.index, data[terms[0]], color='#ff4500', alpha=0.1)

    plt.title(f"📈 {keyword} 최근 90일 변화")
    plt.grid(True, axis='y', linestyle='--', alpha=0.7)
    plt.xlabel("기간 (최근 90일)")
    plt.ylabel("관심도 점수 (0-100)")
    plt.ylim(0, 100)
    plt.axhline(THRESHOLD_HIGH, color='red', linestyle='--', linewidth=1, alpha=0.7, label=f'고점 기준({THRESHOLD_HIGH})')
    plt.legend(loc='upper left')

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()

    caption = f"📈 '{keyword}' 최근 90일 변화 그래프\n현재 점수: {score}점\n쿼리: {', '.join(terms)}\n지리: {'한국' if GEO == 'KR' else '전세계'}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': ('status_trend.png', buf, 'image/png')}
    try:
        resp = requests.post(url, data={'chat_id': chat_id or CHAT_ID, 'caption': caption}, files=files, timeout=10)
        if resp.ok:
            logger.info(f"Status trend graph sent: {keyword}")
            print(f"✅ '{keyword}' 90일 그래프 전송 성공")
        else:
            logger.error(f"Status trend graph failed: keyword={keyword}, status={resp.status_code}, response={resp.text}")
            print(f"❌ '{keyword}' 그래프 전송 실패: status={resp.status_code}, response={resp.text}")
    except Exception as e:
        logger.exception(f"Status trend graph send exception: {keyword}")
        print(f"❌ '{keyword}' 그래프 전송 에러: {e}")


def send_status_trend_graphs(chat_id=None):
    keywords = sorted(load_keywords())
    for kw in keywords:
        if kw in last_trend_data and kw in last_scores:
            send_status_trend_graph(kw, last_trend_data[kw], last_scores[kw], chat_id=chat_id)


def create_dummy_trend_dataframe(keyword, num_days=90):
    end_date = pd.Timestamp.today().normalize()
    dates = pd.date_range(end=end_date, periods=num_days)
    values = [max(5, min(70, int(round(50 + random.gauss(0, 12))))) for _ in range(num_days)]
    return pd.DataFrame({keyword: values}, index=dates)


def build_hot_test_data(keyword, original_data=None):
    if original_data is None or original_data.empty:
        data = create_dummy_trend_dataframe(keyword)
    else:
        data = original_data.copy()
        data = data.astype(float)
        if data.isnull().values.any():
            data = data.fillna(method='ffill').fillna(0)

    for col in data.columns:
        values = [float(v) for v in data[col].tolist()]
        if len(values) < RECENT_WINDOW_DAYS + 3:
            while len(values) < RECENT_WINDOW_DAYS + 3:
                values.insert(0, 20.0)

        for i in range(len(values) - RECENT_WINDOW_DAYS):
            values[i] = min(70.0, max(0.0, values[i]))

        hot_start = len(values) - RECENT_WINDOW_DAYS
        for idx in range(hot_start, len(values)):
            values[idx] = 95.0 + min(5.0, idx - hot_start)

        data[col] = [min(100, max(0, int(round(v)))) for v in values]

    return data


def send_test_trend(keyword, chat_id=None):
    keywords = load_keywords()
    if keyword not in keywords:
        send_text_message(f"🧪 테스트 실패: '{keyword}'이(가) 감시 목록에 없습니다. 먼저 키워드를 추가하세요.", chat_id=chat_id)
        return

    original_data = last_trend_data.get(keyword)
    synthetic_data = build_hot_test_data(keyword, original_data)
    terms = list(synthetic_data.columns)
    current_avg, prev_avg = get_recent_average(synthetic_data, terms)
    score = int(round(current_avg)) if current_avg is not None else 100
    diff = int(round(current_avg - prev_avg)) if prev_avg is not None else None

    send_text_message(f"🧪 테스트용 핫 트렌드 전송: '{keyword}'", chat_id=chat_id)
    send_trend_report(keyword, synthetic_data, score, diff=diff, chat_id=chat_id)


def send_status_report(chat_id=None):
    text = build_status_text()
    send_text_message(text, chat_id=chat_id)
    send_status_trend_graphs(chat_id=chat_id)


def send_text_message(text, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={'chat_id': chat_id or CHAT_ID, 'text': text}, timeout=10)
        if resp.ok:
            logger.info("Telegram text sent successfully")
            print(f"✅ 텔레그램 텍스트 전송 성공")
        else:
            logger.error(f"Telegram text failed: status={resp.status_code}, response={resp.text}")
            print(f"❌ 텔레그램 텍스트 전송 실패: status={resp.status_code}, response={resp.text}")
    except Exception as e:
        logger.exception("Telegram text send exception")
        print(f"❌ 텔레그램 텍스트 전송 에러: {e}")


def get_telegram_updates(offset=None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {'timeout': timeout}
    if offset is not None:
        params['offset'] = offset
    try:
        resp = requests.get(url, params=params, timeout=timeout + 5)
        if resp.ok:
            return resp.json().get('result', [])
    except Exception as e:
        logger.exception(f"Telegram getUpdates error: {e}")
    return []


def handle_command(cmd, arg, chat_id):
    if cmd in ('add', '추가') and arg:
        keywords = load_keywords()
        keywords.add(arg)
        save_keywords(keywords)
        send_text_message(f"➕ '{arg}' 추가됨. 그래프를 전송합니다.", chat_id=chat_id)
        analyze_trend(arg, True, chat_id=chat_id)
    elif cmd in ('del', '삭제') and arg:
        keywords = load_keywords()
        if arg in keywords:
            keywords.remove(arg)
            save_keywords(keywords)
            last_scores.pop(arg, None)
            send_text_message(f"🗑️ '{arg}' 감시 종료.", chat_id=chat_id)
        else:
            send_text_message("목록에 없는 키워드입니다.", chat_id=chat_id)
    elif cmd in ('list', '목록'):
        keywords = load_keywords()
        send_text_message(f"📋 현재 감시 중: {list(keywords)}", chat_id=chat_id)
    elif cmd in ('status', '상태'):
        send_status_report(chat_id=chat_id)
    elif cmd in ('test', '테스트'):
        if arg:
            send_test_trend(arg, chat_id=chat_id)
        else:
            keywords = sorted(load_keywords())
            if not keywords:
                send_text_message("🧪 테스트할 감시 키워드가 없습니다. 먼저 키워드를 추가하세요.", chat_id=chat_id)
            else:
                send_test_trend(random.choice(keywords), chat_id=chat_id)
    elif cmd in ('help', 'start', '도움'):
        send_text_message("명령어: 추가 [단어], 삭제 [단어], 목록, 상태, 테스트 [단어]", chat_id=chat_id)
    else:
        send_text_message("알 수 없는 명령어입니다. 사용법: 추가, 삭제, 목록, 상태, 테스트", chat_id=chat_id)


def process_telegram_update(update):
    message = update.get('message') or update.get('edited_message')
    if not message:
        return
    text = message.get('text')
    if not text:
        return
    chat_id = str(message['chat']['id'])
    text = text.strip()
    if text.startswith('/'):
        text = text[1:]
    parts = text.split()
    if not parts:
        return
    cmd = parts[0].lower()
    arg = ' '.join(parts[1:]) if len(parts) > 1 else None
    handle_command(cmd, arg, chat_id)


def telegram_poll_loop():
    offset = None
    while True:
        updates = get_telegram_updates(offset=offset, timeout=20)
        for update in updates:
            process_telegram_update(update)
            offset = update['update_id'] + 1
        time.sleep(TELEGRAM_POLL_INTERVAL)

# --- 4. 트렌드 분석 엔진 ---
last_scores = {}
last_trend_data = {}
last_fetch_time = {}

load_cache()

def create_pytrends():
    return TrendReq(
        hl='ko-KR',
        tz=540,
        timeout=(10, 25),
        retries=3,
        requests_args={
            'headers': get_request_headers(),
            'proxies': PROXIES,
            'verify': True,
        }
    )


def analyze_trend(keyword, is_first=False, chat_id=None):
    try:
        terms = parse_keyword_terms(keyword)
        if not is_first and keyword in last_trend_data and is_cache_fresh(keyword):
            data = last_trend_data[keyword]
            current_avg, prev_avg = get_recent_average(data, terms)
            current_score = int(round(current_avg)) if current_avg is not None else None
            logger.info(f"Using cached trend data for {keyword}: current_avg={current_avg}, prev_avg={prev_avg}")
        else:
            pytrends = create_pytrends()
            time.sleep(random.uniform(2.5, 5.0))

            retry_attempts = 3
            wait_seconds = 1.0
            data = None

            for attempt in range(1, retry_attempts + 1):
                try:
                    pytrends.build_payload(terms, timeframe=TREND_TIMEFRAME, geo=GEO)
                    data = pytrends.interest_over_time()
                    break
                except Exception as err:
                    err_text = str(err)
                    if '429' in err_text or 'Too Many Requests' in err_text:
                        logger.warning(f"429 detected during analyze_trend: {keyword}, attempt {attempt}/{retry_attempts}, wait {wait_seconds:.1f}s")
                        print(f"⚠️ 429 감지: 시도 {attempt}/{retry_attempts}, 대기 {wait_seconds:.1f}s")
                        if attempt == retry_attempts:
                            send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {err_text}", chat_id=chat_id)
                            logger.error(f"Google Trend 429 after retries: {keyword}, error={err_text}")
                            raise
                        time.sleep(wait_seconds)
                        wait_seconds *= 2
                        continue
                    raise

            if data is None or data.empty:
                current_score = None
                prev_avg = None
            else:
                current_avg, prev_avg = get_recent_average(data, terms)
                current_score = int(round(current_avg)) if current_avg is not None else None
                last_trend_data[keyword] = data
                last_fetch_time[keyword] = time.time()
                save_cache()

        if data is not None and not data.empty:
            logger.info(f"Analyzed trend for {keyword}: current_avg={current_avg}, prev_avg={prev_avg}")
            
            should_send = False
            diff = None

            if is_first:
                should_send = True
            elif prev_avg is not None:
                diff = current_avg - prev_avg
                if diff >= THRESHOLD_ACCEL or current_score >= THRESHOLD_HIGH:
                    should_send = True
            
            if should_send:
                send_trend_report(keyword, data, current_score, diff, is_first, chat_id=chat_id)
            
            last_scores[keyword] = current_score
    except Exception as e:
        error_text = str(e)
        logger.exception(f"{keyword} analyze_trend exception: {error_text}")
        print(f"⚠️ {keyword} 조회 오류: {error_text}")
        if '429' in error_text or 'Too Many Requests' in error_text:
            send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {error_text}", chat_id=chat_id)

# --- 5. 백그라운드 루프 & 메인 CLI ---
def monitor_loop():
    while True:
        # 15분(900초) 대기
        time.sleep(random.randint(900, 1000))
        keywords = load_keywords()

        for kw in list(keywords):
            if should_fetch_daily(kw):
                analyze_trend(kw)
                time.sleep(5)

def main():
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=telegram_poll_loop, daemon=True).start()
    
    print("="*45)
    print("🚀 TREND ACCELERATOR V3.0 (Graph Mode)")
    print("명령어: 추가 [단어], 삭제 [단어], 목록, 상태, 테스트 [단어], 종료")
    print("="*45)

    while True:
        try:
            user_in = input(">> ").strip().split()
            if not user_in: continue
            
            cmd = user_in[0].lower()
            arg = " ".join(user_in[1:]) if len(user_in) > 1 else None
            keywords = load_keywords()

            if cmd in ("add", "추가") and arg:
                keywords.add(arg)
                save_keywords(keywords)
                analyze_trend(arg, True)
                print(f"➕ '{arg}' 추가됨. 그래프가 전송됩니다.")

            elif cmd in ("del", "삭제") and arg:
                if arg in keywords:
                    keywords.remove(arg)
                    save_keywords(keywords)
                    last_scores.pop(arg, None)
                    print(f"🗑️ '{arg}' 감시 종료.")
                else:
                    print("목록에 없습니다.")

            elif cmd in ("list", "목록"):
                print(f"📋 현재 감시 중: {list(keywords)}")

            elif cmd in ("status", "상태"):
                status_msg = build_status_text()
                print(status_msg)
                send_status_report()

            elif cmd in ("test", "테스트"):
                if arg:
                    send_test_trend(arg)
                elif not keywords:
                    print("🧪 테스트할 감시 키워드가 없습니다. 먼저 키워드를 추가하세요.")
                else:
                    choice = random.choice(sorted(keywords))
                    print(f"🧪 '{choice}' 키워드로 테스트 트렌드 그래프를 전송합니다.")
                    send_test_trend(choice)

            elif cmd in ("exit", "종료"):
                break
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
