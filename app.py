"""배틀그라운드 닉네임 수집 및 관리 시스템 (Streamlit)."""

import base64
from contextlib import contextmanager
from datetime import datetime
from email.message import EmailMessage
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import gzip
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
import pandas as pd
import streamlit as st

try:
  import streamlit.components.v1 as components
except ImportError:
  components = None

try:
  from google import genai
except ImportError:
  genai = None

try:
  import openai
except ImportError:
  openai = None

try:
  from cryptography.fernet import Fernet, InvalidToken
except ImportError:
  Fernet = None
  InvalidToken = Exception

try:  # 선택: 드래그로 영역 지정 (pip install streamlit-cropper)
  from streamlit_cropper import st_cropper
except ImportError:
  st_cropper = None

# 페이지 설정 (Streamlit 첫 호출이어야 함)
# 시간대 (리눅스 호스팅은 기본 UTC라 한국 시간으로 맞춘다. 환경변수 APP_TIMEZONE으로 변경 가능)
if hasattr(time, 'tzset'):
  os.environ['TZ'] = os.environ.get('APP_TIMEZONE', 'Asia/Seoul')
  time.tzset()

st.set_page_config(
    page_title='배틀그라운드 닉네임 수집 및 관리 시스템',
    page_icon='🎮',
    layout='wide',
)

# ==========================================
# 0. 설정 상수
# ==========================================
DB_FILE = os.environ.get('PUBG_DB_FILE', 'pubg_manager.db')
KEY_FILE = '.app_secret.key'
PBKDF2_ITERATIONS = 200_000
MAX_SQUAD = 4
MAX_NICK_LEN = 32
MAX_UPLOAD_MB = 10
MAX_BATCH_FILES = 8  # 한 번에 처리할 스크린샷 수
MAX_IMAGE_SIDE = 3840  # 업로드 이미지 보관 해상도 (작은 글씨 보존)
MAX_FULL_SIDE = 2000  # 전체 화면 인식 시 해상도 제한
MAX_SEND_SIDE = 2400  # AI 전송 시 해상도 제한
MAX_TARGET_SIDE = 4000  # 업스케일 후 최대 변 길이
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 60
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,20}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# 권한: 최종관리자 > 일반관리자 > 일반회원
ROLE_SUPER, ROLE_ADMIN, ROLE_USER = 'super', 'admin', 'user'
ROLE_LABELS = {
    ROLE_SUPER: '👑 최종관리자',
    ROLE_ADMIN: '🛡️ 일반관리자',
    ROLE_USER: '일반회원',
}
ROLE_RANK = {ROLE_USER: 0, ROLE_ADMIN: 1, ROLE_SUPER: 2}

CODE_TTL_SECONDS = 600  # 이메일 인증 코드 유효 시간
CODE_MAX_ATTEMPTS = 5
CODE_RESEND_SECONDS = 60
TWOFA_PENDING = '__2fa__'  # login_user가 2단계 인증 대기 상태일 때 돌려주는 값
SETUP_CODE_FILE = 'setup_code.txt'

# 자동 클라우드 동기화 (GitHub 비공개 저장소에 암호화 스냅샷 저장)
SYNC_MIN_INTERVAL = 60  # 변경 후 업로드까지 최소 간격(초)
SYNC_DEFAULT_PATH = 'pubg_manager.db.enc'

# 로그인 상태 유지(자동 로그인)
REMEMBER_COOKIE = 'pubg_rt'
REMEMBER_IDLE_DAYS = 14  # 이 기간 동안 접속이 없으면 만료 (접속할 때마다 연장)
REMEMBER_MAX_DAYS = 30  # 연장해도 최초 로그인 후 이 기간이 지나면 다시 로그인
REMEMBER_GRACE_SECONDS = 120  # 토큰 교체 직후 이전 토큰의 유예 시간
REMEMBER_MAX_DEVICES = 10

FREE_OCR = '무료 기본 OCR (EasyOCR)'
CUSTOM_OPENROUTER = 'openrouter:custom'

# 제공업체 설정 (OpenAI 호환 API는 base_url만 다름)
PROVIDERS = {
    'gemini': {
        'name': 'Google Gemini',
        'env': 'GEMINI_API_KEY',
        'url': 'https://aistudio.google.com/apikey',
        'base_url': None,
        'note': '무료 티어 제공 (Flash / Flash-Lite 계열)',
    },
    'groq': {
        'name': 'Groq',
        'env': 'GROQ_API_KEY',
        'url': 'https://console.groq.com/keys',
        'base_url': 'https://api.groq.com/openai/v1',
        'note': '무료 티어 제공',
    },
    'openai': {
        'name': 'OpenAI',
        'env': 'OPENAI_API_KEY',
        'url': 'https://platform.openai.com/api-keys',
        'base_url': None,
        'note': '유료',
    },
    'openrouter': {
        'name': 'OpenRouter',
        'env': 'OPENROUTER_API_KEY',
        'url': 'https://openrouter.ai/keys',
        'base_url': 'https://openrouter.ai/api/v1',
        'note': '모델 ID 끝이 ":free"인 비전 모델은 무료',
    },
}

# 전적 사이트 (닉네임 클릭 → 프로필 바로 열기). 다른 사이트는 환경변수로 교체 가능:
#   STATS_URL_TEMPLATE='https://example.com/{platform}/{nickname}'
STATS_URL_TEMPLATE = os.environ.get(
    'STATS_URL_TEMPLATE', 'https://dak.gg/pubg/profile/{platform}/{nickname}'
)
STATS_PLATFORMS = {'Steam': 'steam', 'Kakao': 'kakao'}

# (저장용 ID, 표시 이름, 제공업체)  ※ 종료된 모델은 제외
MODEL_CATALOG = [
    (FREE_OCR, FREE_OCR, 'local'),
    ('gemini-3.8-flash', 'gemini-3.8-flash (최신 · 고성능)', 'gemini'),
    ('gemini-3.6-flash', 'gemini-3.6-flash (균형형)', 'gemini'),
    ('gemini-3.5-flash-lite', 'gemini-3.5-flash-lite (빠르고 저렴)', 'gemini'),
    ('gemini-3.1-flash-lite', 'gemini-3.1-flash-lite (가벼움)', 'gemini'),
    ('qwen/qwen3.6-27b', 'Groq · qwen3.6-27b (무료 티어)', 'groq'),
    ('gpt-5.4-mini', 'OpenAI · gpt-5.4-mini (저렴)', 'openai'),
    ('gpt-5.5', 'OpenAI · gpt-5.5 (고성능)', 'openai'),
    (CUSTOM_OPENROUTER, 'OpenRouter · 모델 ID 직접 입력', 'openrouter'),
]
MODEL_BY_ID = {m[0]: m for m in MODEL_CATALOG}

# 스쿼드 목록 기본 인식 영역 (왼쪽 하단, 이미지 크기 대비 %)
DEFAULT_CROP_X = (0.0, 20.0)
DEFAULT_CROP_Y = (82.0, 100.0)
OCR_MIN_WIDTH = 900  # '자동' 업스케일: 크롭 이미지를 이 너비까지 확대
UPSCALE_OPTIONS = {'자동': 0, '끔': 1, '2x': 2, '3x': 3, '4x': 4}

# '[HOT6] 닉네임' 형태: 클랜 태그와 닉네임 분리
# (맨 앞 슬롯 번호 한 글자 오인식은 무시, 괄호 오인식 대비)
CLAN_RE = re.compile(
    r'^\s*(?:\S\s+)?[\[\(\{]\s*([^\]\)\}]{0,12}?)\s*[\]\)\}]\s*(.*)$'
)
MAX_CLAN_LEN = 12

AI_PROMPT = (
    '이 배틀그라운드 스크린샷에서 왼쪽 하단 스쿼드 목록(1~4번 슬롯)에 표시된'
    f' 팀원을 위에서 아래 순서대로 최대 {MAX_SQUAD}명까지 읽어주세요.'
    ' 한 줄에 한 명씩 "클랜태그 | 닉네임" 형식으로 적고, 클랜 태그는'
    ' 대괄호 [] 안의 글자이며 대괄호 기호는 빼고 적습니다. 클랜 태그가'
    ' 없으면 닉네임만 적으세요. 예) ABCD | PlayerOne. 캐릭터 머리 위 이름이나'
    ' 다른 문구는 무시하고, 다른 설명이나 번호는 절대 적지 마세요.'
)


# ==========================================
# 1. 보안 유틸 (비밀번호 해시 / API 키 암호화)
# ==========================================
def hash_password(password):
  salt = secrets.token_hex(16)
  digest = hashlib.pbkdf2_hmac(
      'sha256', password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
  )
  return f'pbkdf2${PBKDF2_ITERATIONS}${salt}${digest.hex()}'


def verify_password(password, stored):
  """신규(PBKDF2) 및 구버전(SHA-256) 해시를 모두 검증한다."""
  if stored.startswith('pbkdf2$'):
    try:
      _, iterations, salt, expected = stored.split('$')
      digest = hashlib.pbkdf2_hmac(
          'sha256', password.encode(), bytes.fromhex(salt), int(iterations)
      )
    except ValueError:
      return False
    return hmac.compare_digest(digest.hex(), expected)
  legacy = hashlib.sha256(password.encode()).hexdigest()
  return hmac.compare_digest(legacy, stored)


def get_secret(name, default=''):
  """환경변수 → Streamlit Secrets 순으로 조회한다 (호스팅의 Secrets 설정 지원)."""
  value = os.environ.get(name)
  if value:
    return value
  try:
    value = st.secrets.get(name)
  except Exception:  # secrets 파일/설정이 없는 환경
    return default
  return str(value) if value else default


@st.cache_resource
def get_fernet():
  """API 키 암호화용 Fernet. cryptography 미설치 시 None."""
  if Fernet is None:
    return None
  secret = get_secret('APP_SECRET_KEY')
  if secret:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
  elif os.path.exists(KEY_FILE):
    with open(KEY_FILE, 'rb') as f:
      key = f.read().strip()
  else:
    key = Fernet.generate_key()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
      f.write(key)
  return Fernet(key)


def encrypt_secret(plain):
  if not plain:
    return ''
  fernet = get_fernet()
  if fernet is None:
    return plain
  return 'enc:' + fernet.encrypt(plain.encode()).decode()


def decrypt_secret(stored):
  if not stored:
    return ''
  if not stored.startswith('enc:'):
    return stored
  fernet = get_fernet()
  if fernet is None:
    return ''
  try:
    return fernet.decrypt(stored[4:].encode()).decode()
  except InvalidToken:
    return ''


# ==========================================
# 2. 데이터베이스
# ==========================================
@contextmanager
def db():
  conn = sqlite3.connect(DB_FILE, timeout=10)
  changes_before = conn.total_changes
  try:
    yield conn
    conn.commit()
    if conn.total_changes != changes_before:
      mark_dirty()  # 데이터가 바뀌었으면 클라우드 동기화 예약
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.close()


def now_str():
  return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ==========================================
# 2-1. 자동 클라우드 동기화 (재시작해도 데이터가 사라지지 않도록)
# ==========================================
@st.cache_resource
def sync_state():
  """프로세스 전체에서 공유되는 동기화 상태."""
  return {
      'lock': threading.Lock(), 'dirty': False, 'pushing': False,
      'timer_active': False, 'timer': None, 'timer_due': 0.0,
      'ready': False, 'last_push': 0.0,
      'last_ok': '', 'last_error': '', 'sha': None, 'last_attempt': 0.0,
      'restored': '', 'pushes': 0,
  }


def sync_config():
  token = get_secret('GITHUB_SYNC_TOKEN')
  repo = get_secret('GITHUB_SYNC_REPO')
  if not (token and repo):
    return None
  return {
      'token': token,
      'repo': repo.strip().strip('/'),
      'branch': get_secret('GITHUB_SYNC_BRANCH') or 'main',
      'path': get_secret('GITHUB_SYNC_PATH') or SYNC_DEFAULT_PATH,
      'api': (get_secret('GITHUB_API_URL') or 'https://api.github.com').rstrip('/'),
      'fernet': get_fernet(),
  }


def sync_problem():
  """동기화를 켤 수 없는 이유 (정상이면 빈 문자열)."""
  if not sync_config():
    return '설정되지 않음'
  if Fernet is None:
    return '`cryptography` 패키지가 필요합니다 (requirements.txt에 추가).'
  if not get_secret('APP_SECRET_KEY'):
    return '`APP_SECRET_KEY`를 Secrets에 고정해야 합니다 (스냅샷 암호화 키, 없으면 복구할 수 없어요).'
  return ''


def snapshot_db_bytes():
  """SQLite 백업 API로 일관된 DB 스냅샷(bytes)을 만든다 (권한 검사 없음)."""
  with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, 'snapshot.db')
    src = sqlite3.connect(DB_FILE)
    dst = sqlite3.connect(path)
    try:
      src.backup(dst)
    finally:
      dst.close()
      src.close()
    with open(path, 'rb') as f:
      return f.read()


def pack_snapshot(raw, fernet):
  return fernet.encrypt(gzip.compress(raw, 6))


def unpack_snapshot(blob, fernet):
  return gzip.decompress(fernet.decrypt(blob))


def _gh_request(cfg, method, path, body=None, accept='application/vnd.github+json', timeout=25):
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(cfg['api'] + path, data=data, method=method)
  req.add_header('Authorization', f"Bearer {cfg['token']}")
  req.add_header('Accept', accept)
  req.add_header('X-GitHub-Api-Version', '2022-11-28')
  req.add_header('User-Agent', 'pubg-nickname-manager')
  if data is not None:
    req.add_header('Content-Type', 'application/json')
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      return resp.status, resp.read()
  except urllib.error.HTTPError as e:
    return e.code, e.read()


def _contents_path(cfg):
  return f"/repos/{cfg['repo']}/contents/{quote(cfg['path'])}"


def github_fetch(cfg):
  """원격 스냅샷 바이트. 파일이 없으면 None."""
  status, body = _gh_request(
      cfg, 'GET', f"{_contents_path(cfg)}?ref={quote(cfg['branch'])}",
      accept='application/vnd.github.raw+json',
  )
  if status == 200:
    return body
  if status == 404:
    return None
  raise RuntimeError(f'GitHub 응답 {status}: {body[:200]!r}')


def github_sha(cfg):
  status, body = _gh_request(cfg, 'GET', f"{_contents_path(cfg)}?ref={quote(cfg['branch'])}")
  if status == 200:
    return json.loads(body)['sha']
  if status == 404:
    return None
  raise RuntimeError(f'GitHub 응답 {status}: {body[:200]!r}')


def github_put(cfg, blob, state):
  for attempt in range(2):
    sha = state['sha'] if (attempt == 0 and state['sha']) else github_sha(cfg)
    body = {
        'message': f'snapshot {now_str()}',
        'content': base64.b64encode(blob).decode(),
        'branch': cfg['branch'],
    }
    if sha:
      body['sha'] = sha
    status, resp = _gh_request(cfg, 'PUT', _contents_path(cfg), body)
    if status in (200, 201):
      state['sha'] = json.loads(resp)['content']['sha']
      return
    if status in (409, 422) and attempt == 0:  # sha가 어긋남 → 최신 sha로 재시도
      state['sha'] = None
      continue
    raise RuntimeError(f'GitHub 저장 실패 {status}: {resp[:200]!r}')


def schedule_push(cfg, state, delay=None):
  """업로드를 예약한다. 이미 예약돼 있어도 더 빠른 예약이면 교체한다."""
  with state['lock']:
    state['dirty'] = True
    if state['pushing']:  # 업로드 중이면 끝난 뒤 다시 예약됨
      return
    if delay is None:
      delay = max(0.0, SYNC_MIN_INTERVAL - (time.time() - state['last_push']))
    due = time.time() + delay
    if state['timer_active'] and state['timer_due'] <= due + 0.01:
      return
    if state['timer'] is not None:
      state['timer'].cancel()
    timer = threading.Timer(delay, push_worker, args=(cfg, state))
    timer.daemon = True
    state['timer'] = timer
    state['timer_active'] = True
    state['timer_due'] = due
  timer.start()


def push_worker(cfg, state):
  with state['lock']:
    state['timer_active'] = False
    state['timer'] = None
    if not state['dirty'] or state['pushing']:
      return
    state['dirty'] = False
    state['pushing'] = True
  error = ''
  try:
    github_put(cfg, pack_snapshot(snapshot_db_bytes(), cfg['fernet']), state)
  except Exception as e:  # 실패하면 다시 시도
    error = f'{type(e).__name__}: {e}'
  with state['lock']:
    state['pushing'] = False
    state['last_push'] = time.time()
    if error:
      state['last_error'] = error
      state['dirty'] = True
    else:
      state['last_ok'] = now_str()
      state['last_error'] = ''
      state['pushes'] += 1
    again = state['dirty']
  if again:
    schedule_push(cfg, state, delay=120 if error else SYNC_MIN_INTERVAL)


def mark_dirty():
  """DB가 바뀌었음을 알린다 (동기화가 꺼져 있으면 아무 일도 안 함)."""
  cfg = sync_config()
  if not cfg or sync_problem():
    return
  state = sync_state()
  if not state['ready']:  # 원격 확인 전에는 업로드 금지 (원격 데이터를 덮어쓰지 않도록)
    return
  schedule_push(cfg, state)


def sync_push_now():
  """즉시 업로드 (관리자 버튼). (성공 여부, 메시지)."""
  problem = sync_problem()
  if problem:
    return False, problem
  cfg, state = sync_config(), sync_state()
  if not state['ready']:
    return False, '시작 시 원격 확인이 끝나지 않았어요. 연결 테스트로 원인을 확인하세요.'
  with state['lock']:
    state['dirty'] = False
    state['pushing'] = True
  try:
    github_put(cfg, pack_snapshot(snapshot_db_bytes(), cfg['fernet']), state)
  except Exception as e:
    with state['lock']:
      state['pushing'] = False
      state['dirty'] = True
      state['last_error'] = f'{type(e).__name__}: {e}'
    return False, state['last_error']
  with state['lock']:
    state['pushing'] = False
    state['last_push'] = time.time()
    state['last_ok'] = now_str()
    state['last_error'] = ''
    state['pushes'] += 1
  return True, '업로드했습니다.'


def sync_test():
  """저장소 접근, 브랜치, 스냅샷 존재 여부를 점검한다. (성공 여부, 메시지)."""
  problem = sync_problem()
  if problem:
    return False, problem
  cfg = sync_config()
  try:
    status, body = _gh_request(cfg, 'GET', f"/repos/{cfg['repo']}")
    if status != 200:
      return False, f'저장소에 접근할 수 없어요 (응답 {status}). 저장소 이름과 토큰 권한을 확인하세요.'
    blob = github_fetch(cfg)
  except Exception as e:
    return False, f'{type(e).__name__}: {e}'
  if blob is None:
    return True, '연결 정상 · 아직 저장된 스냅샷은 없어요 (첫 변경 후 자동 업로드됩니다).'
  try:
    unpack_snapshot(blob, cfg['fernet'])
  except Exception:
    return False, '스냅샷이 있지만 복호화에 실패했어요. APP_SECRET_KEY가 업로드 때와 다른 것 같아요.'
  return True, f'연결 정상 · 스냅샷 {len(blob) / 1024:,.1f} KB 확인'


def sync_pull_restore():
  """원격 스냅샷으로 현재 DB를 교체한다 (관리자 버튼). (성공 여부, 메시지)."""
  problem = sync_problem()
  if problem:
    return False, problem
  cfg = sync_config()
  try:
    blob = github_fetch(cfg)
    if blob is None:
      return False, '원격에 저장된 스냅샷이 없습니다.'
    return restore_database(unpack_snapshot(blob, cfg['fernet']))
  except Exception as e:
    return False, f'{type(e).__name__}: {e}'


def sync_bootstrap():
  """시작 시 1회: 로컬 DB가 비어 있고 원격 스냅샷이 있으면 자동 복원한다.

  이 확인이 끝나기 전에는 업로드하지 않는다 (빈 DB가 원격 데이터를 덮어쓰는 사고 방지).
  """
  state = sync_state()
  if state['ready'] or sync_problem():
    return
  if time.time() - state['last_attempt'] < 60:
    return
  state['last_attempt'] = time.time()
  cfg = sync_config()
  try:
    with db() as conn:
      users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if users > 0:
      state['restored'] = '로컬 데이터를 그대로 사용'
      state['ready'] = True
      return
    blob = github_fetch(cfg)
    if blob is None:
      state['restored'] = '원격 스냅샷 없음 (새로 시작)'
      state['ready'] = True
      return
    ok, msg = restore_database(unpack_snapshot(blob, cfg['fernet']))
    if not ok:
      raise RuntimeError(msg)
    state['restored'] = f'원격에서 자동 복원: {msg}'
    state['ready'] = True
    state['last_error'] = ''
  except Exception as e:
    state['last_error'] = f'시작 시 복원 확인 실패: {type(e).__name__}: {e}'


def init_db():
  with db() as conn:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            api_key TEXT,
            model_name TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nicknames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            nickname TEXT,
            clan TEXT DEFAULT '',
            source_image TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_keys (
            username TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            PRIMARY KEY (username, provider)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin TEXT,
            action TEXT,
            target TEXT,
            detail TEXT,
            created_at TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO app_settings VALUES ('allow_signup', '1')"
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_nick_user ON nicknames(username)'
    )
    # 구버전 DB에 clan 컬럼 추가
    cols = {r[1] for r in conn.execute('PRAGMA table_info(nicknames)')}
    if 'clan' not in cols:
      conn.execute("ALTER TABLE nicknames ADD COLUMN clan TEXT DEFAULT ''")

    # 구버전(users.api_key 단일 컬럼) → 제공업체별 암호화 저장으로 이전
    legacy = conn.execute(
        "SELECT username, api_key, model_name FROM users"
        " WHERE api_key IS NOT NULL AND api_key != ''"
    ).fetchall()
    for username, key, model in legacy:
      provider = 'openai' if (model or '').startswith('gpt') else 'gemini'
      conn.execute(
          'INSERT OR IGNORE INTO user_keys VALUES (?, ?, ?)',
          (username, provider, encrypt_secret(key)),
      )
      conn.execute("UPDATE users SET api_key = '' WHERE username = ?", (username,))

    # 권한(role) / 이메일 컬럼 추가 (구버전 DB 호환)
    ucols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
    if 'role' not in ucols:
      conn.execute('ALTER TABLE users ADD COLUMN role TEXT')
    if 'email' not in ucols:
      conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if 'email_verified' not in ucols:
      conn.execute('ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0')
    conn.execute(
        "UPDATE users SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'user' END"
        " WHERE role IS NULL OR role = ''"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_signups (
            username TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER DEFAULT 0,
            sent_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_tokens (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_used REAL NOT NULL
        )
    """)
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_token_user ON login_tokens(username)'
    )
    if 'twofa' not in ucols:
      conn.execute('ALTER TABLE users ADD COLUMN twofa INTEGER DEFAULT 0')
    if 'login_alert' not in ucols:
      conn.execute('ALTER TABLE users ADD COLUMN login_alert INTEGER DEFAULT 0')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_codes (
            purpose TEXT NOT NULL,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER DEFAULT 0,
            sent_at REAL NOT NULL,
            PRIMARY KEY (purpose, username)
        )
    """)

    # 함께한 사람(스쿼드 기록) / 개인 메모·즐겨찾기 / 내 닉네임
    conn.execute("""
        CREATE TABLE IF NOT EXISTS squad_members (
            squad_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            nickname TEXT NOT NULL,
            clan TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_squad_id ON squad_members(squad_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_squad_owner ON squad_members(owner)')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nick_notes (
            owner TEXT NOT NULL,
            nick_key TEXT NOT NULL,
            nickname TEXT NOT NULL,
            favorite INTEGER DEFAULT 0,
            tags TEXT DEFAULT '',
            note TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner, nick_key)
        )
    """)
    if 'my_nickname' not in ucols:
      conn.execute("ALTER TABLE users ADD COLUMN my_nickname TEXT DEFAULT ''")
    # 구버전 데이터: 같은 시각·같은 출처로 저장된 닉네임을 한 판으로 간주 (최초 1회)
    if conn.execute('SELECT COUNT(*) FROM squad_members').fetchone()[0] == 0:
        groups = {}
        for u, ts, src, nick, clan in conn.execute(
            "SELECT username, created_at, COALESCE(source_image, ''), nickname,"
            " COALESCE(clan, '') FROM nicknames WHERE created_at IS NOT NULL ORDER BY id"
        ).fetchall():
          groups.setdefault((u, ts, src), []).append((nick, clan))
        for (u, ts, _src), members in groups.items():
          if len(members) >= 2:
            sid = secrets.token_hex(6)
            conn.executemany(
                'INSERT INTO squad_members VALUES (?, ?, ?, ?, ?)',
                [(sid, u, n, c, ts) for n, c in members],
            )


def super_exists():
  with db() as conn:
    return bool(
        conn.execute("SELECT 1 FROM users WHERE role = 'super'").fetchone()
    )


def get_setup_code():
  """최종관리자 최초 설정용 코드. 환경변수 또는 setup_code.txt 파일에 보관."""
  env = get_secret('ADMIN_SETUP_CODE')
  if env:
    return env
  if os.path.exists(SETUP_CODE_FILE):
    with open(SETUP_CODE_FILE, encoding='utf-8') as f:
      return f.read().strip()
  code = secrets.token_hex(4)
  with open(SETUP_CODE_FILE, 'w', encoding='utf-8') as f:
    f.write(code + '\n')
  print(
      f'[최종관리자 설정 코드] {code}  (파일: {os.path.abspath(SETUP_CODE_FILE)})',
      flush=True,
  )
  return code


init_db()
if not super_exists():
  get_setup_code()  # 설정 코드 파일 생성 + 콘솔 출력


# 세션 상태 초기화
for _key, _default in {
    'logged_in': False,
    'username': '',
    'is_admin': 0,
    'role': 'user',
    'signup_pending': '',
    'last_mail_at': 0.0,
    'must_change_pw': False,
    'login_fails': 0,
    'lock_until': 0.0,
    'extract_ver': 0,
}.items():
  st.session_state.setdefault(_key, _default)


def flash(message, kind='success'):
  """st.rerun() 이후에도 보이는 알림."""
  st.session_state['_flash'] = (kind, message)


def show_flash():
  item = st.session_state.pop('_flash', None)
  if item:
    getattr(st, item[0])(item[1])


# ==========================================
# 3. 인증
# ==========================================
def check_login_lock():
  now = time.time()
  if now < st.session_state.lock_until:
    wait = int(st.session_state.lock_until - now) + 1
    return f'시도가 너무 많습니다. {wait}초 후 다시 시도해 주세요.'
  return ''


def note_auth_failure():
  st.session_state.login_fails += 1
  if st.session_state.login_fails >= MAX_LOGIN_FAILS:
    st.session_state.lock_until = time.time() + LOGIN_LOCK_SECONDS
    st.session_state.login_fails = 0


def normalize_role(role, is_admin=0):
  if role in ROLE_RANK:
    return role
  return ROLE_ADMIN if is_admin else ROLE_USER


def open_session(username, remember=False, must_change_pw=False):
  """로그인 성공 처리: 세션 상태 설정 + (선택) 로그인 유지 토큰 발급."""
  with db() as conn:
    row = conn.execute(
        'SELECT role, is_admin FROM users WHERE username = ?', (username,)
    ).fetchone()
  role = normalize_role(row[0], row[1]) if row else ROLE_USER
  st.session_state.update(
      logged_in=True,
      username=username,
      role=role,
      is_admin=int(role != ROLE_USER),
      login_fails=0,
      must_change_pw=must_change_pw,
  )
  if remember and cookies_supported():
    issue_login_token(username)


def login_user(username, password, remember=False):
  """(성공 여부, 메시지). 2단계 인증이 필요하면 (False, TWOFA_PENDING)."""
  locked = check_login_lock()
  if locked:
    return False, locked

  with db() as conn:
    row = conn.execute(
        'SELECT password, twofa, email, email_verified FROM users WHERE username = ?',
        (username,),
    ).fetchone()

  if row and verify_password(password, row[0]):
    if not row[0].startswith('pbkdf2$'):  # 구버전 해시 자동 업그레이드
      with db() as conn:
        conn.execute(
            'UPDATE users SET password = ? WHERE username = ?',
            (hash_password(password), username),
        )
    weak = password == 'admin123'
    if row[1] and row[3] and row[2] and smtp_configured():  # 2단계 인증
      sent, reason = issue_email_code(
          '2fa', username, row[2], '[배틀그라운드 닉네임 관리] 로그인 인증 코드',
          '로그인을 시도하셨습니다.',
      )
      if not sent and reason != 'cooldown':
        return False, '인증 메일을 보내지 못했습니다. 잠시 후 다시 시도하거나 관리자에게 문의하세요.'
      st.session_state.twofa_pending = {
          'username': username, 'remember': bool(remember), 'weak': weak,
      }
      return False, TWOFA_PENDING
    open_session(username, remember, must_change_pw=weak)
    notify_login(username)
    return True, ''

  note_auth_failure()
  return False, '아이디 또는 비밀번호가 올바르지 않습니다.'


def finish_twofa(code):
  pending = st.session_state.get('twofa_pending')
  if not pending:
    return False, '로그인을 처음부터 다시 시도해 주세요.'
  locked = check_login_lock()
  if locked:
    return False, locked
  ok, msg, _ = verify_email_code('2fa', pending['username'], code)
  if not ok:
    note_auth_failure()
    with db() as conn:
      still = conn.execute(
          "SELECT 1 FROM email_codes WHERE purpose = '2fa' AND username = ?",
          (pending['username'],),
      ).fetchone()
    if not still:  # 만료/시도 초과로 코드가 삭제됨 → 처음부터
      st.session_state.twofa_pending = None
    return False, msg
  open_session(pending['username'], pending['remember'], pending['weak'])
  st.session_state.twofa_pending = None
  notify_login(pending['username'])
  return True, ''


# ---------- 로그인 상태 유지 (쿠키 + 서버 저장 토큰) ----------
def _token_hash(token):
  return hashlib.sha256(token.encode()).hexdigest()


def cookies_supported():
  try:
    st.context.cookies  # Streamlit 1.37+
    return True
  except Exception:
    return False


def read_remember_cookie():
  try:
    return st.context.cookies.get(REMEMBER_COOKIE, '') or ''
  except Exception:
    return ''


def queue_cookie(action, token='', seconds=0):
  """브라우저 쿠키 설정/삭제 명령을 예약한다 (다음 화면 그리기 때 전송)."""
  st.session_state['cookie_cmd'] = (action, token, int(seconds))


def flush_cookie_cmd():
  cmd = st.session_state.pop('cookie_cmd', None)
  if not cmd or components is None:
    return
  action, token, seconds = cmd
  value = json.dumps(token if action == 'set' else '')
  max_age = max(seconds, 0) if action == 'set' else 0
  components.html(
      f"""<script>
      (function () {{
        try {{
          var p = window.parent;
          var secure = p.location.protocol === 'https:' ? '; Secure' : '';
          p.document.cookie = '{REMEMBER_COOKIE}=' + encodeURIComponent({value}) +
              '; max-age={max_age}; path=/; SameSite=Lax' + secure;
        }} catch (e) {{}}
      }})();
      </script>""",
      height=0,
  )


def issue_login_token(username, issued_at=None):
  """새 로그인 유지 토큰을 발급해 쿠키로 내려보낸다 (서버에는 해시만 저장)."""
  now = time.time()
  issued_at = issued_at or now
  expires = min(
      now + REMEMBER_IDLE_DAYS * 86400, issued_at + REMEMBER_MAX_DAYS * 86400
  )
  token = secrets.token_urlsafe(32)
  with db() as conn:
    conn.execute('DELETE FROM login_tokens WHERE expires_at < ?', (now,))
    conn.execute(
        'INSERT INTO login_tokens VALUES (?, ?, ?, ?, ?)',
        (_token_hash(token), username, issued_at, expires, now),
    )
    conn.execute(  # 기기 수 제한: 오래된 것부터 정리
        'DELETE FROM login_tokens WHERE username = ? AND token_hash IN ('
        ' SELECT token_hash FROM login_tokens WHERE username = ?'
        ' ORDER BY issued_at DESC LIMIT -1 OFFSET ?)',
        (username, username, REMEMBER_MAX_DEVICES),
    )
  queue_cookie('set', token, expires - now)
  return token


def login_with_cookie():
  """쿠키의 토큰이 유효하면 자동 로그인하고 토큰을 교체한다."""
  token = read_remember_cookie()
  if not token:
    return False
  now = time.time()
  thash = _token_hash(token)
  with db() as conn:
    row = conn.execute(
        'SELECT username, issued_at, expires_at FROM login_tokens WHERE token_hash = ?',
        (thash,),
    ).fetchone()
    user = (
        conn.execute(
            'SELECT role, is_admin FROM users WHERE username = ?', (row[0],)
        ).fetchone()
        if row
        else None
    )
    if not row or row[2] < now or not user:
      conn.execute('DELETE FROM login_tokens WHERE token_hash = ?', (thash,))
      queue_cookie('clear')
      return False
    # 사용한 토큰은 잠깐만 더 유효 (새 쿠키가 도착하기 전 새로고침 대비)
    conn.execute(
        'UPDATE login_tokens SET expires_at = ?, last_used = ? WHERE token_hash = ?',
        (min(row[2], now + REMEMBER_GRACE_SECONDS), now, thash),
    )
  role = normalize_role(user[0], user[1])
  st.session_state.update(
      logged_in=True, username=row[0], role=role, is_admin=int(role != ROLE_USER),
      login_fails=0, must_change_pw=False,
  )
  issue_login_token(row[0], issued_at=row[1])
  return True


def logout():
  token = read_remember_cookie()
  if token:
    with db() as conn:
      conn.execute(
          'DELETE FROM login_tokens WHERE token_hash = ?', (_token_hash(token),)
      )
    queue_cookie('clear')
  st.session_state.update(
      logged_in=False, username='', role=ROLE_USER, is_admin=0,
      must_change_pw=False,
  )
  st.session_state.pop('extracted_nicknames', None)
  st.session_state.pop('extracted_batches', None)


def refresh_session_role():
  """매 실행마다 DB의 최신 권한을 반영한다 (삭제된 계정은 로그아웃)."""
  with db() as conn:
    row = conn.execute(
        'SELECT role, is_admin FROM users WHERE username = ?',
        (st.session_state.username,),
    ).fetchone()
  if not row:
    logout()
    return False
  role = normalize_role(row[0], row[1])
  st.session_state.role = role
  st.session_state.is_admin = int(role != ROLE_USER)
  return True


# ---------- 이메일(SMTP) ----------
def smtp_config():
  def pick(env_name, key, default=''):
    return get_secret(env_name) or get_setting(key, default)

  try:
    port = int(pick('SMTP_PORT', 'smtp_port', '465') or 465)
  except ValueError:
    port = 465
  user = pick('SMTP_USER', 'smtp_user')
  return {
      'host': pick('SMTP_HOST', 'smtp_host'),
      'port': port,
      'user': user,
      'password': get_secret('SMTP_PASSWORD')
      or decrypt_secret(get_setting('smtp_password', '')),
      'sender': pick('SMTP_FROM', 'smtp_from') or user,
      'security': pick('SMTP_SECURITY', 'smtp_security', 'ssl').lower(),
  }


def smtp_configured():
  cfg = smtp_config()
  return bool(cfg['host'] and cfg['user'] and cfg['password'])


def email_verification_on():
  """가입 시 이메일 인증을 실제로 요구하는지 (설정 ON + SMTP 구성 완료)."""
  return get_setting('require_email_verify', '1') == '1' and smtp_configured()


def send_email(to_addr, subject, body, cfg=None):
  cfg = cfg or smtp_config()
  if not (cfg['host'] and cfg['user'] and cfg['password']):
    raise RuntimeError('SMTP가 설정되지 않았습니다.')
  msg = EmailMessage()
  msg['Subject'] = subject
  msg['From'] = cfg['sender'] or cfg['user']
  msg['To'] = to_addr
  msg.set_content(body)
  context = ssl.create_default_context()
  if cfg['security'] == 'ssl':
    server = smtplib.SMTP_SSL(cfg['host'], cfg['port'], context=context, timeout=15)
  else:
    server = smtplib.SMTP(cfg['host'], cfg['port'], timeout=15)
  with server:
    if cfg['security'] == 'starttls':
      server.starttls(context=context)
    server.login(cfg['user'], cfg['password'])
    server.send_message(msg)


def mask_email(email):
  name, _, domain = email.partition('@')
  return f'{name[:2]}***@{domain}'


# ---------- 회원가입 ----------
def validate_signup(username, password, email, email_required):
  if get_setting('allow_signup', '1') != '1':
    return '현재 신규 회원가입이 중지되어 있습니다. 관리자에게 문의하세요.'
  if not USERNAME_RE.match(username):
    return '아이디는 영문/숫자/밑줄(_) 3~20자로 입력해 주세요.'
  if len(password) < 8:
    return '비밀번호는 8자 이상이어야 합니다.'
  if email or email_required:
    if not EMAIL_RE.match(email) or len(email) > 100:
      return '올바른 이메일 주소를 입력해 주세요.'
  with db() as conn:
    if conn.execute(
        'SELECT 1 FROM users WHERE username = ?', (username,)
    ).fetchone():
      return '이미 존재하는 사용자 아이디입니다.'
    if email and conn.execute(
        'SELECT 1 FROM users WHERE lower(email) = lower(?)', (email,)
    ).fetchone():
      return '이미 가입에 사용된 이메일입니다.'
  return ''


def register_user(username, password, email=''):
  """이메일 인증을 쓰지 않는 가입 (SMTP 미설정 / 인증 해제 시)."""
  email = email.strip().lower()
  err = validate_signup(username, password, email, False)
  if err:
    return False, err
  with db() as conn:
    conn.execute(
        'INSERT INTO users (username, password, api_key, model_name, is_admin,'
        ' role, email, email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (username, hash_password(password), '', FREE_OCR, 0, ROLE_USER,
         email, 0, now_str()),
    )
  return True, '회원가입이 완료되었습니다. 로그인해 주세요.'


def _code_hash(code, salt):
  return hashlib.sha256((salt + code).encode()).hexdigest()


def _issue_code(username, email, password_hash):
  """인증 코드를 만들어 메일로 보내고 가입 대기 정보를 저장한다."""
  code = f'{secrets.randbelow(10 ** 6):06d}'
  salt = secrets.token_hex(8)
  try:
    send_email(
        email,
        '[배틀그라운드 닉네임 관리] 이메일 인증 코드',
        f'인증 코드: {code}\n\n{CODE_TTL_SECONDS // 60}분 안에 입력해 주세요.\n'
        '본인이 요청하지 않았다면 이 메일을 무시하세요.',
    )
  except Exception as e:  # 상세 원인은 서버 콘솔에만 남긴다
    print(f'[메일 발송 실패] {type(e).__name__}: {e}', flush=True)
    return False, '메일 발송에 실패했습니다. 이메일 주소를 확인하거나 관리자에게 문의하세요.'
  now = time.time()
  with db() as conn:
    conn.execute(
        'INSERT OR REPLACE INTO pending_signups VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        (username, email, password_hash, _code_hash(code, salt), salt,
         now + CODE_TTL_SECONDS, now),
    )
  st.session_state.last_mail_at = now
  return True, f'{mask_email(email)} 로 인증 코드를 보냈습니다.'


def start_signup(username, password, email):
  email = email.strip().lower()
  err = validate_signup(username, password, email, True)
  if err:
    return False, err
  now = time.time()
  if now - st.session_state.last_mail_at < CODE_RESEND_SECONDS:
    return False, '잠시 후 다시 시도해 주세요 (메일 재발송 대기 중).'
  with db() as conn:
    conn.execute('DELETE FROM pending_signups WHERE expires_at < ?', (now - 3600,))
    if conn.execute(
        'SELECT 1 FROM pending_signups WHERE (username = ? OR lower(email) = ?)'
        ' AND sent_at > ?',
        (username, email, now - CODE_RESEND_SECONDS),
    ).fetchone():
      return False, '잠시 후 다시 시도해 주세요 (메일 재발송 대기 중).'
  return _issue_code(username, email, hash_password(password))


def pending_email(username):
  with db() as conn:
    row = conn.execute(
        'SELECT email FROM pending_signups WHERE username = ?', (username,)
    ).fetchone()
  return row[0] if row else ''


def resend_code(username):
  now = time.time()
  if now - st.session_state.last_mail_at < CODE_RESEND_SECONDS:
    wait = int(CODE_RESEND_SECONDS - (now - st.session_state.last_mail_at)) + 1
    return False, f'{wait}초 후에 다시 보낼 수 있어요.'
  with db() as conn:
    row = conn.execute(
        'SELECT email, password FROM pending_signups WHERE username = ?',
        (username,),
    ).fetchone()
  if not row:
    return False, '진행 중인 가입이 없습니다. 처음부터 다시 진행해 주세요.'
  return _issue_code(username, row[0], row[1])


def cancel_signup(username):
  with db() as conn:
    conn.execute('DELETE FROM pending_signups WHERE username = ?', (username,))


def finish_signup(username, code):
  code = (code or '').strip()
  with db() as conn:
    row = conn.execute(
        'SELECT email, password, code_hash, salt, expires_at, attempts'
        ' FROM pending_signups WHERE username = ?',
        (username,),
    ).fetchone()
    if not row:
      return False, '진행 중인 가입이 없습니다. 처음부터 다시 진행해 주세요.'
    email, pw_hash, code_hash, salt, expires_at, attempts = row
    if time.time() > expires_at:
      conn.execute('DELETE FROM pending_signups WHERE username = ?', (username,))
      return False, '인증 코드가 만료되었습니다. 처음부터 다시 진행해 주세요.'
    if attempts >= CODE_MAX_ATTEMPTS:
      conn.execute('DELETE FROM pending_signups WHERE username = ?', (username,))
      return False, '시도 횟수를 초과했습니다. 처음부터 다시 진행해 주세요.'
    if not hmac.compare_digest(_code_hash(code, salt), code_hash):
      conn.execute(
          'UPDATE pending_signups SET attempts = attempts + 1 WHERE username = ?',
          (username,),
      )
      left = CODE_MAX_ATTEMPTS - attempts - 1
      return False, f'인증 코드가 올바르지 않습니다. (남은 시도 {left}회)'
    if conn.execute(
        'SELECT 1 FROM users WHERE username = ? OR lower(email) = lower(?)',
        (username, email),
    ).fetchone():
      conn.execute('DELETE FROM pending_signups WHERE username = ?', (username,))
      return False, '이미 가입된 아이디 또는 이메일입니다.'
    conn.execute(
        'INSERT INTO users (username, password, api_key, model_name, is_admin,'
        ' role, email, email_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (username, pw_hash, '', FREE_OCR, 0, ROLE_USER, email, 1, now_str()),
    )
    conn.execute('DELETE FROM pending_signups WHERE username = ?', (username,))
  return True, '이메일 인증이 완료되어 가입되었습니다. 로그인해 주세요.'


# ---------- 이메일 코드 공통 (비밀번호 찾기 · 이메일 등록 · 2단계 인증) ----------
def issue_email_code(purpose, username, email, subject, intro):
  """6자리 코드를 만들어 메일로 보낸다. (성공 여부, 실패 사유: '' | 'cooldown' | 'send_failed')."""
  now = time.time()
  with db() as conn:
    row = conn.execute(
        'SELECT sent_at FROM email_codes WHERE purpose = ? AND username = ?',
        (purpose, username),
    ).fetchone()
  if row and now - row[0] < CODE_RESEND_SECONDS:
    return False, 'cooldown'
  code = f'{secrets.randbelow(10 ** 6):06d}'
  salt = secrets.token_hex(8)
  try:
    send_email(
        email, subject,
        f'{intro}\n\n인증 코드: {code}\n\n{CODE_TTL_SECONDS // 60}분 안에 입력해 주세요.\n'
        '본인이 요청하지 않았다면 이 메일을 무시하고, 필요하면 비밀번호를 변경하세요.',
    )
  except Exception as e:  # 상세 원인은 서버 콘솔에만 남긴다
    print(f'[메일 발송 실패] {type(e).__name__}: {e}', flush=True)
    return False, 'send_failed'
  with db() as conn:
    conn.execute(
        'INSERT OR REPLACE INTO email_codes VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        (purpose, username, email, _code_hash(code, salt), salt,
         now + CODE_TTL_SECONDS, now),
    )
  return True, ''


def verify_email_code(purpose, username, code):
  """(성공 여부, 메시지, 코드를 보냈던 이메일). 성공하면 코드는 즉시 삭제된다."""
  code = (code or '').strip()
  with db() as conn:
    row = conn.execute(
        'SELECT email, code_hash, salt, expires_at, attempts FROM email_codes'
        ' WHERE purpose = ? AND username = ?',
        (purpose, username),
    ).fetchone()
    if not row:
      return False, '인증 코드를 먼저 요청해 주세요.', ''
    email, code_hash, salt, expires_at, attempts = row
    if time.time() > expires_at:
      conn.execute('DELETE FROM email_codes WHERE purpose = ? AND username = ?', (purpose, username))
      return False, '인증 코드가 만료되었습니다. 처음부터 다시 진행해 주세요.', ''
    if attempts >= CODE_MAX_ATTEMPTS:
      conn.execute('DELETE FROM email_codes WHERE purpose = ? AND username = ?', (purpose, username))
      return False, '시도 횟수를 초과했습니다. 처음부터 다시 진행해 주세요.', ''
    if not hmac.compare_digest(_code_hash(code, salt), code_hash):
      conn.execute(
          'UPDATE email_codes SET attempts = attempts + 1 WHERE purpose = ? AND username = ?',
          (purpose, username),
      )
      return False, f'인증 코드가 올바르지 않습니다. (남은 시도 {CODE_MAX_ATTEMPTS - attempts - 1}회)', ''
    conn.execute('DELETE FROM email_codes WHERE purpose = ? AND username = ?', (purpose, username))
  return True, '', email


# ---------- 비밀번호 찾기 ----------
def request_password_reset(username, email):
  """계정 존재 여부를 노출하지 않도록 항상 같은 응답을 준다. (성공 여부, 메시지)."""
  if not smtp_configured():
    return False, '이메일 발송이 설정되어 있지 않습니다. 관리자에게 문의해 주세요.'
  now = time.time()
  if now - st.session_state.last_mail_at < CODE_RESEND_SECONDS:
    return False, '잠시 후 다시 시도해 주세요 (메일 재발송 대기 중).'
  email = email.strip().lower()
  with db() as conn:
    row = conn.execute(
        "SELECT 1 FROM users WHERE username = ? AND lower(email) = ? AND email != ''",
        (username, email),
    ).fetchone()
  if row:
    issue_email_code('reset', username, email, '[배틀그라운드 닉네임 관리] 비밀번호 재설정 코드', '비밀번호 재설정을 요청하셨습니다.')
  st.session_state.last_mail_at = now
  return True, '입력하신 정보가 일치하면 이메일로 코드를 보냈어요. (스팸함도 확인해 보세요)'


def finish_password_reset(username, code, new_pw):
  if len(new_pw) < 8:
    return False, '새 비밀번호는 8자 이상이어야 합니다.'
  ok, msg, _ = verify_email_code('reset', username, code)
  if not ok:
    return False, msg
  with db() as conn:
    conn.execute(
        'UPDATE users SET password = ? WHERE username = ?',
        (hash_password(new_pw), username),
    )
    conn.execute('DELETE FROM login_tokens WHERE username = ?', (username,))  # 모든 기기 로그아웃
  return True, '비밀번호가 변경되었습니다. 새 비밀번호로 로그인해 주세요.'


# ---------- 이메일 등록/인증 (기존 회원) · 보안 설정 ----------
def get_security(username):
  with db() as conn:
    row = conn.execute(
        'SELECT COALESCE(email, \'\'), COALESCE(email_verified, 0), COALESCE(twofa, 0),'
        ' COALESCE(login_alert, 0) FROM users WHERE username = ?',
        (username,),
    ).fetchone()
  keys = ('email', 'verified', 'twofa', 'alert')
  return dict(zip(keys, row)) if row else dict.fromkeys(keys, '')


def start_email_verify(username, email):
  email = email.strip().lower()
  if not smtp_configured():
    return False, '이메일 발송이 설정되어 있지 않습니다. 관리자에게 문의해 주세요.'
  if not EMAIL_RE.match(email) or len(email) > 100:
    return False, '올바른 이메일 주소를 입력해 주세요.'
  with db() as conn:
    if conn.execute(
        "SELECT 1 FROM users WHERE lower(email) = ? AND username != ? AND email != ''",
        (email, username),
    ).fetchone():
      return False, '이미 다른 계정에서 사용 중인 이메일입니다.'
  sent, reason = issue_email_code('email', username, email, '[배틀그라운드 닉네임 관리] 이메일 인증 코드', '이메일 등록을 요청하셨습니다.')
  if sent:
    return True, f'{mask_email(email)} 로 인증 코드를 보냈습니다.'
  if reason == 'cooldown':
    return False, '잠시 후 다시 시도해 주세요 (메일 재발송 대기 중).'
  return False, '메일 발송에 실패했습니다. 주소를 확인하거나 관리자에게 문의하세요.'


def finish_email_verify(username, code):
  ok, msg, email = verify_email_code('email', username, code)
  if not ok:
    return False, msg
  with db() as conn:
    if conn.execute(
        "SELECT 1 FROM users WHERE lower(email) = ? AND username != ? AND email != ''",
        (email, username),
    ).fetchone():
      return False, '이미 다른 계정에서 사용 중인 이메일입니다.'
    conn.execute(
        'UPDATE users SET email = ?, email_verified = 1 WHERE username = ?',
        (email, username),
    )
  return True, '✅ 이메일이 인증되었습니다.'


def set_security_flag(username, field, enabled):
  """2단계 인증(twofa) / 로그인 알림(login_alert) 켜고 끄기. (성공 여부, 메시지)."""
  if field not in ('twofa', 'login_alert'):
    return False, '알 수 없는 설정입니다.'
  if enabled:
    sec = get_security(username)
    if not (sec['email'] and sec['verified']):
      return False, '먼저 이메일을 등록하고 인증해 주세요.'
    if not smtp_configured():
      return False, '이메일 발송이 설정되어 있지 않아 켤 수 없습니다.'
  with db() as conn:
    conn.execute(f'UPDATE users SET {field} = ? WHERE username = ?', (int(enabled), username))
  return True, '설정을 저장했습니다.'


def _send_quiet(to_addr, subject, body, cfg):
  try:
    send_email(to_addr, subject, body, cfg=cfg)
  except Exception as e:
    print(f'[알림 메일 실패] {type(e).__name__}: {e}', flush=True)


def notify_login(username):
  """로그인 알림 메일 (설정한 회원만, 백그라운드 발송)."""
  sec = get_security(username)
  if not (sec['alert'] and sec['verified'] and sec['email'] and smtp_configured()):
    return
  try:
    ua = (st.context.headers.get('User-Agent', '') or '')[:120]
  except Exception:
    ua = ''
  body = (
      f'{now_str()} 에 계정 {username} 으로 새 로그인이 있었습니다.\n'
      f'접속 기기 정보: {ua or "알 수 없음"}\n\n'
      '본인이 아니라면 즉시 비밀번호를 변경하고 "모든 기기에서 로그아웃"을 누르세요.'
  )
  threading.Thread(
      target=_send_quiet,
      args=(sec['email'], '[배틀그라운드 닉네임 관리] 새 로그인 알림', body, smtp_config()),
      daemon=True,
  ).start()


# ---------- 최종관리자 최초 설정 ----------
def claim_super_admin(username, password, setup_code):
  """최종관리자가 없을 때 한 번만: 기존 계정을 지정하거나 새 계정을 만든다."""
  if super_exists():
    return False, '이미 최종관리자가 설정되어 있습니다.'
  locked = check_login_lock()
  if locked:
    return False, locked
  if not hmac.compare_digest(setup_code.strip(), get_setup_code()):
    note_auth_failure()
    return False, '설정 코드가 올바르지 않습니다.'
  with db() as conn:
    row = conn.execute(
        'SELECT password FROM users WHERE username = ?', (username,)
    ).fetchone()
    if row:
      if not verify_password(password, row[0]):
        note_auth_failure()
        return False, '기존 계정의 비밀번호가 올바르지 않습니다.'
      conn.execute(
          "UPDATE users SET role = 'super', is_admin = 1 WHERE username = ?",
          (username,),
      )
    else:
      if not USERNAME_RE.match(username):
        return False, '아이디는 영문/숫자/밑줄(_) 3~20자로 입력해 주세요.'
      if len(password) < 8:
        return False, '비밀번호는 8자 이상이어야 합니다.'
      conn.execute(
          'INSERT INTO users (username, password, api_key, model_name, is_admin,'
          ' role, email, email_verified, created_at)'
          ' VALUES (?, ?, ?, ?, 1, ?, ?, 0, ?)',
          (username, hash_password(password), '', FREE_OCR, ROLE_SUPER, '', now_str()),
      )
  if os.path.exists(SETUP_CODE_FILE):  # 더 이상 필요 없는 코드 파일 정리
    try:
      os.remove(SETUP_CODE_FILE)
    except OSError:
      pass
  return True, f'✅ {username} 계정이 최종관리자로 설정되었습니다. 로그인해 주세요.'


def change_password(username, old_pw, new_pw):
  if len(new_pw) < 8:
    return False, '새 비밀번호는 8자 이상이어야 합니다.'
  with db() as conn:
    row = conn.execute(
        'SELECT password FROM users WHERE username = ?', (username,)
    ).fetchone()
    if not row or not verify_password(old_pw, row[0]):
      return False, '현재 비밀번호가 올바르지 않습니다.'
    conn.execute(
        'UPDATE users SET password = ? WHERE username = ?',
        (hash_password(new_pw), username),
    )
    conn.execute('DELETE FROM login_tokens WHERE username = ?', (username,))
  queue_cookie('clear')
  return True, '비밀번호가 변경되었습니다.'


# ==========================================
# 4. 사용자 설정 (모델 / API 키)
# ==========================================
def get_user_model(username):
  with db() as conn:
    row = conn.execute(
        'SELECT model_name FROM users WHERE username = ?', (username,)
    ).fetchone()
  return row[0] if row and row[0] else FREE_OCR


def save_user_model(username, model_name):
  with db() as conn:
    conn.execute(
        'UPDATE users SET model_name = ? WHERE username = ?',
        (model_name, username),
    )


def get_saved_key(username, provider):
  with db() as conn:
    row = conn.execute(
        'SELECT api_key FROM user_keys WHERE username = ? AND provider = ?',
        (username, provider),
    ).fetchone()
  return decrypt_secret(row[0]) if row else ''


def save_key(username, provider, key):
  with db() as conn:
    conn.execute(
        'INSERT OR REPLACE INTO user_keys VALUES (?, ?, ?)',
        (username, provider, encrypt_secret(key)),
    )


def delete_key(username, provider):
  with db() as conn:
    conn.execute(
        'DELETE FROM user_keys WHERE username = ? AND provider = ?',
        (username, provider),
    )


def resolve_saved_model(saved):
  """저장된 model_name → (선택 목록 ID, 직접 입력 모델 ID)."""
  if saved and saved.startswith('openrouter:') and saved != CUSTOM_OPENROUTER:
    return CUSTOM_OPENROUTER, saved.split(':', 1)[1]
  if saved in MODEL_BY_ID:
    return saved, ''
  return FREE_OCR, ''  # 종료·삭제된 모델은 무료 OCR로 대체


# ==========================================
# 5. 이미지 / 닉네임 추출
# ==========================================
@st.cache_data(show_spinner=False, max_entries=8)
def load_image(data):
  """업로드 이미지를 RGB로 변환하고 긴 변을 제한한다 (속도·비용 절감)."""
  img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
  img = img.convert('RGB')  # RGBA/팔레트 PNG의 JPEG 저장 오류 방지
  img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
  return img


def image_to_b64(image):
  image = limit_size(image, MAX_SEND_SIDE)
  buf = io.BytesIO()
  image.save(buf, format='JPEG', quality=90)
  return base64.b64encode(buf.getvalue()).decode()


def dedupe_entries(entries):
  """닉네임 기준(대소문자 무시)으로 중복 제거."""
  seen, result = set(), []
  for e in entries:
    key = e['nickname'].lower()
    if e['nickname'] and key not in seen:
      seen.add(key)
      result.append(e)
  return result


def split_clan(text):
  """'[HOT6] Tag1013' → ('HOT6', 'Tag1013'). 태그가 없으면 ('', 원문)."""
  m = CLAN_RE.match(text or '')
  if m:
    return m.group(1).strip(), m.group(2).strip()
  return '', (text or '').strip()


def make_entry(text):
  """'[클랜] 닉네임' 문자열 → {'clan', 'nickname'}."""
  clan, nick = split_clan(text)
  return {
      'clan': clan.strip(' \t"\'`*[](){}'),
      'nickname': nick.strip(' \t"\'`*,;'),
  }


def entry_label(clan, nickname):
  return f'[{clan}] {nickname}' if clan else nickname


def limit_size(image, max_side):
  """긴 변이 max_side를 넘으면 줄인 복사본을 반환한다."""
  if max(image.size) <= max_side:
    return image
  copy = image.copy()
  copy.thumbnail((max_side, max_side), Image.LANCZOS)
  return copy


def crop_region(image, x_pct, y_pct):
  """이미지 크기 대비 %(x: 좌→우, y: 상→하) 범위를 잘라낸다."""
  w, h = image.size
  x0 = int(w * x_pct[0] / 100)
  y0 = int(h * y_pct[0] / 100)
  x1 = max(int(w * x_pct[1] / 100), x0 + 1)
  y1 = max(int(h * y_pct[1] / 100), y0 + 1)
  return image.crop((x0, y0, x1, y1))


def enhance_image(region, scale, contrast=False, sharpen=False):
  """업스케일(LANCZOS) + 선택적 대비/선명도 보정. scale 0 = 자동."""
  if scale == 0:
    factor = max(1.0, OCR_MIN_WIDTH / region.width)
  else:
    factor = float(scale)
  factor = min(factor, MAX_TARGET_SIDE / max(region.size))
  if factor > 1.01:
    region = region.resize(
        (round(region.width * factor), round(region.height * factor)),
        Image.LANCZOS,
    )
  if contrast:
    region = ImageOps.autocontrast(region, cutoff=1)
  if sharpen:
    region = region.filter(
        ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
    )
  return region


def draw_region_overlay(image, x_pct, y_pct):
  """선택 영역만 밝게, 나머지는 어둡게 표시한 미리보기."""
  preview = limit_size(image, 1280).copy()
  w, h = preview.size
  box = (
      int(w * x_pct[0] / 100),
      int(h * y_pct[0] / 100),
      max(int(w * x_pct[1] / 100), int(w * x_pct[0] / 100) + 1),
      max(int(h * y_pct[1] / 100), int(h * y_pct[0] / 100) + 1),
  )
  dimmed = ImageEnhance.Brightness(preview).enhance(0.35)
  dimmed.paste(preview.crop(box), box[:2])
  ImageDraw.Draw(dimmed).rectangle(box, outline=(255, 64, 64), width=3)
  return dimmed


def parse_entries(text):
  """AI 응답 텍스트에서 [{'clan', 'nickname'}] 목록을 추출한다."""
  text = re.sub(r'<think>.*?</think>', '', text or '', flags=re.S)
  lines = [ln for ln in text.splitlines() if ln.strip()]
  if len(lines) <= 1 and '|' not in text:  # 쉼표로 한 줄에 답한 경우
    lines = [p for p in re.split(r'[,;]+', text) if p.strip()]
  entries = []
  for line in lines:
    line = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s+', '', line)  # 목록 기호 제거
    if '|' in line:
      clan, nick = line.split('|', 1)
      entry = make_entry(nick)
      entry['clan'] = clan.strip(' \t"\'`*[](){}')
    else:
      entry = make_entry(line)
    if (
        entry['nickname']
        and len(entry['nickname']) <= MAX_NICK_LEN
        and len(entry['clan']) <= MAX_CLAN_LEN
    ):
      entries.append(entry)
  return dedupe_entries(entries)[:MAX_SQUAD]


@st.cache_resource(show_spinner='EasyOCR 모델을 불러오는 중입니다 (최초 1회)...')
def get_ocr_reader():
  try:
    import easyocr

    return easyocr.Reader(['en', 'ko'], gpu=False)
  except Exception:
    return None


def group_rows(results):
  """OCR 박스들을 같은 줄끼리 묶어 (왼→오) 한 줄 문자열로 만든다."""
  items = []
  for box, text, _ in results:
    ys = [pt[1] for pt in box]
    xs = [pt[0] for pt in box]
    items.append({
        'cy': sum(ys) / len(ys),
        'h': max(ys) - min(ys),
        'x': min(xs),
        'text': text,
    })
  items.sort(key=lambda i: i['cy'])
  rows = []
  for it in items:
    if rows and abs(it['cy'] - rows[-1][-1]['cy']) <= max(
        it['h'], rows[-1][-1]['h']
    ) * 0.6:
      rows[-1].append(it)
    else:
      rows.append([it])
  return [' '.join(i['text'] for i in sorted(r, key=lambda i: i['x'])) for r in rows]


def process_free_ocr(image):
  reader = get_ocr_reader()
  if reader is None:
    raise RuntimeError(
        '무료 OCR 모듈이 설치되지 않았습니다 (pip install easyocr).'
    )
  import numpy as np

  # 낮은 대비를 보정하고, 같은 줄의 글자(클랜 태그 + 닉네임)를 묶어서 해석한다
  enhanced = ImageOps.autocontrast(image.convert('L'), cutoff=1)
  results = reader.readtext(np.array(enhanced), detail=1)
  good = [
      (box, text.strip(), conf)
      for box, text, conf in results
      if conf >= 0.3 and not re.fullmatch(r'\d', text.strip())  # 슬롯 번호 제외
  ]

  entries = []
  for line in group_rows(good):
    clan, nick = split_clan(line)
    tokens = nick.split()
    if not clan and len(tokens) >= 2:  # 괄호를 못 읽은 경우: 앞 토큰을 클랜으로 간주
      clan, tokens = tokens[0], tokens[1:]
    nick = re.sub(r'[^\w\-.]', '', tokens[0]) if tokens else ''
    clan = re.sub(r'[^\w\-.]', '', clan)[:MAX_CLAN_LEN]
    if len(nick) >= 2 and not nick.isdigit():
      entries.append({'clan': clan, 'nickname': nick})
  return dedupe_entries(entries)[:MAX_SQUAD]


def ask_gemini(image, model, api_key):
  if genai is None:
    raise RuntimeError('google-genai 패키지가 필요합니다 (pip install google-genai).')
  client = genai.Client(api_key=api_key)
  response = client.models.generate_content(
      model=model, contents=[image, AI_PROMPT]
  )
  return response.text or ''


def ask_openai_compatible(image, model, api_key, base_url):
  if openai is None:
    raise RuntimeError('openai 패키지가 필요합니다 (pip install openai).')
  client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=60)
  response = client.chat.completions.create(
      model=model,
      messages=[{
          'role': 'user',
          'content': [
              {'type': 'text', 'text': AI_PROMPT},
              {
                  'type': 'image_url',
                  'image_url': {
                      'url': f'data:image/jpeg;base64,{image_to_b64(image)}'
                  },
              },
          ],
      }],
  )
  return response.choices[0].message.content or ''


def run_extraction(image, model_id, custom_model, api_key):
  """([{'clan', 'nickname'}], 안내 메시지)를 반환한다."""
  provider = MODEL_BY_ID[model_id][2]
  if provider == 'local':
    return process_free_ocr(image), ''
  if not api_key:
    return process_free_ocr(image), (
        '💡 API 키가 없어 [무료 기본 OCR 엔진]으로 자동 전환했습니다.'
    )

  model = custom_model if model_id == CUSTOM_OPENROUTER else model_id
  if not model:
    raise ValueError('OpenRouter 모델 ID를 입력해 주세요.')
  if provider == 'gemini':
    text = ask_gemini(image, model, api_key)
  else:
    text = ask_openai_compatible(
        image, model, api_key, PROVIDERS[provider]['base_url']
    )
  return parse_entries(text), ''


def friendly_error(exc):
  msg = str(exc)
  low = msg.lower()
  if '429' in msg or 'quota' in low or 'rate limit' in low:
    hint = '요청 한도를 초과했습니다. 잠시 후 다시 시도하거나 다른 모델을 선택해 보세요.'
  elif any(k in low for k in ('401', '403', 'api key', 'api_key', 'permission')):
    hint = 'API 키가 올바르지 않거나 권한이 없습니다. 키를 확인해 주세요.'
  elif '404' in msg or 'not found' in low or 'no longer available' in low:
    hint = '모델이 종료되었거나 이름이 바뀌었을 수 있습니다. 다른 모델을 선택해 주세요.'
  else:
    hint = '일시적인 오류일 수 있습니다. 다시 시도해 주세요.'
  return f'{hint}\n\n상세: {msg[:300]}'


# ==========================================
# 6. 닉네임 저장 / UI 헬퍼
# ==========================================
def save_nicknames(username, entries, source):
  """닉네임 기준 중복은 건너뛰되, 클랜이 바뀌었으면 갱신한다.

  (신규 저장 수, 클랜 갱신 수, 건너뛴 수)를 반환한다.
  """
  added = updated = skipped = 0
  with db() as conn:
    existing = {
        nick.lower(): (row_id, clan or '')
        for row_id, nick, clan in conn.execute(
            'SELECT id, nickname, clan FROM nicknames WHERE username = ?',
            (username,),
        )
    }
    stamp = now_str()
    for e in entries:
      key = e['nickname'].lower()
      if key in existing:
        row_id, old_clan = existing[key]
        if e['clan'] and e['clan'] != old_clan:
          conn.execute(
              'UPDATE nicknames SET clan = ? WHERE id = ?', (e['clan'], row_id)
          )
          existing[key] = (row_id, e['clan'])
          updated += 1
        else:
          skipped += 1
        continue
      cur = conn.execute(
          'INSERT INTO nicknames (username, nickname, clan, source_image,'
          ' created_at) VALUES (?, ?, ?, ?, ?)',
          (username, e['nickname'], e['clan'], source, stamp),
      )
      existing[key] = (cur.lastrowid, e['clan'])
      added += 1
    if len(entries) >= 2:  # 한 번에 저장한 2명 이상 = 한 판(스쿼드)
      sid = secrets.token_hex(6)
      conn.executemany(
          'INSERT INTO squad_members VALUES (?, ?, ?, ?, ?)',
          [(sid, username, e['nickname'], e['clan'], stamp) for e in entries],
      )
  return added, updated, skipped


def reset_region():
  st.session_state['crop_x'] = DEFAULT_CROP_X
  st.session_state['crop_y'] = DEFAULT_CROP_Y


def pick_region_by_drag(image):
  """streamlit-cropper로 드래그 선택 → ((x0, x1), (y0, y1)) %."""
  preview = limit_size(image, 700).copy()  # 표시 크기와 좌표 기준을 일치시킴
  box = st_cropper(
      preview,
      realtime_update=True,
      box_color='#FF4B4B',
      aspect_ratio=None,
      return_type='box',
  )
  w, h = preview.size
  clamp = lambda v: round(min(max(v, 0.0), 100.0), 1)
  return (
      (clamp(box['left'] / w * 100), clamp((box['left'] + box['width']) / w * 100)),
      (clamp(box['top'] / h * 100), clamp((box['top'] + box['height']) / h * 100)),
  )


def show_df(df, column_config=None):
  try:
    st.dataframe(df, width='stretch', hide_index=True, column_config=column_config)
  except Exception:
    st.dataframe(
        df, use_container_width=True, hide_index=True, column_config=column_config
    )


def show_editor(df, disabled, key, column_config=None):
  try:
    return st.data_editor(
        df, width='stretch', hide_index=True, disabled=disabled, key=key,
        column_config=column_config,
    )
  except Exception:
    return st.data_editor(
        df, use_container_width=True, hide_index=True, disabled=disabled,
        key=key, column_config=column_config,
    )


def stats_url(nickname, platform='steam'):
  """닉네임 → 전적 사이트 프로필 URL (한글·특수문자는 URL 인코딩)."""
  return STATS_URL_TEMPLATE.replace('{platform}', platform).replace(
      '{nickname}', quote(nickname, safe='')
  )


def current_platform():
  return STATS_PLATFORMS.get(st.session_state.get('stats_platform'), 'steam')


def md_escape(text):
  return re.sub(r'([\\\[\]*_`<>])', r'\\\1', text)


def link_column(label, display_text=None):
  try:
    return st.column_config.LinkColumn(label, display_text=display_text)
  except TypeError:  # 구버전 Streamlit
    return st.column_config.LinkColumn(label)


MOBILE_LIST_LIMIT = 100

RESPONSIVE_CSS = """
<style>
/* ---------- 공통 (PC) ---------- */
.block-container { max-width: 1400px; }
div[data-baseweb="tab-list"] { overflow-x: auto; scrollbar-width: thin; }
button[data-baseweb="tab"] { white-space: nowrap; }
/* 쿠키 전송용 보이지 않는 iframe은 공간을 차지하지 않게 */
div[data-testid="stElementContainer"]:has(iframe[height="0"]),
.element-container:has(iframe[height="0"]) {
  position: absolute; height: 0; margin: 0; padding: 0; overflow: hidden;
}

/* ---------- 모바일 (폭 640px 이하) ---------- */
@media (max-width: 640px) {
  .block-container { padding: 3.2rem 0.75rem 5rem 0.75rem !important; }
  h1 { font-size: 1.5rem !important; line-height: 1.3 !important; }
  h2 { font-size: 1.25rem !important; }
  h3 { font-size: 1.08rem !important; }
  /* iOS 입력창 확대 방지 + 터치하기 쉬운 크기 */
  input, textarea { font-size: 16px !important; }
  .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button,
  [data-testid="stLinkButton"] a, a[data-testid^="stBaseLinkButton"] {
    width: 100% !important; min-height: 2.9rem;
  }
  button[data-baseweb="tab"] { padding: 0.5rem 0.7rem; font-size: 0.92rem; }
  [data-testid="stMetricValue"] { font-size: 1.25rem !important; }
  [data-testid="stMetricLabel"] p { font-size: 0.72rem !important; }
  [data-testid="stMetricDelta"] { font-size: 0.7rem !important; }
  /* 숫자 요약은 한 줄(3칸)로 유지 */
  .st-key-metrics_row [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.4rem !important; }
  .st-key-metrics_row [data-testid="stColumn"], .st-key-metrics_row [data-testid="column"] {
    min-width: 0 !important; flex: 1 1 0 !important; width: auto !important;
  }
  .st-key-metrics_grid [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.4rem !important; }
  .st-key-metrics_grid [data-testid="stColumn"], .st-key-metrics_grid [data-testid="column"] {
    min-width: calc(33% - 0.4rem) !important; flex: 1 1 calc(33% - 0.4rem) !important; width: auto !important;
  }
}
</style>
"""


def inject_css():
  st.markdown(RESPONSIVE_CSS, unsafe_allow_html=True)


def is_mobile():
  """화면 모드 설정(자동/모바일/PC) 또는 접속 기기(User-Agent)로 모바일 여부 판단."""
  mode = st.session_state.get('view_mode', '자동')
  if mode == '모바일':
    return True
  if mode == 'PC':
    return False
  try:
    ua = st.context.headers.get('User-Agent', '')
  except Exception:  # 구버전 Streamlit
    return False
  return bool(re.search(r'Mobi|iPhone|iPod', ua or ''))


def keyed_container(key):
  """CSS로 꾸미기 위한 이름 붙은 컨테이너 (구버전은 일반 컨테이너)."""
  try:
    return st.container(key=key)
  except TypeError:
    return st.container()


def bordered_container():
  try:
    return st.container(border=True)
  except TypeError:
    return st.container()


def text_input_ac(label, autocomplete=None, **kwargs):
  """autocomplete 속성을 지원하면 적용 (브라우저/비밀번호 관리자의 저장·자동완성용)."""
  try:
    return st.text_input(label, autocomplete=autocomplete, **kwargs)
  except TypeError:  # 구버전 Streamlit
    return st.text_input(label, **kwargs)


def _short_time(text):
  return text[:16] if re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', text) else text


def nick_link_table(df, nick_col='nickname', label='닉네임 (클릭 → 전적)', mobile_cols=None):
  """닉네임을 전적 페이지 링크로 표시한다. PC는 표, 모바일은 카드형 목록.

  PC 표의 셀 값은 'URL#닉네임' 이고, 화면에는 '#' 뒤의 닉네임만 보인다.
  """
  platform = current_platform()

  if is_mobile():
    clan_cols = [c for c in ('clan', '클랜') if c in df.columns]
    if mobile_cols is None:
      mobile_cols = [c for c in df.columns if c != nick_col and c not in clan_cols][:3]
    extra_cols = [c for c in mobile_cols if c in df.columns]
    lines = []
    for _, row in df.head(MOBILE_LIST_LIMIT).iterrows():
      nick = str(row[nick_col])
      clan = next((str(row[c]).replace('`', '') for c in clan_cols if str(row[c]).strip()), '')
      head = f'**[{md_escape(nick)}]({stats_url(nick, platform)})**'
      if clan:
        head = f'`{clan}` ' + head
      extras = [
          _short_time(str(row[c])) for c in extra_cols
          if str(row[c]).strip() not in ('', 'nan', 'None')
      ]
      line = f'- {head}'
      if extras:
        line += '  \n  ' + md_escape(' · '.join(extras))
      lines.append(line)
    st.markdown('\n'.join(lines))
    if len(df) > MOBILE_LIST_LIMIT:
      st.caption(f'상위 {MOBILE_LIST_LIMIT}건만 표시했어요. 검색으로 좁혀 보세요.')
    return

  linked = df.copy()
  linked[nick_col] = [f'{stats_url(n, platform)}#{n}' for n in df[nick_col]]
  try:
    show_df(linked, {nick_col: link_column(label, r'#(.+)$')})
  except Exception:
    show_df(df)
    st.caption('이 Streamlit 버전은 링크 열을 지원하지 않아 일반 표로 표시합니다 (pip install -U streamlit).')


def show_image(img, caption):
  try:
    st.image(img, caption=caption, width='stretch')
  except Exception:
    st.image(img, caption=caption, use_container_width=True)


# ==========================================
# 6-0. 닉네임 검색 / 최근 목록 / 수정 (전체 회원 공용)
# ==========================================
SEARCH_LIMIT = 300


def time_ago(ts):
  try:
    dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
  except (TypeError, ValueError):
    return ''
  sec = (datetime.now() - dt).total_seconds()
  if sec < 60:
    return '방금 전'
  if sec < 3600:
    return f'{int(sec // 60)}분 전'
  if sec < 86400:
    return f'{int(sec // 3600)}시간 전'
  if sec < 86400 * 30:
    return f'{int(sec // 86400)}일 전'
  return ts[:10]


def search_nicknames(query, limit=SEARCH_LIMIT):
  """모든 회원이 올린 닉네임을 검색한다 (닉네임 기준으로 묶어서 최신 등록 1건 표시)."""
  where, params = '', []
  if query:
    q = query.lower()
    where = (
        ' WHERE lower(nickname) IN (SELECT lower(nickname) FROM nicknames'
        ' WHERE instr(lower(nickname), ?) > 0'
        " OR instr(lower(COALESCE(clan, '')), ?) > 0"
        ' OR instr(lower(username), ?) > 0)'
    )
    params = [q, q, q]
  sql = (
      "SELECT n.id, n.username, COALESCE(n.clan, '') AS clan, n.nickname,"
      ' n.created_at, g.first_at, g.cnt'
      ' FROM nicknames n JOIN ('
      '   SELECT MAX(id) AS mid, MIN(created_at) AS first_at,'
      '          COUNT(DISTINCT username) AS cnt'
      f'   FROM nicknames{where} GROUP BY lower(nickname)'
      ' ) g ON n.id = g.mid ORDER BY n.id DESC LIMIT ?'
  )
  with db() as conn:
    return pd.read_sql(sql, conn, params=params + [limit])


def load_recent_nicknames(limit=20):
  with db() as conn:
    return pd.read_sql(
        "SELECT id, username, COALESCE(clan, '') AS clan, nickname, created_at"
        ' FROM nicknames ORDER BY id DESC LIMIT ?',
        conn,
        params=(limit,),
    )


def recent_view(df):
  nmap = notes_map(st.session_state.get('username', ''))
  return pd.DataFrame({
      '닉네임': df['nickname'],
      '클랜': df['clan'],
      '등록 시각': df['created_at'],
      '경과': df['created_at'].map(time_ago),
      '등록자': df['username'],
      '내 메모': [note_badge(nmap, n) for n in df['nickname']],
  })


def search_view(df):
  nmap = notes_map(st.session_state.get('username', ''))
  return pd.DataFrame({
      '닉네임': df['nickname'],
      '클랜': df['clan'],
      '최근 등록': df['created_at'],
      '경과': df['created_at'].map(time_ago),
      '등록 회원 수': df['cnt'],
      '등록자': df['username'],
      '내 메모': [note_badge(nmap, n) for n in df['nickname']],
      '최초 등록': df['first_at'],
  })


def show_recent_nicknames(limit=20):
  st.subheader('🕒 최근 추가된 닉네임 (전체 회원)')
  df = load_recent_nicknames(limit)
  if df.empty:
    st.caption('아직 등록된 닉네임이 없습니다.')
  else:
    nick_link_table(recent_view(df), nick_col='닉네임', mobile_cols=['경과', '등록자', '내 메모'])


def update_nickname(actor, is_admin, row_id, clan, nick):
  """본인(또는 관리자)이 등록한 닉네임/클랜을 수정한다. (성공 여부, 메시지)."""
  clan = (clan or '').strip(' []()')
  nick = (nick or '').strip()
  if not nick:
    return False, '닉네임을 입력해 주세요.'
  if len(nick) > MAX_NICK_LEN:
    return False, f'닉네임은 {MAX_NICK_LEN}자 이하여야 합니다.'
  if len(clan) > MAX_CLAN_LEN:
    return False, f'클랜 태그는 {MAX_CLAN_LEN}자 이하여야 합니다.'
  with db() as conn:
    row = conn.execute(
        'SELECT username, nickname FROM nicknames WHERE id = ?', (row_id,)
    ).fetchone()
    if not row:
      return False, '대상 닉네임을 찾을 수 없습니다.'
    owner, old_nick = row[0], row[1]
    if owner != actor and not is_admin:
      return False, '본인이 등록한 닉네임만 수정할 수 있습니다.'
    if conn.execute(
        'SELECT 1 FROM nicknames WHERE username = ?'
        ' AND lower(nickname) = lower(?) AND id != ?',
        (owner, nick, row_id),
    ).fetchone():
      return False, '이미 등록된 닉네임입니다.'
    conn.execute(
        'UPDATE nicknames SET clan = ?, nickname = ? WHERE id = ?',
        (clan, nick, row_id),
    )
    propagate_rename(conn, owner, old_nick, nick, clan)
  if owner != actor:
    log_admin('닉네임 수정', owner, f'#{row_id} → {entry_label(clan, nick)}')
  return True, f'✅ {entry_label(clan, nick)} 로 수정했습니다.'


def stats_button(label, url):
  try:
    st.link_button(label, url)
  except AttributeError:  # 구버전 Streamlit
    st.markdown(f'[{label}]({url})')


# ==========================================
# 6-2. 함께한 사람 · 메모/즐겨찾기 · 글자 혼동 · 전적 API · 시간 통계
# ==========================================
PRESET_TAGS = ['👍 팀원 좋음', '⚠️ 주의', '🚫 핵 의심', '🏆 고수', '🤝 친구', '🔁 또 같이하고 싶음']
MAX_TAGS = 5
MAX_TAG_LEN = 20
MAX_NOTE_LEN = 300


def nick_key(nickname):
  return (nickname or '').strip().lower()


# ---------- 함께한 사람 (스쿼드 기록) ----------
def get_my_nickname(username):
  with db() as conn:
    row = conn.execute(
        "SELECT COALESCE(my_nickname, '') FROM users WHERE username = ?", (username,)
    ).fetchone()
  return row[0] if row else ''


def set_my_nickname(username, nickname):
  with db() as conn:
    conn.execute(
        'UPDATE users SET my_nickname = ? WHERE username = ?',
        ((nickname or '').strip()[:MAX_NICK_LEN], username),
    )


def top_partners(owner, exclude='', limit=30):
  """내 기록에서 가장 자주 등장한 닉네임."""
  sql = (
      "SELECT MAX(nickname) AS nickname, MAX(COALESCE(clan, '')) AS clan,"
      ' COUNT(DISTINCT squad_id) AS games, MAX(created_at) AS last_at'
      ' FROM squad_members WHERE owner = ?'
  )
  params = [owner]
  if exclude:
    sql += ' AND lower(nickname) != lower(?)'
    params.append(exclude)
  sql += ' GROUP BY lower(nickname) ORDER BY games DESC, last_at DESC LIMIT ?'
  params.append(limit)
  with db() as conn:
    return pd.read_sql(sql, conn, params=params)


def partners_of(owner, nickname, limit=30):
  """특정 닉네임과 같은 판에 있었던 사람들."""
  with db() as conn:
    return pd.read_sql(
        "SELECT MAX(m2.nickname) AS nickname, MAX(COALESCE(m2.clan, '')) AS clan,"
        ' COUNT(DISTINCT m2.squad_id) AS games, MAX(m2.created_at) AS last_at'
        ' FROM squad_members m1 JOIN squad_members m2'
        '   ON m1.squad_id = m2.squad_id AND lower(m2.nickname) != lower(m1.nickname)'
        ' WHERE m1.owner = ? AND lower(m1.nickname) = lower(?)'
        ' GROUP BY lower(m2.nickname) ORDER BY games DESC, last_at DESC LIMIT ?',
        conn,
        params=(owner, nickname, limit),
    )


def games_with(owner, nickname):
  with db() as conn:
    return conn.execute(
        'SELECT COUNT(DISTINCT squad_id) FROM squad_members'
        ' WHERE owner = ? AND lower(nickname) = lower(?)',
        (owner, nickname),
    ).fetchone()[0]


def recent_squads(owner, nickname='', limit=10):
  """최근 스쿼드 목록 [{'time', 'members': [(clan, nick)]}] (nickname이 있으면 그 사람이 있던 판만)."""
  where, params = 'WHERE owner = ?', [owner]
  if nickname:
    where += (
        ' AND squad_id IN (SELECT squad_id FROM squad_members'
        ' WHERE owner = ? AND lower(nickname) = lower(?))'
    )
    params += [owner, nickname]
  sql = (
      "SELECT squad_id, MAX(created_at) AS ts,"
      " GROUP_CONCAT(COALESCE(clan, '') || '|' || nickname, ';;')"
      f' FROM squad_members {where} GROUP BY squad_id ORDER BY ts DESC LIMIT ?'
  )
  with db() as conn:
    rows = conn.execute(sql, params + [limit]).fetchall()
  squads = []
  for _sid, ts, members in rows:
    pairs = []
    for part in (members or '').split(';;'):
      clan, _, nick = part.partition('|')
      if nick:
        pairs.append((clan, nick))
    squads.append({'time': ts, 'members': pairs})
  return squads


def squad_markdown(squads, platform):
  lines = []
  for sq in squads:
    names = ' · '.join(
        (f'`{c.replace(chr(96), "")}` ' if c else '')
        + f'[{md_escape(n)}]({stats_url(n, platform)})'
        for c, n in sq['members']
    )
    lines.append(f"- **{_short_time(sq['time'])}** — {names}")
  return '\n'.join(lines)


def propagate_rename(conn, owner, old_nick, new_nick, new_clan):
  """닉네임을 고쳤을 때 스쿼드 기록과 개인 메모의 이름도 함께 바꾼다."""
  conn.execute(
      'UPDATE squad_members SET nickname = ?, clan = ?'
      ' WHERE owner = ? AND lower(nickname) = lower(?)',
      (new_nick, new_clan, owner, old_nick),
  )
  old_key, new_key = nick_key(old_nick), nick_key(new_nick)
  if old_key != new_key and conn.execute(
      'SELECT 1 FROM nick_notes WHERE owner = ? AND nick_key = ?', (owner, new_key)
  ).fetchone() is None:
    conn.execute(
        'UPDATE nick_notes SET nick_key = ?, nickname = ? WHERE owner = ? AND nick_key = ?',
        (new_key, new_nick, owner, old_key),
    )


def cleanup_squads():
  """삭제된 닉네임이 남긴 스쿼드 기록 정리."""
  with db() as conn:
    conn.execute(
        'DELETE FROM squad_members WHERE NOT EXISTS ('
        ' SELECT 1 FROM nicknames n WHERE n.username = squad_members.owner'
        ' AND lower(n.nickname) = lower(squad_members.nickname))'
    )
    conn.execute(
        'DELETE FROM squad_members WHERE squad_id IN ('
        ' SELECT squad_id FROM squad_members GROUP BY squad_id HAVING COUNT(*) < 2)'
    )


# ---------- 개인 메모 · 태그 · 즐겨찾기 ----------
def clean_tags(tags):
  out = []
  for t in tags:
    t = (t or '').strip()
    if t and len(t) <= MAX_TAG_LEN and t not in out:
      out.append(t)
  return out[:MAX_TAGS]


def parse_tags(preset_selected, extra_text):
  extra = (extra_text or '').replace('，', ',').split(',')
  return clean_tags(list(preset_selected) + extra)


def get_note(owner, nickname):
  with db() as conn:
    row = conn.execute(
        'SELECT nickname, favorite, tags, note, updated_at FROM nick_notes'
        ' WHERE owner = ? AND nick_key = ?',
        (owner, nick_key(nickname)),
    ).fetchone()
  if not row:
    return None
  return {
      'nickname': row[0], 'favorite': bool(row[1]),
      'tags': [t for t in (row[2] or '').split(',') if t],
      'note': row[3] or '', 'updated_at': row[4],
  }


def save_note(owner, nickname, favorite, tags, note):
  nickname = (nickname or '').strip()
  if not nickname:
    return False, '닉네임을 입력해 주세요.'
  if len(nickname) > MAX_NICK_LEN:
    return False, f'닉네임은 {MAX_NICK_LEN}자 이하여야 합니다.'
  tags = clean_tags(tags)
  note = (note or '').strip()[:MAX_NOTE_LEN]
  with db() as conn:
    if not favorite and not tags and not note:
      conn.execute(
          'DELETE FROM nick_notes WHERE owner = ? AND nick_key = ?',
          (owner, nick_key(nickname)),
      )
      return True, '즐겨찾기·태그·메모가 모두 비어 있어 항목을 정리했어요.'
    conn.execute(
        'INSERT OR REPLACE INTO nick_notes VALUES (?, ?, ?, ?, ?, ?, ?)',
        (owner, nick_key(nickname), nickname, int(bool(favorite)),
         ','.join(tags), note, now_str()),
    )
  return True, f'✅ {nickname} 메모를 저장했습니다.'


def delete_note(owner, nickname):
  with db() as conn:
    conn.execute(
        'DELETE FROM nick_notes WHERE owner = ? AND nick_key = ?',
        (owner, nick_key(nickname)),
    )


def load_notes(owner):
  with db() as conn:
    return pd.read_sql(
        'SELECT nickname, favorite, tags, note, updated_at FROM nick_notes'
        ' WHERE owner = ? ORDER BY favorite DESC, updated_at DESC',
        conn,
        params=(owner,),
    )


def notes_map(owner):
  df = load_notes(owner)
  return {
      nick_key(r.nickname): {
          'favorite': bool(r.favorite),
          'tags': [t for t in (r.tags or '').split(',') if t],
          'note': r.note or '',
      }
      for r in df.itertuples()
  }


def note_badge(nmap, nickname):
  """표에 보여줄 한 줄 요약: ⭐ 태그 메모앞부분."""
  n = nmap.get(nick_key(nickname))
  if not n:
    return ''
  parts = ['⭐'] if n['favorite'] else []
  parts += n['tags'][:2]
  if n['note']:
    parts.append(n['note'][:18] + ('…' if len(n['note']) > 18 else ''))
  return ' '.join(parts)


# ---------- 글자 혼동 도우미 ----------
CONFUSABLE_CANON = {
    'l': 'l', 'I': 'l', '1': 'l', '|': 'l',
    'O': '0', 'o': '0', '0': '0',
    'S': '5', '5': '5', 'B': '8', '8': '8', 'Z': '2', '2': '2',
}
CONFUSABLE_ALTS = {
    'l': 'I1', 'I': 'l1', '1': 'lI', 'O': '0', '0': 'O', 'o': '0',
    'S': '5', '5': 'S', 'B': '8', '8': 'B', 'Z': '2', '2': 'Z',
}


def confusable_key(text):
  """헷갈리는 글자(l/I/1, O/0, S/5, B/8, Z/2)를 같은 것으로 취급한 비교 키."""
  return ''.join(CONFUSABLE_CANON.get(c, c.lower()) for c in text)


def confusable_variants(nickname, limit=20):
  """헷갈리는 글자를 하나씩 바꾼 후보 (원본 제외)."""
  out = []
  for i, ch in enumerate(nickname):
    for alt in CONFUSABLE_ALTS.get(ch, ''):
      cand = nickname[:i] + alt + nickname[i + 1:]
      if cand != nickname and cand not in out:
        out.append(cand)
        if len(out) >= limit:
          return out
  return out


def find_similar_in_db(nickname, limit=5):
  """철자만 헷갈리는(같은 길이, 같은 키) 이미 등록된 닉네임 [(닉네임, 클랜)]."""
  key = confusable_key(nickname)
  with db() as conn:
    rows = conn.execute(
        "SELECT DISTINCT nickname, COALESCE(clan, '') FROM nicknames WHERE length(nickname) = ?",
        (len(nickname),),
    ).fetchall()
  found = {}
  for n, c in rows:
    if n != nickname and confusable_key(n) == key and (n not in found or (c and not found[n])):
      found[n] = c  # 같은 닉네임은 한 번만 (클랜 정보가 있는 쪽 우선)
  return list(found.items())[:limit]


# ---------- PUBG 공식 API 전적 요약 (키 필요) ----------
PUBG_API_BASE = 'https://api.pubg.com'
PUBG_MODES = [
    ('squad-fpp', '스쿼드 1인칭'), ('squad', '스쿼드 3인칭'),
    ('duo-fpp', '듀오 1인칭'), ('duo', '듀오 3인칭'),
    ('solo-fpp', '솔로 1인칭'), ('solo', '솔로 3인칭'),
]


def pubg_api_key():
  return get_secret('PUBG_API_KEY') or decrypt_secret(get_setting('pubg_api_key', ''))


def pubg_request(path, api_key):
  req = urllib.request.Request(
      PUBG_API_BASE + path,
      headers={'Authorization': f'Bearer {api_key}', 'Accept': 'application/vnd.api+json'},
  )
  with urllib.request.urlopen(req, timeout=10) as resp:
    return json.loads(resp.read().decode('utf-8'))


@st.cache_data(ttl=600, show_spinner=False)
def fetch_pubg_summary(nickname, shard, _api_key):
  """모드별 평생 전적 요약 DataFrame. (10분 캐시: API 호출 한도 절약)"""
  players = pubg_request(
      f'/shards/{shard}/players?filter[playerNames]={quote(nickname, safe="")}', _api_key
  )
  items = players.get('data') or []
  if not items:
    raise LookupError('플레이어를 찾을 수 없습니다.')
  life = pubg_request(f'/shards/{shard}/players/{items[0]["id"]}/seasons/lifetime', _api_key)
  modes = life['data']['attributes']['gameModeStats']
  rows = []
  for key, label in PUBG_MODES:
    m = modes.get(key) or {}
    rounds = m.get('roundsPlayed', 0)
    if not rounds:
      continue
    wins, kills = m.get('wins', 0), m.get('kills', 0)
    rows.append({
        '모드': label,
        '판수': rounds,
        '승리': wins,
        'TOP10': m.get('top10s', 0),
        '킬': kills,
        'K/D(추정)': round(kills / max(1, rounds - wins), 2),
        '평균 딜량': round(m.get('damageDealt', 0) / rounds),
        '헤드샷 %': round(m.get('headshotKills', 0) / kills * 100, 1) if kills else 0.0,
    })
  return pd.DataFrame(rows)


def pubg_friendly_error(exc):
  if isinstance(exc, LookupError):
    return str(exc) + ' (닉네임 대소문자와 플랫폼을 확인하세요)'
  if isinstance(exc, urllib.error.HTTPError):
    return {
        401: 'PUBG API 키가 올바르지 않습니다.',
        404: '플레이어를 찾을 수 없습니다. (닉네임 대소문자와 플랫폼을 확인하세요)',
        429: 'API 호출 한도를 초과했어요. 1분 뒤 다시 시도하세요.',
    }.get(exc.code, f'PUBG API 오류 ({exc.code})')
  return f'전적을 불러오지 못했습니다: {type(exc).__name__}'


# ---------- 시간대별 · 요일별 통계 ----------
def load_time_stats():
  with db() as conn:
    hours = dict(conn.execute(
        "SELECT CAST(strftime('%H', created_at) AS INTEGER), COUNT(*) FROM nicknames"
        ' WHERE created_at IS NOT NULL GROUP BY 1'
    ).fetchall())
    days = dict(conn.execute(
        "SELECT CAST(strftime('%w', created_at) AS INTEGER), COUNT(*) FROM nicknames"
        ' WHERE created_at IS NOT NULL GROUP BY 1'
    ).fetchall())
  hdf = pd.DataFrame(
      {'등록 수': [hours.get(h, 0) for h in range(24)]},
      index=[f'{h:02d}시' for h in range(24)],
  )
  names = ['일', '월', '화', '수', '목', '금', '토']  # strftime %w: 0 = 일요일
  order = [1, 2, 3, 4, 5, 6, 0]
  ddf = pd.DataFrame(
      {'등록 수': [days.get(d, 0) for d in order]},
      index=[names[d] + '요일' for d in order],
  )
  return hdf, ddf


# ---------- 화면용 보조 함수 ----------
def prepare_target(img, use_crop, crop_x, crop_y, scale_label, contrast, sharpen):
  """인식에 쓸 이미지(영역 자르기 + 업스케일/보정)를 만든다."""
  region = crop_region(img, crop_x, crop_y) if use_crop else limit_size(img, MAX_FULL_SIDE)
  return enhance_image(region, UPSCALE_OPTIONS[scale_label], contrast, sharpen)


def validate_entries(entries):
  if len(entries) > MAX_SQUAD:
    return f'한 번에 최대 {MAX_SQUAD}명까지만 저장할 수 있습니다.'
  if any(len(e['nickname']) > MAX_NICK_LEN for e in entries):
    return f'닉네임은 {MAX_NICK_LEN}자 이하여야 합니다.'
  if any(len(e['clan']) > MAX_CLAN_LEN for e in entries):
    return f'클랜 태그는 {MAX_CLAN_LEN}자 이하여야 합니다.'
  return ''


def apply_nick_choice(pc_key, clan_key, m_key, mobile, nick, clan):
  """'이걸로 바꾸기' 버튼: 입력칸의 값을 DB에 있는 철자로 교체 (버튼 콜백)."""
  if mobile:
    st.session_state[m_key] = entry_label(clan, nick)
  else:
    st.session_state[pc_key] = nick
    if clan:
      st.session_state[clan_key] = clan


def nickname_tools(nickname, key_prefix):
  """닉네임 하나에 대한 보조 도구: 비슷한 DB 닉네임 · 헷갈리는 글자 후보 · 전적 요약(API)."""
  nickname = (nickname or '').strip()
  if not nickname:
    return
  plat = current_platform()
  similar = find_similar_in_db(nickname)
  if similar:
    st.info('DB에 철자만 다른 닉네임이 있어요: ' + ', '.join(f'`{n}`' for n, _ in similar))
  variants = confusable_variants(nickname)
  if variants:
    with st.expander('🔤 헷갈리는 글자 후보로 전적 확인'):
      st.caption(
          'l/I/1, O/0, S/5, B/8, Z/2 처럼 비슷한 글자를 바꾼 후보예요.'
          ' 눌러서 전적 페이지가 열리면 그 철자가 맞는 거예요.'
      )
      st.markdown(' · '.join(f'[{md_escape(v)}]({stats_url(v, plat)})' for v in variants))
  api_key = pubg_api_key()
  if api_key:
    sum_key = f'pubg_sum_{key_prefix}_{plat}_{nickname}'
    if st.button('📊 전적 요약 불러오기', key=f'pubg_btn_{key_prefix}'):
      try:
        st.session_state[sum_key] = fetch_pubg_summary(nickname, plat, api_key)
      except Exception as e:
        st.session_state[sum_key] = pubg_friendly_error(e)
    result = st.session_state.get(sum_key)
    if isinstance(result, pd.DataFrame):
      if result.empty:
        st.caption('모드별 기록이 없어요.')
      else:
        show_df(result)
        st.caption('K/D는 (킬 ÷ 승리하지 못한 판 수)로 추정한 값이에요. 출처: PUBG 공식 API (평생 기록).')
    elif isinstance(result, str):
      st.error(result)


# ==========================================
# 6-1. 관리자 기능 (설정 / 로그 / 회원·닉네임 관리 / 백업)
# ==========================================
def require_admin():
  if not st.session_state.get('is_admin'):
    raise PermissionError('관리자 권한이 필요합니다.')


def require_super():
  if st.session_state.get('role') != ROLE_SUPER:
    raise PermissionError('최종관리자 권한이 필요합니다.')


def get_setting(key, default=''):
  with db() as conn:
    row = conn.execute(
        'SELECT value FROM app_settings WHERE key = ?', (key,)
    ).fetchone()
  return row[0] if row else default


def set_setting(key, value):
  require_super()
  with db() as conn:
    conn.execute(
        'INSERT OR REPLACE INTO app_settings VALUES (?, ?)', (key, value)
    )


def log_admin(action, target='', detail=''):
  with db() as conn:
    conn.execute(
        'INSERT INTO admin_log (admin, action, target, detail, created_at)'
        ' VALUES (?, ?, ?, ?, ?)',
        (st.session_state.get('username', ''), action, target, detail, now_str()),
    )


def _check_manageable(conn, target):
  """대상이 내 권한보다 낮은 타인인지 확인. (대상 권한, 오류 메시지)."""
  if target == st.session_state.username:
    return None, '자신의 계정은 여기서 작업할 수 없습니다.'
  row = conn.execute(
      'SELECT role, is_admin FROM users WHERE username = ?', (target,)
  ).fetchone()
  if not row:
    return None, '대상 회원을 찾을 수 없습니다.'
  role = normalize_role(row[0], row[1])
  if ROLE_RANK[role] >= ROLE_RANK[st.session_state.role]:
    return None, '같거나 높은 권한의 계정은 관리할 수 없습니다.'
  return role, ''


def admin_reset_password(target):
  """임시 비밀번호를 발급한다. (성공 여부, 메시지)."""
  require_admin()
  temp = secrets.token_urlsafe(9)
  with db() as conn:
    _, err = _check_manageable(conn, target)
    if err:
      return False, err
    conn.execute(
        'UPDATE users SET password = ? WHERE username = ?',
        (hash_password(temp), target),
    )
    conn.execute('DELETE FROM login_tokens WHERE username = ?', (target,))
  log_admin('비밀번호 초기화', target)
  return True, f'🔑 **{target}** 님의 임시 비밀번호: `{temp}` (이 알림을 닫으면 다시 볼 수 없습니다)'


def admin_disable_twofa(target):
  """이메일을 잃어버린 회원이 잠기지 않도록 2단계 인증을 해제한다."""
  require_admin()
  with db() as conn:
    _, err = _check_manageable(conn, target)
    if err:
      return False, err
    conn.execute('UPDATE users SET twofa = 0 WHERE username = ?', (target,))
    conn.execute('DELETE FROM email_codes WHERE username = ?', (target,))
  log_admin('2단계 인증 해제', target)
  return True, f'{target} 님의 2단계 인증을 해제했습니다.'


def admin_clear_keys(target):
  require_admin()
  with db() as conn:
    _, err = _check_manageable(conn, target)
    if err:
      return False, err
    count = conn.execute(
        'DELETE FROM user_keys WHERE username = ?', (target,)
    ).rowcount
  log_admin('API 키 삭제', target, f'{count}개')
  return True, f'{target} 님의 API 키 {count}개를 삭제했습니다.'


def admin_delete_user(target, delete_nicks):
  require_admin()
  with db() as conn:
    _, err = _check_manageable(conn, target)
    if err:
      return False, err
    conn.execute('DELETE FROM user_keys WHERE username = ?', (target,))
    conn.execute('DELETE FROM pending_signups WHERE username = ?', (target,))
    conn.execute('DELETE FROM login_tokens WHERE username = ?', (target,))
    conn.execute('DELETE FROM email_codes WHERE username = ?', (target,))
    deleted = conn.execute(
        'DELETE FROM users WHERE username = ?', (target,)
    ).rowcount
    conn.execute('DELETE FROM nick_notes WHERE owner = ?', (target,))
    removed = 0
    if delete_nicks:
      removed = conn.execute(
          'DELETE FROM nicknames WHERE username = ?', (target,)
      ).rowcount
      conn.execute('DELETE FROM squad_members WHERE owner = ?', (target,))
  if not deleted:
    return False, '계정을 삭제하지 못했습니다.'
  log_admin('계정 삭제', target, f'닉네임 {removed}개 함께 삭제' if delete_nicks else '닉네임 유지')
  return True, f'{target} 계정을 삭제했습니다.'


def admin_set_role(target, new_role):
  """최종관리자만: 일반관리자 ↔ 일반회원 변경."""
  require_super()
  if new_role not in (ROLE_ADMIN, ROLE_USER):
    return False, '변경할 수 없는 권한입니다.'
  with db() as conn:
    role, err = _check_manageable(conn, target)
    if err:
      return False, err
    if role == new_role:
      return False, f'이미 {ROLE_LABELS[new_role]} 입니다.'
    conn.execute(
        'UPDATE users SET role = ?, is_admin = ? WHERE username = ?',
        (new_role, int(new_role != ROLE_USER), target),
    )
  log_admin('권한 변경', target, f'{ROLE_LABELS[role]} → {ROLE_LABELS[new_role]}')
  return True, f'{target} 님을 {ROLE_LABELS[new_role]}(으)로 변경했습니다.'


def admin_transfer_super(target, password):
  """최종관리자 권한 위임 (본인만 가능). 위임 후 본인은 일반관리자가 된다."""
  require_super()
  actor = st.session_state.username
  if target == actor:
    return False, '자기 자신에게는 위임할 수 없습니다.'
  with db() as conn:
    mine = conn.execute(
        'SELECT password FROM users WHERE username = ?', (actor,)
    ).fetchone()
    if not mine or not verify_password(password, mine[0]):
      return False, '비밀번호가 올바르지 않습니다.'
    if not conn.execute(
        'SELECT 1 FROM users WHERE username = ?', (target,)
    ).fetchone():
      return False, '대상 회원을 찾을 수 없습니다.'
    conn.execute(
        "UPDATE users SET role = 'super', is_admin = 1 WHERE username = ?",
        (target,),
    )
    conn.execute(
        "UPDATE users SET role = 'admin', is_admin = 1 WHERE username = ?",
        (actor,),
    )
  st.session_state.role = ROLE_ADMIN
  log_admin('최종관리자 위임', target, f'{actor} → {target}')
  return True, f'✅ {target} 님에게 최종관리자 권한을 위임했습니다. 이제 일반관리자입니다.'


def change_own_role_to_user():
  """본인 권한은 본인만 낮출 수 있다 (일반관리자 → 일반회원)."""
  require_admin()
  if st.session_state.role == ROLE_SUPER:
    return False, '최종관리자는 먼저 다른 회원에게 권한을 위임해야 합니다.'
  username = st.session_state.username
  with db() as conn:
    conn.execute(
        "UPDATE users SET role = 'user', is_admin = 0 WHERE username = ?",
        (username,),
    )
  log_admin('본인 권한 변경', username, f'{ROLE_LABELS[ROLE_ADMIN]} → {ROLE_LABELS[ROLE_USER]}')
  st.session_state.role = ROLE_USER
  st.session_state.is_admin = 0
  return True, '일반회원으로 변경했습니다.'


def admin_update_nicknames(rows):
  """rows: [(id, clan, nickname)] → (수정 수, 건너뛴 수)."""
  require_admin()
  updated = skipped = 0
  with db() as conn:
    for row_id, clan, nick in rows:
      clan = (clan or '').strip(' []()')
      nick = (nick or '').strip()
      owner = conn.execute(
          'SELECT username, nickname FROM nicknames WHERE id = ?', (row_id,)
      ).fetchone()
      if (
          not owner
          or not nick
          or len(nick) > MAX_NICK_LEN
          or len(clan) > MAX_CLAN_LEN
          or conn.execute(
              'SELECT 1 FROM nicknames WHERE username = ?'
              ' AND lower(nickname) = lower(?) AND id != ?',
              (owner[0], nick, row_id),
          ).fetchone()
      ):
        skipped += 1
        continue
      conn.execute(
          'UPDATE nicknames SET clan = ?, nickname = ? WHERE id = ?',
          (clan, nick, row_id),
      )
      propagate_rename(conn, owner[0], owner[1], nick, clan)
      updated += 1
  if updated:
    log_admin('닉네임 수정', '', f'{updated}건')
  return updated, skipped


def admin_delete_nicknames(ids):
  require_admin()
  with db() as conn:
    conn.executemany('DELETE FROM nicknames WHERE id = ?', [(i,) for i in ids])
  cleanup_squads()
  log_admin('닉네임 삭제', '', f'{len(ids)}건 (id: {", ".join(map(str, ids[:20]))})')


def load_stats():
  """대시보드용 통계."""
  today = datetime.now().date()
  days = [(today - pd.Timedelta(days=i)).strftime('%Y-%m-%d') for i in range(13, -1, -1)]
  with db() as conn:
    n_users, n_admins = conn.execute(
        'SELECT COUNT(*), COALESCE(SUM(is_admin), 0) FROM users'
    ).fetchone()
    n_nicks, n_unique = conn.execute(
        'SELECT COUNT(*), COUNT(DISTINCT lower(nickname)) FROM nicknames'
    ).fetchone()
    n_today = conn.execute(
        'SELECT COUNT(*) FROM nicknames WHERE substr(created_at, 1, 10) = ?',
        (days[-1],),
    ).fetchone()[0]
    daily = pd.read_sql(
        'SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS cnt FROM nicknames'
        ' WHERE substr(created_at, 1, 10) >= ? GROUP BY day',
        conn,
        params=(days[0],),
    )
    by_user = pd.read_sql(
        'SELECT username, COUNT(*) AS cnt FROM nicknames'
        ' GROUP BY username ORDER BY cnt DESC LIMIT 10',
        conn,
    )
    by_clan = pd.read_sql(
        "SELECT clan, COUNT(*) AS cnt FROM nicknames WHERE COALESCE(clan, '') != ''"
        ' GROUP BY clan ORDER BY cnt DESC LIMIT 10',
        conn,
    )
    recent = pd.read_sql(
        "SELECT created_at, username, COALESCE(clan, '') AS clan, nickname"
        ' FROM nicknames ORDER BY id DESC LIMIT 10',
        conn,
    )
  series = daily.set_index('day')['cnt'].reindex(days, fill_value=0)
  return {
      'users': n_users,
      'admins': int(n_admins),
      'nicks': n_nicks,
      'unique': n_unique,
      'today': n_today,
      'daily': series.to_frame('수집 수'),
      'by_user': by_user.rename(columns={'username': '수집자', 'cnt': '수집 수'}),
      'by_clan': by_clan.rename(columns={'clan': '클랜', 'cnt': '수집 수'}),
      'recent': recent.rename(columns={
          'created_at': '수집 시각', 'username': '수집자',
          'clan': '클랜', 'nickname': '닉네임',
      }),
  }


def load_user_overview():
  with db() as conn:
    df = pd.read_sql(
        'SELECT u.username, u.role, u.is_admin, COALESCE(u.email, \'\') AS email,'
        ' COALESCE(u.email_verified, 0) AS email_verified, COALESCE(u.twofa, 0) AS twofa, u.created_at,'
        ' (SELECT COUNT(*) FROM nicknames n WHERE n.username = u.username) AS nick_count,'
        ' (SELECT MAX(created_at) FROM nicknames n WHERE n.username = u.username) AS last_collected,'
        ' (SELECT COUNT(*) FROM user_keys k WHERE k.username = u.username) AS key_count'
        ' FROM users u ORDER BY u.created_at',
        conn,
    )
  df['role'] = [normalize_role(r, a) for r, a in zip(df['role'], df['is_admin'])]
  return df


def make_db_backup():
  """SQLite 백업 API로 일관된 스냅샷을 만들어 bytes로 반환한다."""
  require_admin()
  return snapshot_db_bytes()


SQLITE_MAGIC = b'SQLite format 3\x00'
MAX_RESTORE_MB = 100


def validate_backup(data):
  """업로드된 백업 파일 검사. (정상 여부, 오류 메시지, 통계)."""
  if len(data) > MAX_RESTORE_MB * 1024 * 1024:
    return False, f'파일이 너무 큽니다 (최대 {MAX_RESTORE_MB}MB).', {}
  if not data.startswith(SQLITE_MAGIC):
    return False, '이 앱의 백업(.db) 파일이 아닙니다.', {}
  with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, 'upload.db')
    with open(path, 'wb') as f:
      f.write(data)
    conn = sqlite3.connect(path)
    try:
      if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        return False, '백업 파일이 손상되어 있습니다.', {}
      tables = {
          r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
      }
      if not {'users', 'nicknames'} <= tables:
        return False, '이 앱의 백업 파일이 아닙니다 (필요한 테이블이 없어요).', {}
      stats = {
          'users': conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
          'nicknames': conn.execute('SELECT COUNT(*) FROM nicknames').fetchone()[0],
      }
    except sqlite3.DatabaseError:
      return False, '백업 파일을 읽을 수 없습니다.', {}
    finally:
      conn.close()
  return True, '', stats


def restore_database(data):
  """백업 파일 내용으로 현재 DB를 통째로 교체한다. (성공 여부, 메시지)."""
  ok, msg, stats = validate_backup(data)
  if not ok:
    return False, msg
  with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, 'restore.db')
    with open(path, 'wb') as f:
      f.write(data)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(DB_FILE)
    try:
      src.backup(dst)
    finally:
      dst.close()
      src.close()
  init_db()  # 구버전 백업이면 새 컬럼/테이블을 보완
  return True, f'회원 {stats["users"]}명, 닉네임 {stats["nicknames"]}개를 복원했습니다.'


def env_status():
  """설치/설정 상태 점검표."""
  def has(module):
    try:
      return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
      return False

  yes, no = '✅ 사용 가능', '❌ 미설치'
  return pd.DataFrame([
      ('Python', sys.version.split()[0]),
      ('Streamlit', st.__version__),
      ('google-genai (Gemini)', yes if has('google.genai') else no),
      ('openai (OpenAI / Groq / OpenRouter)', yes if has('openai') else no),
      ('easyocr (무료 OCR)', yes if has('easyocr') else no),
      ('cryptography (API 키 암호화)', yes if has('cryptography') else '⚠️ 미설치 (평문 저장)'),
      ('streamlit-cropper (드래그 영역 선택)', yes if has('streamlit_cropper') else '선택 사항 (미설치)'),
  ], columns=['항목', '상태'])


sync_bootstrap()  # 재시작 직후라면 원격 스냅샷에서 자동 복원


# ==========================================
# 7. 로그인 화면
# ==========================================
# 저장된 로그인 토큰이 있으면 자동 로그인 (세션 시작 시 1회)
if not st.session_state.logged_in and not st.session_state.get('cookie_checked'):
  st.session_state.cookie_checked = True
  if login_with_cookie():
    st.rerun()
flush_cookie_cmd()
inject_css()

if not st.session_state.logged_in:
  st.title('🎮 배틀그라운드 닉네임 관리 시스템')
  # PC에서는 로그인 폼이 너무 넓지 않게 폭 제한
  st.markdown('<style>.block-container{max-width:680px !important;}</style>', unsafe_allow_html=True)
  show_flash()
  needs_setup = not super_exists()
  login_tabs = st.tabs(
      ['로그인', '회원가입'] + (['🛠️ 최종관리자 설정'] if needs_setup else [])
  )
  tab_login, tab_register = login_tabs[0], login_tabs[1]

  with tab_login:
    st.subheader('로그인')
    st.caption('관리 기능은 권한이 있는 계정으로 로그인했을 때만 표시됩니다.')
    twofa_pend = st.session_state.get('twofa_pending')
    if twofa_pend:
      st.info('📧 가입한 이메일로 보낸 6자리 인증 코드를 입력해 주세요.')
      with st.form('twofa_form'):
        twofa_code = st.text_input('인증 코드 (6자리)', max_chars=6)
        twofa_submit = st.form_submit_button('확인')
      if twofa_submit:
        ok, msg = finish_twofa(twofa_code)
        if ok:
          st.rerun()
        else:
          st.error(msg)
      if st.button('↩︎ 처음부터 다시', key='twofa_cancel'):
        st.session_state.twofa_pending = None
        st.rerun()
    else:
      with st.form('login_form'):
        u_input = text_input_ac('아이디', 'username')
        p_input = text_input_ac('비밀번호', 'current-password', type='password')
        remember = (
            st.checkbox(
                f'로그인 상태 유지 (이 기기에서 최대 {REMEMBER_MAX_DAYS}일)',
                value=False,
                help=f'새로고침하거나 브라우저를 껐다 켜도 로그인이 유지돼요. '
                f'{REMEMBER_IDLE_DAYS}일 동안 접속하지 않거나 {REMEMBER_MAX_DAYS}일이 지나면 '
                '다시 로그인해야 해요. 공용 PC에서는 체크하지 마세요.',
            )
            if cookies_supported()
            else False
        )
        if st.form_submit_button('로그인'):
          ok, msg = login_user(u_input.strip(), p_input, remember)
          if ok or msg == TWOFA_PENDING:
            st.rerun()
          else:
            st.error(msg)

      with st.expander('🔑 비밀번호를 잊으셨나요?'):
        reset_user = st.session_state.get('reset_pending', '')
        if reset_user:
          st.info('📧 이메일로 보낸 6자리 코드와 새 비밀번호를 입력해 주세요.')
          with st.form('reset_form'):
            rs_code = st.text_input('인증 코드 (6자리)', max_chars=6)
            rs_pw1 = st.text_input('새 비밀번호 (8자 이상)', type='password')
            rs_pw2 = st.text_input('새 비밀번호 확인', type='password')
            rs_submit = st.form_submit_button('비밀번호 변경')
          if rs_submit:
            if rs_pw1 != rs_pw2:
              st.error('새 비밀번호가 일치하지 않습니다.')
            else:
              ok, msg = finish_password_reset(reset_user, rs_code, rs_pw1)
              if ok:
                st.session_state.reset_pending = ''
                flash(msg)
                st.rerun()
              else:
                st.error(msg)
          if st.button('↩︎ 처음부터 다시', key='reset_cancel'):
            st.session_state.reset_pending = ''
            st.rerun()
        else:
          st.caption('가입할 때(또는 보안 설정에서) 등록한 이메일로 인증 코드를 보내드려요.')
          with st.form('reset_request_form'):
            rq_user = st.text_input('아이디')
            rq_email = st.text_input('가입한 이메일')
            if st.form_submit_button('인증 코드 받기'):
              ok, msg = request_password_reset(rq_user.strip(), rq_email)
              if ok:
                st.session_state.reset_pending = rq_user.strip()
                st.rerun()
              else:
                st.error(msg)

  with tab_register:
    st.subheader('신규 회원가입')
    if get_setting('allow_signup', '1') != '1':
      st.info('현재 신규 회원가입이 중지되어 있습니다.')
    verify_on = email_verification_on()
    pending_user = st.session_state.get('signup_pending', '')
    if pending_user and not pending_email(pending_user):
      pending_user = ''
      st.session_state.signup_pending = ''

    if verify_on and pending_user:
      st.info(
          f'📧 {mask_email(pending_email(pending_user))} 로 보낸 6자리 인증 코드를'
          f' {CODE_TTL_SECONDS // 60}분 안에 입력해 주세요.'
      )
      with st.form('verify_form'):
        code_in = st.text_input('인증 코드 (6자리)', max_chars=6)
        verify_clicked = st.form_submit_button('인증하고 가입 완료')
      if verify_clicked:
        ok, msg = finish_signup(pending_user, code_in)
        if ok:
          st.session_state.signup_pending = ''
          flash(msg)
          st.rerun()
        else:
          st.error(msg)
      rc1, rc2 = st.columns(2)
      if rc1.button('📨 코드 다시 보내기'):
        ok, msg = resend_code(pending_user)
        (st.success if ok else st.error)(msg)
      if rc2.button('↩︎ 처음부터 다시'):
        cancel_signup(pending_user)
        st.session_state.signup_pending = ''
        st.rerun()
    else:
      with st.form('register_form'):
        nu_input = text_input_ac('사용할 아이디 (영문/숫자/_ 3~20자)', 'username')
        ne_input = st.text_input(
            '이메일 (인증 코드가 발송됩니다)' if verify_on else '이메일 (선택)'
        )
        np_input = text_input_ac('사용할 비밀번호 (8자 이상)', 'new-password', type='password')
        np_confirm = text_input_ac('비밀번호 확인', 'new-password', type='password')
        if st.form_submit_button('인증 코드 받기' if verify_on else '회원가입'):
          if not nu_input or not np_input or (verify_on and not ne_input):
            st.warning('필수 항목을 모두 입력해주세요.')
          elif np_input != np_confirm:
            st.error('비밀번호가 일치하지 않습니다.')
          elif verify_on:
            ok, msg = start_signup(nu_input.strip(), np_input, ne_input)
            if ok:
              st.session_state.signup_pending = nu_input.strip()
              st.rerun()
            else:
              st.error(msg)
          else:
            success, msg = register_user(nu_input.strip(), np_input, ne_input)
            (st.success if success else st.error)(msg)

  if needs_setup:
    with login_tabs[2]:
      st.subheader('최종관리자 설정 (최초 1회)')
      if get_secret('ADMIN_SETUP_CODE'):
        st.caption('호스팅 설정(Secrets)에 등록해 둔 `ADMIN_SETUP_CODE` 값을 입력하세요.')
      else:
        st.caption(
            '설정 코드는 서버 로그(콘솔) 또는 앱 폴더의 `setup_code.txt`에서 확인할 수 있어요.'
            ' 호스팅 중이라면 Secrets에 `ADMIN_SETUP_CODE`를 등록해 두는 방법이 가장 쉬워요.'
        )
      st.caption(
          '이미 가입한 계정의 아이디·비밀번호를 입력하면 그 계정이 최종관리자가 되고,'
          ' 없는 아이디면 새 계정이 만들어집니다.'
      )
      with st.expander('💾 백업 파일로 복원 (재시작으로 데이터가 사라졌을 때)'):
        st.caption('이전에 받아 둔 백업(.db)을 올리면 회원·닉네임이 그대로 복구돼요. 설정 코드가 필요합니다.')
        setup_up = st.file_uploader(
            '백업 파일 (.db)', type=['db', 'sqlite', 'sqlite3'], key='setup_restore_upload'
        )
        setup_code_in = st.text_input('설정 코드', type='password', key='setup_restore_code')
        if st.button('♻️ 복원하기', key='setup_restore_btn'):
          lock_msg = check_login_lock()
          if lock_msg:
            st.error(lock_msg)
          elif setup_up is None:
            st.warning('백업 파일을 선택해 주세요.')
          elif not hmac.compare_digest(setup_code_in.strip(), get_setup_code()):
            note_auth_failure()
            st.error('설정 코드가 올바르지 않습니다.')
          else:
            try:
              r_ok, r_msg = restore_database(setup_up.getvalue())
            except Exception as e:
              r_ok, r_msg = False, f'복원 중 오류가 발생했습니다: {e}'
            if r_ok:
              flash('✅ ' + r_msg + ' 백업 시점의 계정으로 로그인해 주세요.')
              st.rerun()
            else:
              st.error(r_msg)
      with st.form('setup_form'):
        su_input = st.text_input('아이디')
        sp_input = st.text_input('비밀번호', type='password')
        sc_input = st.text_input('설정 코드')
        if st.form_submit_button('최종관리자로 설정'):
          ok, msg = claim_super_admin(su_input.strip(), sp_input, sc_input)
          if ok:
            flash(msg)
            st.rerun()
          else:
            st.error(msg)
  st.stop()

# ==========================================
# 8. 사이드바 (로그인 후)
# ==========================================
if not refresh_session_role():
  st.rerun()
username = st.session_state.username
st.sidebar.title(f'환영합니다, {username}님!')
if st.session_state.role != ROLE_USER:
  st.sidebar.markdown(f'**[{ROLE_LABELS[st.session_state.role]}]**')
  st.sidebar.caption("상단 '👑 관리자 패널' 탭에서 관리 기능을 사용하세요.")

if st.sidebar.button('로그아웃'):
  logout()
  st.rerun()

if st.session_state.must_change_pw:
  st.sidebar.error('⚠️ 기본 비밀번호를 사용 중입니다. 아래에서 꼭 변경해 주세요.')

with st.sidebar.expander('🔑 비밀번호 변경', expanded=st.session_state.must_change_pw):
  with st.form('pw_form', clear_on_submit=True):
    old_pw = st.text_input('현재 비밀번호', type='password')
    new_pw = st.text_input('새 비밀번호 (8자 이상)', type='password')
    new_pw2 = st.text_input('새 비밀번호 확인', type='password')
    if st.form_submit_button('변경'):
      if new_pw != new_pw2:
        st.error('새 비밀번호가 일치하지 않습니다.')
      else:
        ok, msg = change_password(username, old_pw, new_pw)
        if ok:
          st.session_state.must_change_pw = False
          flash(msg)
          st.rerun()
        else:
          st.error(msg)


with st.sidebar.expander('🔐 로그인 관리'):
  with db() as conn:
    n_dev = conn.execute(
        'SELECT COUNT(*) FROM login_tokens WHERE username = ? AND expires_at > ?',
        (username, time.time()),
    ).fetchone()[0]
  st.caption(
      f'로그인 상태가 유지 중인 기기: {n_dev}대 '
      f'(최대 {REMEMBER_MAX_DAYS}일 · {REMEMBER_IDLE_DAYS}일 접속 안 하면 만료)'
  )
  if st.button('모든 기기에서 로그아웃'):
    with db() as conn:
      conn.execute('DELETE FROM login_tokens WHERE username = ?', (username,))
    logout()
    st.rerun()

with st.sidebar.expander('📧 이메일 · 보안'):
  sec = get_security(username)
  if sec['email']:
    st.caption(f"이메일: {mask_email(sec['email'])} " + ('✅ 인증됨' if sec['verified'] else '(미인증)'))
  else:
    st.caption('등록된 이메일이 없어요. 등록하면 **비밀번호 찾기**와 **2단계 인증**을 쓸 수 있어요.')
  if not smtp_configured():
    st.caption('※ 메일 발송이 설정되지 않아 이 기능들을 쓸 수 없어요 (관리자에게 문의).')
  else:
    if st.session_state.get('email_verify_pending'):
      st.info('📧 이메일로 보낸 6자리 코드를 입력해 주세요.')
      with st.form('email_verify_form'):
        ev_code = st.text_input('인증 코드 (6자리)', max_chars=6)
        ev_submit = st.form_submit_button('인증')
      if ev_submit:
        ok, msg = finish_email_verify(username, ev_code)
        if ok:
          st.session_state.email_verify_pending = False
          flash(msg)
          st.rerun()
        else:
          st.error(msg)
      if st.button('취소', key='email_verify_cancel'):
        st.session_state.email_verify_pending = False
        st.rerun()
    else:
      with st.form('email_register_form'):
        ev_email = st.text_input('이메일 등록/변경', placeholder='name@example.com')
        if st.form_submit_button('인증 코드 보내기'):
          ok, msg = start_email_verify(username, ev_email)
          if ok:
            st.session_state.email_verify_pending = True
            st.rerun()
          else:
            st.error(msg)
    st.divider()
    tw_new = st.toggle('🔒 로그인 시 이메일 인증 코드 (2단계 인증)', value=bool(sec['twofa']), disabled=not sec['verified'], key='toggle_twofa')
    if tw_new != bool(sec['twofa']):
      ok, msg = set_security_flag(username, 'twofa', tw_new)
      flash(msg, 'success' if ok else 'error')
      st.rerun()
    al_new = st.toggle('🔔 새 로그인 알림 메일', value=bool(sec['alert']), disabled=not sec['verified'], key='toggle_alert')
    if al_new != bool(sec['alert']):
      ok, msg = set_security_flag(username, 'login_alert', al_new)
      flash(msg, 'success' if ok else 'error')
      st.rerun()
    st.caption('로그인 상태 유지(자동 로그인)로 들어올 때는 2단계 인증을 다시 묻지 않아요.')

st.sidebar.divider()
st.sidebar.selectbox(
    '🔍 전적 검색 플랫폼',
    list(STATS_PLATFORMS),
    key='stats_platform',
    help='닉네임을 클릭하면 이 플랫폼 기준 DAK.GG 전적 페이지가 새 탭으로 열립니다.',
)
st.sidebar.selectbox(
    '📱 화면 모드',
    ['자동', '모바일', 'PC'],
    key='view_mode',
    help='자동: 접속 기기에 맞춰 표시 · 목록이 불편하면 직접 바꿔보세요.',
)

# ==========================================
# 9. 메인 영역
# ==========================================
show_flash()

tab_labels = [
    '📸 이미지 수집 및 관리', '🔎 닉네임 검색', '🤝 함께한 사람',
    '⭐ 즐겨찾기·메모', '📊 내 닉네임 목록',
]
if st.session_state.is_admin:
  tab_labels.append('👑 관리자 패널')
_tabs = st.tabs(tab_labels)
main_tab1, main_tab_search, main_tab_squad, main_tab_notes, main_tab2 = _tabs[:5]
if st.session_state.is_admin:
  main_tab3 = _tabs[5]

# ------------------------------------------
# [탭 1] 이미지 수집 및 관리
# ------------------------------------------
with main_tab1:
  st.header(f'📸 배틀그라운드 스크린샷 닉네임 추출 (스쿼드 최대 {MAX_SQUAD}명)')
  st.write(
      f'게임 스크린샷을 업로드하면 스쿼드 최대 인원인 **최대 {MAX_SQUAD}명까지만**'
      ' 닉네임을 추출합니다.'
  )

  with st.expander('⚙️ AI / OCR 인식 설정', expanded=False):
    model_ids = [m[0] for m in MODEL_CATALOG]
    default_id, saved_custom = resolve_saved_model(get_user_model(username))
    selected_model = st.selectbox(
        '사용할 모델 / 인식 엔진 선택',
        model_ids,
        index=model_ids.index(default_id),
        format_func=lambda i: MODEL_BY_ID[i][1],
    )
    provider = MODEL_BY_ID[selected_model][2]

    custom_model = ''
    if selected_model == CUSTOM_OPENROUTER:
      custom_model = st.text_input(
          'OpenRouter 모델 ID',
          value=saved_custom,
          placeholder='예: 제공자/모델명:free (이미지 입력 지원 모델)',
      ).strip()

    saved_key, env_key, api_key_input = '', '', ''
    if provider == 'local':
      st.caption('API 키 없이 동작합니다. 닉네임 영역만 잘라 올리면 더 정확해요.')
    else:
      info = PROVIDERS[provider]
      saved_key = get_saved_key(username, provider)
      env_key = get_secret(info['env'])
      api_key_input = st.text_input(
          f"{info['name']} API 키",
          type='password',
          placeholder=(
              '저장된 키 사용 중 (바꿀 때만 입력)' if saved_key else '키를 입력하세요'
          ),
      ).strip()
      st.caption(f"{info['note']} · [키 발급]({info['url']})")
      if env_key and not saved_key and not api_key_input:
        st.caption(f"서버 환경변수 {info['env']} 를 사용합니다.")
      if Fernet is None:
        st.warning(
            '`cryptography` 미설치: API 키가 암호화되지 않고 저장됩니다.'
            ' (pip install cryptography)'
        )

    col_save, col_del = st.columns(2)
    if col_save.button('설정 저장'):
      to_save = selected_model
      if selected_model == CUSTOM_OPENROUTER and custom_model:
        to_save = f'openrouter:{custom_model}'
      save_user_model(username, to_save)
      if provider != 'local' and api_key_input:
        save_key(username, provider, api_key_input)
      flash('설정이 저장되었습니다.')
      st.rerun()
    if provider != 'local' and saved_key and col_del.button('키 삭제'):
      delete_key(username, provider)
      flash('저장된 API 키를 삭제했습니다.')
      st.rerun()

    active_key = api_key_input or saved_key or env_key

  engine_label = (
      custom_model
      if selected_model == CUSTOM_OPENROUTER and custom_model
      else MODEL_BY_ID[selected_model][1]
  )
  st.caption(f'🤖 현재 인식 엔진: {engine_label}  (위 "AI / OCR 인식 설정"에서 변경)')

  uploaded_files = st.file_uploader(
      '스크린샷 업로드 (여러 장 가능)',
      type=['png', 'jpg', 'jpeg', 'webp'],
      accept_multiple_files=True,
  )
  files = list(uploaded_files or [])
  if len(files) > MAX_BATCH_FILES:
    st.warning(f'한 번에 최대 {MAX_BATCH_FILES}장까지 처리해요. 앞의 {MAX_BATCH_FILES}장만 사용합니다.')
    files = files[:MAX_BATCH_FILES]
  loaded = []  # [(파일명, 이미지)]
  for f in files:
    if f.size > MAX_UPLOAD_MB * 1024 * 1024:
      st.error(f'{f.name}: 파일 크기는 {MAX_UPLOAD_MB}MB 이하여야 합니다.')
      continue
    try:
      loaded.append((f.name, load_image(f.getvalue())))
    except Exception:
      st.error(f'{f.name}: 이미지를 열 수 없습니다. 올바른 이미지 파일인지 확인해 주세요.')
  image = loaded[0][1] if loaded else None

  if image is not None:
    with st.expander('🎯 인식 영역 · 정확도 향상', expanded=True):
      use_crop = st.checkbox(
          '특정 영역만 인식 (기본: 왼쪽 하단 스쿼드 목록)',
          value=True,
          help='머리 위 이름·채팅 등 다른 글자가 섞이는 것을 막아 정확도가 크게 올라갑니다.',
      )
      crop_x, crop_y = (0.0, 100.0), (0.0, 100.0)
      if use_crop:
        region_mode = (
            st.radio('영역 지정 방식', ['슬라이더', '드래그'], horizontal=True)
            if st_cropper
            else '슬라이더'
        )
        if region_mode == '드래그':
          try:
            crop_x, crop_y = pick_region_by_drag(image)
          except Exception as e:
            st.warning(f'드래그 선택을 쓸 수 없어 슬라이더로 전환합니다. ({e})')
            region_mode = '슬라이더'
        if region_mode == '슬라이더':
          st.caption('해상도나 UI 크기가 달라 잘리면 범위를 조절하세요 (이미지 크기 대비 %).')
          sc1, sc2 = st.columns(2)
          crop_x = sc1.slider(
              '가로 범위 (왼쪽 → 오른쪽)', 0.0, 100.0, DEFAULT_CROP_X,
              step=0.5, key='crop_x',
          )
          crop_y = sc2.slider(
              '세로 범위 (위 → 아래)', 0.0, 100.0, DEFAULT_CROP_Y,
              step=0.5, key='crop_y',
          )
          st.button('↺ 기본 영역으로 되돌리기', on_click=reset_region)
        if not st_cropper:
          st.caption('드래그로 영역을 고르려면 `pip install streamlit-cropper` 후 다시 실행하세요.')

      st.markdown('**🔍 업스케일 · 보정**')
      scale_label = st.radio(
          '업스케일 배율',
          list(UPSCALE_OPTIONS),
          horizontal=True,
          help='글씨가 작거나 흐릿하면 2x~4x로 키워 보세요. 자동은 영역이 작을 때만 확대합니다.',
      )
      oc1, oc2 = st.columns(2)
      contrast = oc1.checkbox('대비 자동 보정', value=False)
      sharpen = oc2.checkbox('선명하게 (샤프닝)', value=False)

    target = prepare_target(image, use_crop, crop_x, crop_y, scale_label, contrast, sharpen)
    caption = f'실제 인식 대상 · {target.width}×{target.height}px'
    if len(loaded) > 1:
      st.caption(
          f'미리보기는 첫 번째 이미지({loaded[0][0]}) 기준이에요.'
          f' 같은 영역·보정 설정이 {len(loaded)}장 모두에 적용됩니다.'
      )

    if use_crop:
      col_a, col_b = st.columns([3, 2])
      with col_a:
        show_image(draw_region_overlay(image, crop_x, crop_y), '선택 영역 (밝은 부분이 인식 대상)')
      with col_b:
        show_image(target, caption)
    else:
      show_image(target, caption)

    run_label = '🤖 닉네임 자동 추출 시작' + (f' ({len(loaded)}장)' if len(loaded) > 1 else '')
    if st.button(run_label):
      label = custom_model if selected_model == CUSTOM_OPENROUTER else selected_model
      new_batches, notices = [], []
      progress = st.progress(0.0, text='분석 준비 중...')
      for i, (name, img_i) in enumerate(loaded):
        progress.progress(i / len(loaded), text=f'[{label}] {name} 분석 중... ({i + 1}/{len(loaded)})')
        tgt = target if i == 0 else prepare_target(
            img_i, use_crop, crop_x, crop_y, scale_label, contrast, sharpen
        )
        try:
          names, notice = run_extraction(tgt, selected_model, custom_model, active_key)
        except Exception as e:
          st.error(f'{name}: {friendly_error(e)}')
          continue
        if notice and notice not in notices:
          notices.append(notice)
        if names:
          new_batches.append({'name': name, 'entries': names})
        else:
          st.warning(f'{name}: 인식된 닉네임이 없습니다.')
      progress.empty()
      for n in notices:
        st.info(n)
      if new_batches:
        st.session_state['extracted_batches'] = new_batches
        st.session_state.extract_ver += 1
        total = sum(len(b['entries']) for b in new_batches)
        st.success(f'닉네임 추출 완료! ({len(new_batches)}장 · 총 {total}명)')

  # 추출된 닉네임 확인 및 등록
  batches = st.session_state.get('extracted_batches')
  if batches:
    st.divider()
    st.subheader('✨ 추출된 닉네임 확인 및 선택 등록')
    ver = st.session_state.extract_ver
    st.caption('글자가 틀렸을 수 있어요. 닉네임을 고친 뒤 🔍 버튼으로 전적 페이지를 열어 확인하세요.')
    plat = current_platform()
    mobile = is_mobile()

    picked_batches = []  # [(파일명, 선택된 항목)]
    current_all = []     # 글자 혼동 확인용 [(batch idx, idx, 닉네임, 클랜)]
    for b, batch in enumerate(batches):
      picked = []
      if len(batches) > 1:
        st.markdown(f'#### 📷 {batch["name"]}')
      if mobile:
        # 모바일: 한 명당 카드 1개 (체크 · "[클랜] 닉네임" 한 칸 · 전적 버튼)
        for idx, entry in enumerate(batch['entries']):
          with bordered_container():
            is_checked = st.checkbox(f'{idx + 1}번 저장', value=True, key=f'mchk_{ver}_{b}_{idx}')
            raw = st.text_input(
                f'{idx + 1}번 · [클랜] 닉네임',
                value=entry_label(entry['clan'], entry['nickname']),
                key=f'mtxt_{ver}_{b}_{idx}',
                help='형식: [클랜] 닉네임 (클랜이 없으면 닉네임만)',
            )
            parsed = make_entry(raw)
            if parsed['nickname']:
              stats_button('🔍 전적 보기', stats_url(parsed['nickname'], plat))
            else:
              st.button('🔍 전적 보기', disabled=True, key=f'mnolink_{ver}_{b}_{idx}')
          if parsed['nickname']:
            current_all.append((b, idx, parsed['nickname'], parsed['clan']))
          if is_checked and parsed['nickname']:
            picked.append(parsed)
      else:
        head = st.columns([1, 2, 4, 2])
        for col, text in zip(head, ['선택', '클랜', '닉네임 (수정 가능)', '전적 확인']):
          col.caption(text)
        for idx, entry in enumerate(batch['entries']):
          col1, col2, col3, col4 = st.columns([1, 2, 4, 2])
          with col1:
            is_checked = st.checkbox('선택', value=True, key=f'chk_{ver}_{b}_{idx}')
          with col2:
            clan_edit = st.text_input(
                f'클랜 {idx + 1}',
                value=entry['clan'],
                key=f'clan_{ver}_{b}_{idx}',
                placeholder='(없음)',
                label_visibility='collapsed',
            )
          with col3:
            nick_edit = st.text_input(
                f'닉네임 {idx + 1}',
                value=entry['nickname'],
                key=f'txt_{ver}_{b}_{idx}',
                label_visibility='collapsed',
            ).strip()
          with col4:
            if nick_edit:
              stats_button('🔍 전적 보기', stats_url(nick_edit, plat))
            else:
              st.button('🔍 전적 보기', disabled=True, key=f'nolink_{ver}_{b}_{idx}')
          if nick_edit:
            current_all.append((b, idx, nick_edit, clan_edit.strip(' []()')))
          if is_checked and nick_edit:
            picked.append({'clan': clan_edit.strip(' []()'), 'nickname': nick_edit})
      picked_batches.append((batch['name'], picked))

    # 글자 혼동 도우미: DB에 철자만 다른 닉네임이 있으면 알려주고, 후보 링크를 보여준다
    hints = [
        (b, idx, nick, find_similar_in_db(nick), confusable_variants(nick))
        for b, idx, nick, _clan in current_all
    ]
    if any(sim for *_x, sim, _v in hints):
      st.warning('🔤 DB에 철자만 다른 닉네임이 있어요. 같은 사람이라면 아래 버튼으로 바꿔 주세요.')
      for b, idx, nick, sim, _v in hints:
        for j, (sim_nick, sim_clan) in enumerate(sim):
          st.button(
              f'`{nick}` → `{sim_nick}`(으)로 바꾸기',
              key=f'sim_{ver}_{b}_{idx}_{j}',
              on_click=apply_nick_choice,
              args=(f'txt_{ver}_{b}_{idx}', f'clan_{ver}_{b}_{idx}',
                    f'mtxt_{ver}_{b}_{idx}', mobile, sim_nick, sim_clan),
          )
    with_variants = [(nick, v) for _b, _i, nick, _s, v in hints if v]
    if with_variants:
      with st.expander('🔤 헷갈리는 글자 확인 (l/I/1, O/0, S/5 …)'):
        st.caption('비슷한 글자를 바꾼 후보예요. 눌러서 전적 페이지가 열리면 그 철자가 맞는 거예요.')
        for nick, variants in with_variants:
          st.markdown(
              f'**{md_escape(nick)}** → '
              + ' · '.join(f'[{md_escape(v)}]({stats_url(v, plat)})' for v in variants[:10])
          )

    manual_add = st.text_input(
        '직접 추가 (쉼표로 구분, "[클랜] 닉네임" 형식 가능 · 전체 합계'
        f' 최대 {MAX_SQUAD}명)',
        key=f'manual_add_{ver}',
    )

    if st.button('선택한 닉네임 DB에 저장하기', type='primary'):
      plans, error = [], ''
      for name, picked in picked_batches:
        final = dedupe_entries(picked)
        if not final:
          continue
        err = validate_entries(final)
        if err:
          error = f'{name}: {err}'
          break
        plans.append((name, final))
      if not error:
        extra = dedupe_entries([make_entry(n) for n in manual_add.split(',') if n.strip()])
        if extra:
          err = validate_entries(extra)
          if err:
            error = f'직접 추가: {err}'
          else:
            plans.append(('직접 입력', extra))
      if error:
        st.error(error)
      elif not plans:
        st.warning('저장할 닉네임이 선택되지 않았습니다.')
      else:
        added = updated = skipped = 0
        for source, final in plans:
          a, u, sk = save_nicknames(username, final, source)
          added, updated, skipped = added + a, updated + u, skipped + sk
        parts = [f'{added}개 저장']
        if updated:
          parts.append(f'클랜 정보 {updated}개 갱신')
        if skipped:
          parts.append(f'이미 등록된 {skipped}개 건너뜀')
        if len(plans) > 1:
          parts.append(f'{len(plans)}판 기록')
        flash('✅ ' + ', '.join(parts))
        del st.session_state['extracted_batches']
        st.rerun()

  st.divider()
  show_recent_nicknames(10)

# ------------------------------------------
# [탭 검색] 닉네임 검색 · 전적 확인 (모든 회원)
# ------------------------------------------
with main_tab_search:
  st.header('🔎 닉네임 검색 · 전적 확인')
  st.caption('모든 회원이 DB에 올린 닉네임을 함께 검색·조회할 수 있어요.')

  with db() as conn:
    n_total, n_unique, n_today = conn.execute(
        'SELECT COUNT(*), COUNT(DISTINCT lower(nickname)),'
        ' COALESCE(SUM(substr(created_at, 1, 10) = ?), 0) FROM nicknames',
        (datetime.now().strftime('%Y-%m-%d'),),
    ).fetchone()
  with keyed_container('metrics_row'):
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric('등록된 닉네임', n_total)
    mc2.metric('고유 닉네임', n_unique)
    mc3.metric('오늘 추가', n_today)

  query = st.text_input(
      '🔍 닉네임 · 클랜 · 등록자 검색',
      placeholder='일부만 입력해도 검색돼요 (대소문자 구분 없음)',
      key='search_query',
  ).strip()

  if query:
    cand = search_nicknames(query)
    suffix = f' (최대 {SEARCH_LIMIT}건까지 표시)' if len(cand) >= SEARCH_LIMIT else ''
    st.subheader(f'검색 결과 {len(cand)}건{suffix}')
    if cand.empty:
      st.info('검색 결과가 없습니다. 철자를 바꿔서 다시 찾아보세요.')
    else:
      nick_link_table(search_view(cand), nick_col='닉네임', mobile_cols=['경과', '등록자', '내 메모'])
  else:
    cand = load_recent_nicknames(30)
    st.subheader('🕒 최근 추가된 닉네임')
    if cand.empty:
      st.info('아직 등록된 닉네임이 없습니다.')
    else:
      nick_link_table(recent_view(cand), nick_col='닉네임', mobile_cols=['경과', '등록자', '내 메모'])

  # ---- 전적 확인 (오타 수정 후 검색) ----
  st.divider()
  st.subheader('🎯 전적 확인 · 닉네임 수정')
  st.caption(
      'OCR이 글자를 잘못 읽었을 수 있어요. 목록에서 고르거나 직접 입력한 뒤,'
      ' 닉네임을 고쳐서 전적 페이지를 열어보세요.'
  )
  DIRECT = 0
  rows = {int(r.id): r for r in cand.itertuples()}
  pick = st.selectbox(
      '목록에서 선택 (또는 직접 입력)',
      [DIRECT] + list(rows),
      format_func=lambda i: (
          '✍️ 직접 입력'
          if i == DIRECT
          else f'{entry_label(rows[i].clan, rows[i].nickname)} · {rows[i].created_at[:16]}'
      ),
  )
  base = rows.get(pick)
  vc1, vc2 = st.columns([1, 3])
  v_clan = vc1.text_input('클랜', value=base.clan if base else '', key=f'v_clan_{pick}')
  v_nick = vc2.text_input(
      '닉네임 (수정 가능)', value=base.nickname if base else '', key=f'v_nick_{pick}'
  ).strip()

  vb1, vb2 = st.columns(2)
  with vb1:
    if v_nick:
      stats_button('🔍 DAK.GG에서 전적 보기', stats_url(v_nick, current_platform()))
    else:
      st.button('🔍 DAK.GG에서 전적 보기', disabled=True, key='verify_disabled')
  with vb2:
    if base is not None:
      if st.session_state.is_admin or base.username == username:
        unchanged = (v_nick, v_clan.strip(' []()')) == (base.nickname, base.clan)
        if st.button('💾 DB의 닉네임·클랜을 이 값으로 수정', disabled=unchanged or not v_nick):
          ok, msg = update_nickname(
              username, st.session_state.is_admin, int(pick), v_clan, v_nick
          )
          flash(msg, 'success' if ok else 'error')
          st.rerun()
      else:
        st.caption('다른 회원이 등록한 닉네임은 관리자만 수정할 수 있어요.')

  nickname_tools(v_nick, 'search')

# ------------------------------------------
# [탭] 함께한 사람 (스쿼드 기록)
# ------------------------------------------
with main_tab_squad:
  st.header('🤝 함께한 사람')
  st.caption(
      '스크린샷을 저장할 때 한 번에 저장한 스쿼드(2명 이상)를 한 판으로 기록해요.'
      ' 내가 저장한 기록 기준이에요.'
  )
  my_nick = get_my_nickname(username)
  with st.expander('👤 내 닉네임 설정 (목록에서 제외돼요)', expanded=not my_nick):
    my_nick_in = st.text_input('내 닉네임', value=my_nick, key='my_nick_input')
    if st.button('저장', key='my_nick_save'):
      set_my_nickname(username, my_nick_in)
      flash('내 닉네임을 저장했어요.')
      st.rerun()

  partners = top_partners(username, exclude=my_nick)
  if partners.empty:
    st.info('아직 기록이 없어요. 수집 탭에서 스쿼드 스크린샷을 저장하면 여기에 쌓여요.')
  else:
    st.subheader('자주 같이 한 사람')
    nick_link_table(
        pd.DataFrame({
            '닉네임': partners['nickname'],
            '클랜': partners['clan'],
            '함께한 판': partners['games'].map(lambda g: f'{g}판'),
            '마지막': partners['last_at'].map(time_ago),
        }),
        nick_col='닉네임',
        mobile_cols=['함께한 판', '마지막'],
    )

    st.divider()
    st.subheader('👤 이 사람과의 기록')
    who = st.selectbox('닉네임 선택', list(partners['nickname']), key='squad_who')
    st.write(f'**{who}** 님과 함께한 판: **{games_with(username, who)}판**')
    with_who = partners_of(username, who)
    if not with_who.empty:
      st.caption('이 사람과 같은 판에 있던 다른 사람들')
      nick_link_table(
          pd.DataFrame({
              '닉네임': with_who['nickname'],
              '클랜': with_who['clan'],
              '함께한 판': with_who['games'].map(lambda g: f'{g}판'),
              '마지막': with_who['last_at'].map(time_ago),
          }),
          nick_col='닉네임',
          mobile_cols=['함께한 판', '마지막'],
      )
    st.markdown('**최근 함께한 판**')
    st.markdown(squad_markdown(recent_squads(username, who, 10), current_platform()))

  squads_now = recent_squads(username, limit=10)
  if squads_now:
    st.divider()
    st.subheader('🕒 최근 스쿼드')
    st.markdown(squad_markdown(squads_now, current_platform()))

# ------------------------------------------
# [탭] 즐겨찾기 · 메모 (개인)
# ------------------------------------------
with main_tab_notes:
  st.header('⭐ 즐겨찾기 · 메모')
  st.caption('나만 볼 수 있는 개인 메모예요. 닉네임을 누르면 전적 페이지가 열려요.')
  notes_df = load_notes(username)

  def _notes_view(df):
    return pd.DataFrame({
        '닉네임': df['nickname'],
        '태그': df['tags'].str.replace(',', ' · '),
        '메모': df['note'].str.slice(0, 40),
    })

  st.subheader('⭐ 즐겨찾기 전적 바로가기')
  fav_df = notes_df[notes_df['favorite'] == 1]
  if fav_df.empty:
    st.caption('즐겨찾기가 없어요. 아래에서 닉네임을 즐겨찾기로 추가해 보세요.')
  else:
    nick_link_table(_notes_view(fav_df), nick_col='닉네임', mobile_cols=['태그', '메모'])
  other_df = notes_df[notes_df['favorite'] != 1]
  if not other_df.empty:
    with st.expander(f'📝 메모만 있는 닉네임 ({len(other_df)})'):
      nick_link_table(_notes_view(other_df), nick_col='닉네임', mobile_cols=['태그', '메모'])

  st.divider()
  st.subheader('📝 메모 추가 · 수정')
  with db() as conn:
    my_names = [
        r[0] for r in conn.execute(
            'SELECT DISTINCT nickname FROM nicknames WHERE username = ?', (username,)
        )
    ]
  known = sorted(set(my_names) | set(notes_df['nickname']), key=str.lower)
  DIRECT_N = '✍️ 직접 입력'
  pick_n = st.selectbox('닉네임 선택', [DIRECT_N] + known, key='note_pick')
  nick_in = st.text_input(
      '닉네임', value='' if pick_n == DIRECT_N else pick_n, key=f'note_nick_{pick_n}'
  ).strip()
  existing = get_note(username, nick_in) if nick_in else None
  kk = nick_key(nick_in)
  cur_tags = existing['tags'] if existing else []
  tag_options = PRESET_TAGS + [t for t in cur_tags if t not in PRESET_TAGS]
  fav_in = st.checkbox('⭐ 즐겨찾기', value=bool(existing and existing['favorite']), key=f'note_fav_{kk}')
  tags_in = st.multiselect(
      '태그', tag_options, default=[t for t in cur_tags if t in tag_options], key=f'note_tags_{kk}'
  )
  extra_in = st.text_input('직접 태그 (쉼표로 구분)', key=f'note_extra_{kk}')
  note_in = st.text_area(
      '메모', value=existing['note'] if existing else '', max_chars=MAX_NOTE_LEN, key=f'note_text_{kk}'
  )
  nb1, nb2 = st.columns(2)
  if nb1.button('💾 저장', key='note_save', type='primary'):
    ok, msg = save_note(username, nick_in, fav_in, parse_tags(tags_in, extra_in), note_in)
    flash(msg, 'success' if ok else 'error')
    st.rerun()
  if existing and nb2.button('🗑️ 메모 삭제', key='note_delete'):
    delete_note(username, nick_in)
    flash('메모를 삭제했어요.')
    st.rerun()
  if nick_in:
    stats_button('🔍 전적 보기', stats_url(nick_in, current_platform()))
    nickname_tools(nick_in, 'notes')

# ------------------------------------------
# [탭 2] 내 닉네임 목록
# ------------------------------------------
with main_tab2:
  st.header('📊 내가 수집한 닉네임 목록')

  with db() as conn:
    df_my = pd.read_sql(
        "SELECT id, COALESCE(clan, '') AS clan, nickname, source_image,"
        ' created_at FROM nicknames'
        ' WHERE username = ? ORDER BY id DESC',
        conn,
        params=(username,),
    )

  if df_my.empty:
    st.info('아직 수집된 닉네임이 없습니다.')
  else:
    nick_link_table(df_my, mobile_cols=['created_at', 'source_image'])
    st.caption('닉네임을 클릭하면 DAK.GG 전적 페이지가 새 탭으로 열립니다 (검색 없이 바로 프로필).')
    st.download_button(
        label='📥 내 닉네임 목록 CSV 다운로드',
        data=df_my.to_csv(index=False).encode('utf-8-sig'),
        file_name=f'pubg_nicknames_{username}.csv',
        mime='text/csv',
    )

    st.divider()
    st.subheader('🗑️ 닉네임 삭제')
    names_by_id = {
        int(i): entry_label(c, n)
        for i, c, n in zip(df_my['id'], df_my['clan'], df_my['nickname'])
    }
    del_ids = st.multiselect(
        '삭제할 닉네임을 선택하세요 (여러 개 선택 가능)',
        options=list(names_by_id),
        format_func=lambda i: f'{names_by_id[i]} (#{i})',
    )
    if st.button('선택한 닉네임 삭제', disabled=not del_ids):
      with db() as conn:
        conn.executemany(
            'DELETE FROM nicknames WHERE id = ? AND username = ?',
            [(i, username) for i in del_ids],
        )
      cleanup_squads()
      flash(f'{len(del_ids)}개의 닉네임이 삭제되었습니다.')
      st.rerun()

# ------------------------------------------
# [탭 3] 관리자 패널 (관리자 전용)
# ------------------------------------------
if st.session_state.is_admin:
  with main_tab3:
    st.header('👑 관리자 패널')
    if st.session_state.role == ROLE_SUPER:
      _last = get_setting('last_backup_at', '')
      try:
        _age_h = (datetime.now() - datetime.strptime(_last, '%Y-%m-%d %H:%M:%S')).total_seconds() / 3600
      except ValueError:
        _age_h = None
      if _age_h is None:
        st.warning('💾 아직 DB 백업을 만든 적이 없어요. 호스팅이 재시작되면 데이터가 사라질 수 있으니 `시스템` 탭에서 백업을 받아 두세요.')
      elif _age_h > 24:
        st.warning(f'💾 마지막 백업이 {_age_h / 24:.0f}일 전이에요. `시스템` 탭에서 새 백업을 받아 두세요.')
    a_dash, a_nick, a_user, a_log, a_sys = st.tabs(
        ['📈 대시보드', '🗂 닉네임 관리', '👥 회원 관리', '📜 활동 로그', '⚙️ 시스템 (최종관리자)']
    )

    # ---------- 대시보드 ----------
    with a_dash:
      stats = load_stats()
      with keyed_container('metrics_grid'):
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric('전체 회원', stats['users'], f"관리자 {stats['admins']}명")
        m2.metric('수집된 닉네임', stats['nicks'])
        m3.metric('고유 닉네임', stats['unique'], help='대소문자를 무시한 중복 제거 수')
        m4.metric('오늘 수집', stats['today'])
        m5.metric('가입 상태', '허용' if get_setting('allow_signup', '1') == '1' else '중지')

      left, right = st.columns(2)
      with left:
        st.subheader('최근 14일 수집 추이')
        st.bar_chart(stats['daily'])
      with right:
        st.subheader('회원별 수집 순위 (상위 10)')
        show_df(stats['by_user'])

      left2, right2 = st.columns(2)
      with left2:
        st.subheader('클랜 분포 (상위 10)')
        if stats['by_clan'].empty:
          st.caption('클랜 정보가 있는 닉네임이 아직 없습니다.')
        else:
          show_df(stats['by_clan'])
      with right2:
        st.subheader('최근 수집 10건')
        nick_link_table(stats['recent'], nick_col='닉네임')

      st.divider()
      st.subheader('🕒 시간대별 · 요일별 등록')
      _hdf, _ddf = load_time_stats()
      tcol1, tcol2 = st.columns(2)
      with tcol1:
        st.caption('시간대별 (한국 시간 기준)')
        st.bar_chart(_hdf)
      with tcol2:
        st.caption('요일별')
        st.bar_chart(_ddf)

    # ---------- 닉네임 관리 ----------
    with a_nick:
      with db() as conn:
        df_all = pd.read_sql(
            'SELECT id, username, COALESCE(clan, \'\') AS clan, nickname,'
            ' source_image, created_at FROM nicknames ORDER BY id DESC',
            conn,
        )

      f1, f2, f3 = st.columns([2, 2, 3])
      sel_user = f1.selectbox('수집자', ['전체'] + sorted(df_all['username'].unique()))
      sel_clan = f2.selectbox(
          '클랜',
          ['전체', '(클랜 없음)'] + sorted(c for c in df_all['clan'].unique() if c),
      )
      query = f3.text_input('🔍 닉네임 / 클랜 / 수집자 검색').strip()

      if sel_user != '전체':
        df_all = df_all[df_all['username'] == sel_user]
      if sel_clan == '(클랜 없음)':
        df_all = df_all[df_all['clan'] == '']
      elif sel_clan != '전체':
        df_all = df_all[df_all['clan'] == sel_clan]
      if query:
        df_all = df_all[
            df_all['nickname'].str.contains(query, case=False, regex=False, na=False)
            | df_all['clan'].str.contains(query, case=False, regex=False, na=False)
            | df_all['username'].str.contains(query, case=False, regex=False, na=False)
        ]

      st.caption(f'총 {len(df_all)}건 · 클랜/닉네임 칸은 표에서 직접 수정할 수 있습니다.')
      st.download_button(
          label='📥 현재 목록 CSV 다운로드',
          data=df_all.to_csv(index=False).encode('utf-8-sig'),
          file_name='pubg_all_nicknames.csv',
          mime='text/csv',
      )

      if df_all.empty:
        st.info('조건에 맞는 데이터가 없습니다.')
      else:
        view = df_all.head(500)
        if len(df_all) > 500:
          st.caption('표에는 최근 500건만 표시됩니다 (CSV에는 전체 포함).')
        plat = current_platform()
        edited = show_editor(
            view.assign(전적=[stats_url(n, plat) for n in view['nickname']]),
            disabled=['id', 'username', 'source_image', 'created_at', '전적'],
            key=f'nick_editor_{sel_user}_{sel_clan}_{query}_{plat}',
            column_config={'전적': link_column('전적', '🔍 전적 보기')},
        )
        if st.button('✏️ 수정 내용 저장'):
          changes = [
              (int(old.id), new.clan, new.nickname)
              for old, new in zip(view.itertuples(), edited.itertuples())
              if (old.clan, old.nickname) != (new.clan, new.nickname)
          ]
          if not changes:
            st.info('변경된 내용이 없습니다.')
          else:
            updated, skipped = admin_update_nicknames(changes)
            msg = f'{updated}건 수정'
            if skipped:
              msg += f', {skipped}건 건너뜀 (빈 값·길이 초과·중복)'
            flash(msg, 'success' if updated else 'warning')
            st.rerun()

        st.divider()
        labels = {
            int(r.id): f'#{r.id} · {entry_label(r.clan, r.nickname)} ({r.username})'
            for r in view.itertuples()
        }
        admin_del = st.multiselect(
            '삭제할 데이터 선택',
            options=list(labels),
            format_func=lambda i: labels[i],
        )
        if st.button('🗑️ 선택 데이터 삭제', disabled=not admin_del):
          admin_delete_nicknames(admin_del)
          flash(f'{len(admin_del)}개의 데이터가 삭제되었습니다.')
          st.rerun()

      with st.expander('🔁 여러 회원이 함께 수집한 닉네임'):
        with db() as conn:
          df_dup = pd.read_sql(
              'SELECT MIN(nickname) AS nickname, COUNT(DISTINCT username) AS users,'
              ' GROUP_CONCAT(DISTINCT username) AS collectors FROM nicknames'
              ' GROUP BY lower(nickname) HAVING users > 1 ORDER BY users DESC',
              conn,
          )
        if df_dup.empty:
          st.caption('겹치는 닉네임이 없습니다.')
        else:
          nick_link_table(
              df_dup.rename(columns={
                  'nickname': '닉네임', 'users': '수집 회원 수', 'collectors': '수집자'
              }),
              nick_col='닉네임',
              mobile_cols=['수집자'],
          )

    # ---------- 회원 관리 ----------
    with a_user:
      my_role = st.session_state.role
      df_users = load_user_overview()
      role_map = dict(zip(df_users['username'], df_users['role']))
      users_view = pd.DataFrame({
          '아이디': df_users['username'],
          '권한': df_users['role'].map(ROLE_LABELS),
          '이메일': df_users['email'],
          '이메일 인증': df_users['email_verified'].map({1: '✅ 완료'}).fillna(''),
          '2단계 인증': df_users['twofa'].map({1: '✅'}).fillna(''),
          '가입일': df_users['created_at'],
          '수집 닉네임': df_users['nick_count'],
          '마지막 수집': df_users['last_collected'],
          '저장된 API 키': df_users['key_count'],
      })
      show_df(users_view)
      st.caption(
          '권한 순서: 👑 최종관리자 > 🛡️ 일반관리자 > 일반회원 · 자기보다 낮은 권한의'
          ' 회원만 관리할 수 있어요.'
      )

      # ----- 내 권한 (본인 권한은 본인만 변경) -----
      st.divider()
      st.subheader('🙋 내 권한')
      st.write(f'현재 권한: **{ROLE_LABELS[my_role]}**')
      if my_role == ROLE_SUPER:
        heirs = [u for u in df_users['username'] if u != username]
        with st.expander('👑 최종관리자 권한 위임 (본인만 할 수 있어요)'):
          if not heirs:
            st.caption('위임할 다른 회원이 없습니다.')
          else:
            with st.form('transfer_form'):
              heir = st.selectbox('위임받을 회원', heirs)
              pw_confirm = st.text_input('내 비밀번호 확인', type='password')
              agree = st.checkbox('위임하면 나는 일반관리자가 되는 것에 동의합니다.')
              if st.form_submit_button('위임하기'):
                if not agree:
                  st.warning('동의에 체크해 주세요.')
                else:
                  ok, msg = admin_transfer_super(heir, pw_confirm)
                  flash(msg, 'success' if ok else 'error')
                  st.rerun()
      else:
        with st.expander('⬇️ 일반회원으로 내려가기'):
          st.caption('본인 권한은 본인만 낮출 수 있어요. 다시 올리려면 최종관리자의 승인이 필요합니다.')
          with st.form('self_demote_form'):
            agree = st.checkbox('일반회원으로 내려갑니다.')
            if st.form_submit_button('권한 내리기'):
              if not agree:
                st.warning('동의에 체크해 주세요.')
              else:
                ok, msg = change_own_role_to_user()
                flash(msg, 'success' if ok else 'error')
                st.rerun()

      # ----- 회원 작업 -----
      st.divider()
      st.subheader('회원 작업')
      target = st.selectbox(
          '대상 회원',
          list(df_users['username']),
          format_func=lambda u: f'{u} · {ROLE_LABELS[role_map[u]]}',
      )
      t_role = role_map[target]

      if target == username:
        st.info('자신의 계정은 여기서 작업할 수 없어요. 비밀번호는 사이드바에서, 권한은 위 "내 권한"에서 바꿀 수 있습니다.')
      elif ROLE_RANK[t_role] >= ROLE_RANK[my_role]:
        st.info('같거나 높은 권한의 계정은 관리할 수 없습니다.')
      else:
        actions = ['비밀번호 초기화', '2단계 인증 해제', 'API 키 삭제', '계정 삭제']
        if my_role == ROLE_SUPER:
          actions.insert(0, '권한 변경')
        action = st.radio('작업', actions, horizontal=True, key=f'user_action_{target}')

        if action == '권한 변경':
          new_role = st.selectbox(
              '변경할 권한',
              [ROLE_ADMIN, ROLE_USER],
              index=[ROLE_ADMIN, ROLE_USER].index(t_role),
              format_func=lambda r: ROLE_LABELS[r],
              key=f'new_role_{target}',
          )
          st.caption('최종관리자 권한은 위임으로만 넘길 수 있어요 (위 "내 권한" 참고).')
          if st.button('권한 변경 적용', key=f'apply_role_{target}'):
            ok, msg = admin_set_role(target, new_role)
            flash(msg, 'success' if ok else 'error')
            st.rerun()

        elif action == '비밀번호 초기화':
          st.caption('임시 비밀번호를 발급합니다. 회원에게 전달하고, 로그인 후 바로 변경하도록 안내하세요.')
          if st.button('임시 비밀번호 발급', key=f'reset_{target}'):
            ok, msg = admin_reset_password(target)
            flash(msg, 'success' if ok else 'error')
            st.rerun()

        elif action == '2단계 인증 해제':
          st.caption('이메일을 잃어버려 로그인할 수 없는 회원을 위해 2단계 인증을 해제합니다.')
          if st.button('2단계 인증 해제', key=f'twofa_off_{target}'):
            ok, msg = admin_disable_twofa(target)
            flash(msg, 'success' if ok else 'error')
            st.rerun()

        elif action == 'API 키 삭제':
          st.caption('해당 회원이 저장해 둔 모든 AI API 키를 삭제합니다.')
          if st.button('API 키 삭제', key=f'clearkeys_{target}'):
            ok, msg = admin_clear_keys(target)
            flash(msg, 'success' if ok else 'error')
            st.rerun()

        else:  # 계정 삭제
          with st.form(f'delete_user_form_{target}'):
            del_nicks = st.checkbox('이 회원이 수집한 닉네임도 함께 삭제', value=True)
            confirm = st.checkbox(f'정말 {target} 계정을 삭제합니다')
            if st.form_submit_button('🗑️ 계정 삭제'):
              if not confirm:
                st.warning('삭제 확인에 체크해 주세요.')
              else:
                try:
                  ok, msg = admin_delete_user(target, del_nicks)
                except Exception as e:
                  ok, msg = False, f'삭제 중 오류가 발생했습니다: {e}'
                flash(msg, 'success' if ok else 'error')
                st.rerun()

    # ---------- 활동 로그 ----------
    with a_log:
      st.caption('관리자 작업 기록입니다 (최근 300건).')
      with db() as conn:
        df_log = pd.read_sql(
            'SELECT created_at, admin, action, target, detail FROM admin_log'
            ' ORDER BY id DESC LIMIT 300',
            conn,
        ).rename(columns={
            'created_at': '시각', 'admin': '관리자', 'action': '작업',
            'target': '대상', 'detail': '상세',
        })
      if df_log.empty:
        st.info('기록된 작업이 없습니다.')
      else:
        show_df(df_log)
        st.download_button(
            label='📥 로그 CSV 다운로드',
            data=df_log.to_csv(index=False).encode('utf-8-sig'),
            file_name='pubg_admin_log.csv',
            mime='text/csv',
        )

    # ---------- 시스템 ----------
    with a_sys:
      if st.session_state.role != 'super':
        st.info('시스템 설정은 최종관리자만 사용할 수 있습니다.')
      else:
        st.subheader('가입 설정')
        signup_now = get_setting('allow_signup', '1') == '1'
        signup_new = st.toggle('신규 회원가입 허용', value=signup_now)
        if signup_new != signup_now:
          set_setting('allow_signup', '1' if signup_new else '0')
          log_admin('가입 설정 변경', '', '허용' if signup_new else '중지')
          flash('신규 회원가입을 ' + ('허용' if signup_new else '중지') + '했습니다.')
          st.rerun()

        verify_now = get_setting('require_email_verify', '1') == '1'
        verify_new = st.toggle(
            '가입 시 이메일 인증 필수',
            value=verify_now,
            help='메일 서버(SMTP)가 설정되어 있어야 실제로 적용됩니다.',
        )
        if verify_new != verify_now:
          set_setting('require_email_verify', '1' if verify_new else '0')
          log_admin('이메일 인증 설정 변경', '', '필수' if verify_new else '해제')
          flash('이메일 인증을 ' + ('필수로' if verify_new else '해제로') + ' 설정했습니다.')
          st.rerun()
        if email_verification_on():
          st.success('✅ 이메일 인증이 적용 중입니다.')
        elif verify_new:
          st.warning('SMTP가 설정되지 않아 현재는 이메일 인증 없이 가입됩니다. 아래에서 메일 서버를 설정하세요.')

        st.divider()
        st.subheader('📧 이메일(SMTP) 설정')
        if get_secret('SMTP_HOST'):
          st.caption('환경변수(SMTP_*)가 설정되어 있어 입력값보다 우선 적용됩니다.')
        sec_options = ['ssl', 'starttls', 'none']
        cur_sec = get_setting('smtp_security', 'ssl')
        with st.form('smtp_form'):
          sm1, sm2, sm3 = st.columns([3, 1, 2])
          smtp_host_in = sm1.text_input('SMTP 서버', value=get_setting('smtp_host', ''), placeholder='smtp.gmail.com')
          try:
            port_default = int(get_setting('smtp_port', '465') or 465)
          except ValueError:
            port_default = 465
          smtp_port_in = sm2.number_input('포트', min_value=1, max_value=65535, value=port_default)
          smtp_sec_in = sm3.selectbox(
              '보안', sec_options,
              index=sec_options.index(cur_sec) if cur_sec in sec_options else 0,
              help='ssl: 465 포트 · starttls: 587 포트',
          )
          smtp_user_in = st.text_input('로그인 아이디 (보통 이메일 주소)', value=get_setting('smtp_user', ''))
          smtp_pw_in = st.text_input(
              '비밀번호 (앱 비밀번호)', type='password',
              placeholder='저장되어 있음 (바꿀 때만 입력)' if get_setting('smtp_password', '') else '',
          )
          smtp_from_in = st.text_input('보내는 사람 주소 (비우면 로그인 아이디)', value=get_setting('smtp_from', ''))
          if st.form_submit_button('💾 SMTP 설정 저장'):
            set_setting('smtp_host', smtp_host_in.strip())
            set_setting('smtp_port', str(int(smtp_port_in)))
            set_setting('smtp_security', smtp_sec_in)
            set_setting('smtp_user', smtp_user_in.strip())
            set_setting('smtp_from', smtp_from_in.strip())
            if smtp_pw_in:
              set_setting('smtp_password', encrypt_secret(smtp_pw_in))
            log_admin('SMTP 설정 변경')
            flash('SMTP 설정을 저장했습니다. 아래에서 테스트 메일로 확인해 보세요.')
            st.rerun()

        test_to = st.text_input('테스트 메일을 받을 주소').strip()
        if st.button('✉️ 테스트 메일 보내기', disabled=not EMAIL_RE.match(test_to or '')):
          try:
            send_email(test_to, '[배틀그라운드 닉네임 관리] 테스트 메일', 'SMTP 설정이 정상입니다.')
            st.success('테스트 메일을 보냈습니다. 받은편지함(스팸함 포함)을 확인하세요.')
          except Exception as e:
            st.error(f'발송 실패: {type(e).__name__}: {e}')
        with st.expander('자주 쓰는 SMTP 설정 예시'):
          st.markdown(
              '- **Gmail**: `smtp.gmail.com` · 465(ssl) · 2단계 인증 후 발급한 *앱 비밀번호* 사용\n'
              '- **네이버**: `smtp.naver.com` · 465(ssl) · 메일 설정에서 POP3/SMTP 사용 켜기\n'
              '- 서비스마다 정책이 다를 수 있으니 각 메일 서비스의 SMTP 안내를 확인하세요.'
          )

        st.divider()
        st.subheader('☁️ 자동 클라우드 동기화')
        _sync_problem = sync_problem()
        _sync_cfg = sync_config()
        _st = sync_state()
        if _sync_problem == '설정되지 않음':
          st.info(
              '아직 설정되지 않았어요. 설정하면 데이터가 바뀔 때마다 **암호화된 스냅샷**이 GitHub 비공개 저장소에 '
              '자동 저장되고, 앱이 재시작돼 데이터가 사라져도 **시작할 때 자동으로 복원**돼요.'
          )
        elif _sync_problem:
          st.error(_sync_problem)
        else:
          st.success('✅ 자동 동기화가 켜져 있어요.')
          st.caption(
              f"저장소 `{_sync_cfg['repo']}` · 경로 `{_sync_cfg['path']}` · "
              f"마지막 업로드 {_st['last_ok'] or '-'} · 업로드 {_st['pushes']}회 · "
              f"시작 시: {_st['restored'] or '확인 전'}"
          )
          if _st['last_error']:
            st.error(f"최근 오류: {_st['last_error']}")
          sy1, sy2 = st.columns(2)
          if sy1.button('🔌 연결 테스트', key='sync_test_btn'):
            _ok, _msg = sync_test()
            (st.success if _ok else st.error)(_msg)
          if sy2.button('⬆️ 지금 업로드', key='sync_push_btn'):
            _ok, _msg = sync_push_now()
            (st.success if _ok else st.error)(_msg)
          with st.expander('⬇️ 원격 스냅샷으로 복원 (현재 데이터를 덮어씀)'):
            with st.form('sync_pull_form'):
              _agree = st.checkbox('현재 데이터가 모두 원격 스냅샷으로 교체되는 것에 동의합니다.')
              if st.form_submit_button('복원하기'):
                if not _agree:
                  st.warning('동의에 체크해 주세요.')
                else:
                  _ok, _msg = sync_pull_restore()
                  if _ok:
                    log_admin('원격 스냅샷 복원', '', _msg)
                    flash('✅ ' + _msg)
                    st.rerun()
                  else:
                    st.error(_msg)
        with st.expander('설정 방법 (처음 한 번)'):
          st.markdown(
              '1. GitHub에서 **Private** 저장소를 새로 만드세요 (예: `pubg-data`). **"Add a README file"를 체크**해 빈 저장소가 되지 않게 하세요.\n'
              '2. GitHub `Settings → Developer settings → Personal access tokens → Fine-grained tokens`에서 새 토큰을 만들고, '
              '**위 저장소만 선택**한 뒤 권한 `Contents: Read and write`를 주세요.\n'
              '3. Streamlit `Settings → Secrets`에 아래를 추가하고 앱을 재시작하세요.\n'
          )
          st.code(
              'GITHUB_SYNC_TOKEN = "github_pat_..."\n'
              'GITHUB_SYNC_REPO = "내GitHub아이디/pubg-data"\n'
              'APP_SECRET_KEY = "아무도-모르는-긴-문자열"   # 필수: 스냅샷 암호화 키\n'
              '# 선택: GITHUB_SYNC_BRANCH = "main",  GITHUB_SYNC_PATH = "pubg_manager.db.enc"',
              language='toml',
          )
          st.caption(
              '⚠️ APP_SECRET_KEY를 잃어버리면 스냅샷을 복호화할 수 없어요. 앱을 **동시에 여러 곳(로컬+호스팅)에서 같은 저장소로 '
              '동기화하지 마세요** (서로 덮어써요). 변경 후 약 1분 안에 업로드되며, 그 사이 재시작되면 마지막 1분 변경은 잃을 수 있어요.'
          )
        st.divider()
        st.subheader('📊 PUBG 공식 API (전적 요약)')
        st.caption(
            '키를 등록하면 검색·즐겨찾기 화면에서 앱 안에 전적 요약을 보여줘요.'
            ' Secrets에 `PUBG_API_KEY`로 넣어도 됩니다. 키 발급: https://developer.pubg.com'
        )
        if get_secret('PUBG_API_KEY'):
          st.success('Secrets의 `PUBG_API_KEY`를 사용 중입니다.')
        with st.form('pubg_api_form'):
          pubg_key_in = st.text_input(
              'PUBG API 키', type='password',
              placeholder='저장되어 있음 (바꿀 때만 입력)' if get_setting('pubg_api_key', '') else '',
          )
          if st.form_submit_button('💾 키 저장') and pubg_key_in.strip():
            set_setting('pubg_api_key', encrypt_secret(pubg_key_in.strip()))
            log_admin('PUBG API 키 저장')
            flash('PUBG API 키를 저장했습니다.')
            st.rerun()
        if get_setting('pubg_api_key', '') and st.button('저장된 PUBG API 키 삭제'):
          set_setting('pubg_api_key', '')
          log_admin('PUBG API 키 삭제')
          flash('PUBG API 키를 삭제했습니다.')
          st.rerun()
        st.divider()
        st.subheader('데이터베이스')
        size_kb = os.path.getsize(DB_FILE) / 1024 if os.path.exists(DB_FILE) else 0
        with db() as conn:
          counts = {
              t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('users', 'nicknames', 'user_keys', 'admin_log')
          }
        st.caption(
            f'파일: `{DB_FILE}` · {size_kb:,.1f} KB · 회원 {counts["users"]} · 닉네임'
            f' {counts["nicknames"]} · 저장된 API 키 {counts["user_keys"]} · 로그 {counts["admin_log"]}'
        )
        st.caption('⚠️ Streamlit Cloud 같은 호스팅에서는 앱이 재시작되면 이 DB 파일이 사라질 수 있어요. 백업을 자주 받아 두세요.')
        _lb = get_setting('last_backup_at', '')
        st.caption(f'마지막 백업: {_lb or "없음"}')
        if st.button('💾 DB 백업 파일 생성'):
          set_setting('last_backup_at', now_str())
          st.session_state['db_backup'] = make_db_backup()
          st.session_state['db_backup_name'] = (
              f'pubg_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
          )
          log_admin('DB 백업 생성')
        if st.session_state.get('db_backup'):
          st.download_button(
              '⬇️ 백업 파일 다운로드',
              data=st.session_state['db_backup'],
              file_name=st.session_state['db_backup_name'],
              mime='application/octet-stream',
          )
          st.warning('백업에는 비밀번호 해시와 암호화된 API 키가 들어 있습니다. 안전하게 보관하세요.')

        st.divider()
        st.subheader('♻️ 백업에서 복원')
        st.caption(
            '백업 파일(.db)을 올리면 현재 데이터가 **모두 백업 내용으로 교체**돼요. '
            '저장된 API 키·SMTP 비밀번호는 백업 때와 같은 `APP_SECRET_KEY`일 때만 복호화됩니다.'
        )
        if st.session_state.get('pre_restore_backup'):
          st.download_button(
              '⬇️ 복원 직전 데이터 내려받기 (되돌리기용)',
              data=st.session_state['pre_restore_backup'],
              file_name=f'pubg_before_restore_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db',
              mime='application/octet-stream',
          )
        restore_up = st.file_uploader(
            '복원할 백업 파일 (.db)', type=['db', 'sqlite', 'sqlite3'], key='restore_upload'
        )
        if restore_up is not None:
          restore_data = restore_up.getvalue()
          r_ok, r_msg, r_stats = validate_backup(restore_data)
          if not r_ok:
            st.error(r_msg)
          else:
            st.info(f'백업 내용: 회원 {r_stats["users"]}명 · 닉네임 {r_stats["nicknames"]}개')
            with st.form('restore_form'):
              r_agree = st.checkbox('현재 데이터가 모두 덮어써지는 것에 동의합니다.')
              if st.form_submit_button('♻️ 복원하기'):
                if not r_agree:
                  st.warning('동의에 체크해 주세요.')
                else:
                  try:
                    pre_snapshot = make_db_backup()
                    r_ok, r_msg = restore_database(restore_data)
                  except Exception as e:
                    r_ok, r_msg = False, f'복원 중 오류가 발생했습니다: {e}'
                  if r_ok:
                    st.session_state['pre_restore_backup'] = pre_snapshot
                    log_admin('DB 복원', '', r_msg)
                    flash('✅ ' + r_msg + ' (백업의 계정 기준으로 다시 로그인이 필요할 수 있어요)')
                    st.rerun()
                  else:
                    st.error(r_msg)

        st.divider()
        st.subheader('환경 점검')
        show_df(env_status())
