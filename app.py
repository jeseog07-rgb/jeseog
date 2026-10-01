"""배틀그라운드 닉네임 수집 및 관리 시스템 (Streamlit)."""

import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import time

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
import pandas as pd
import streamlit as st

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
MAX_IMAGE_SIDE = 3840  # 업로드 이미지 보관 해상도 (작은 글씨 보존)
MAX_FULL_SIDE = 2000  # 전체 화면 인식 시 해상도 제한
MAX_SEND_SIDE = 2400  # AI 전송 시 해상도 제한
MAX_TARGET_SIDE = 4000  # 업스케일 후 최대 변 길이
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SECONDS = 60
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,20}$')

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


@st.cache_resource
def get_fernet():
  """API 키 암호화용 Fernet. cryptography 미설치 시 None."""
  if Fernet is None:
    return None
  secret = os.environ.get('APP_SECRET_KEY')
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
  try:
    yield conn
    conn.commit()
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.close()


def now_str():
  return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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

    # 최초 관리자 계정 생성 (고정 비밀번호 사용 금지)
    if not conn.execute(
        "SELECT 1 FROM users WHERE username = 'admin'"
    ).fetchone():
      password = os.environ.get('ADMIN_PASSWORD')
      generated = not password
      if generated:
        password = secrets.token_urlsafe(9)
      conn.execute(
          'INSERT INTO users (username, password, api_key, model_name,'
          ' is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
          ('admin', hash_password(password), '', FREE_OCR, 1, now_str()),
      )
      if generated:
        print(
            '[최초 관리자 계정] 아이디: admin / 임시 비밀번호:'
            f' {password}  (로그인 후 반드시 변경하세요)',
            flush=True,
        )


init_db()

# 세션 상태 초기화
for _key, _default in {
    'logged_in': False,
    'username': '',
    'is_admin': 0,
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
def login_user(username, password):
  now = time.time()
  if now < st.session_state.lock_until:
    wait = int(st.session_state.lock_until - now) + 1
    return False, f'로그인 시도가 너무 많습니다. {wait}초 후 다시 시도해 주세요.'

  with db() as conn:
    row = conn.execute(
        'SELECT password, is_admin FROM users WHERE username = ?', (username,)
    ).fetchone()

  if row and verify_password(password, row[0]):
    if not row[0].startswith('pbkdf2$'):  # 구버전 해시 자동 업그레이드
      with db() as conn:
        conn.execute(
            'UPDATE users SET password = ? WHERE username = ?',
            (hash_password(password), username),
        )
    st.session_state.update(
        logged_in=True,
        username=username,
        is_admin=row[1],
        login_fails=0,
        must_change_pw=(password == 'admin123'),
    )
    return True, ''

  st.session_state.login_fails += 1
  if st.session_state.login_fails >= MAX_LOGIN_FAILS:
    st.session_state.lock_until = now + LOGIN_LOCK_SECONDS
    st.session_state.login_fails = 0
  return False, '아이디 또는 비밀번호가 올바르지 않습니다.'


def register_user(username, password):
  if not USERNAME_RE.match(username):
    return False, '아이디는 영문/숫자/밑줄(_) 3~20자로 입력해 주세요.'
  if len(password) < 8:
    return False, '비밀번호는 8자 이상이어야 합니다.'
  with db() as conn:
    if conn.execute(
        'SELECT 1 FROM users WHERE username = ?', (username,)
    ).fetchone():
      return False, '이미 존재하는 사용자 아이디입니다.'
    conn.execute(
        'INSERT INTO users (username, password, api_key, model_name,'
        ' is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (username, hash_password(password), '', FREE_OCR, 0, now_str()),
    )
  return True, '회원가입이 완료되었습니다. 로그인해 주세요.'


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


def show_df(df):
  try:
    st.dataframe(df, width='stretch', hide_index=True)
  except Exception:
    st.dataframe(df, use_container_width=True, hide_index=True)


def show_image(img, caption):
  try:
    st.image(img, caption=caption, width='stretch')
  except Exception:
    st.image(img, caption=caption, use_container_width=True)


# ==========================================
# 7. 로그인 화면
# ==========================================
if not st.session_state.logged_in:
  st.title('🎮 배틀그라운드 닉네임 관리 시스템')
  tab_login, tab_register = st.tabs(['로그인', '회원가입'])

  with tab_login:
    st.subheader('로그인')
    with st.form('login_form'):
      u_input = st.text_input('아이디')
      p_input = st.text_input('비밀번호', type='password')
      if st.form_submit_button('로그인'):
        ok, msg = login_user(u_input.strip(), p_input)
        if ok:
          st.rerun()
        else:
          st.error(msg)

  with tab_register:
    st.subheader('신규 회원가입')
    with st.form('register_form'):
      nu_input = st.text_input('사용할 아이디 (영문/숫자/_ 3~20자)')
      np_input = st.text_input('사용할 비밀번호 (8자 이상)', type='password')
      np_confirm = st.text_input('비밀번호 확인', type='password')
      if st.form_submit_button('회원가입'):
        if not nu_input or not np_input:
          st.warning('아이디와 비밀번호를 모두 입력해주세요.')
        elif np_input != np_confirm:
          st.error('비밀번호가 일치하지 않습니다.')
        else:
          success, msg = register_user(nu_input.strip(), np_input)
          (st.success if success else st.error)(msg)
  st.stop()

# ==========================================
# 8. 사이드바 (로그인 후)
# ==========================================
username = st.session_state.username
st.sidebar.title(f'환영합니다, {username}님!')
if st.session_state.is_admin:
  st.sidebar.markdown('👑 **[관리자 권한]**')

if st.sidebar.button('로그아웃'):
  for _k in ('logged_in', 'username', 'is_admin', 'must_change_pw'):
    st.session_state[_k] = {'logged_in': False, 'username': ''}.get(_k, 0)
  st.session_state.pop('extracted_nicknames', None)
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

st.sidebar.divider()
st.sidebar.subheader('⚙️ AI / OCR 인식 설정')

model_ids = [m[0] for m in MODEL_CATALOG]
default_id, saved_custom = resolve_saved_model(get_user_model(username))
selected_model = st.sidebar.selectbox(
    '사용할 모델 / 인식 엔진 선택',
    model_ids,
    index=model_ids.index(default_id),
    format_func=lambda i: MODEL_BY_ID[i][1],
)
provider = MODEL_BY_ID[selected_model][2]

custom_model = ''
if selected_model == CUSTOM_OPENROUTER:
  custom_model = st.sidebar.text_input(
      'OpenRouter 모델 ID',
      value=saved_custom,
      placeholder='예: 제공자/모델명:free (이미지 입력 지원 모델)',
  ).strip()

saved_key, env_key, api_key_input = '', '', ''
if provider == 'local':
  st.sidebar.caption('API 키 없이 동작합니다. 닉네임 영역만 잘라 올리면 더 정확해요.')
else:
  info = PROVIDERS[provider]
  saved_key = get_saved_key(username, provider)
  env_key = os.environ.get(info['env'], '')
  api_key_input = st.sidebar.text_input(
      f"{info['name']} API 키",
      type='password',
      placeholder=(
          '저장된 키 사용 중 (바꿀 때만 입력)' if saved_key else '키를 입력하세요'
      ),
  ).strip()
  st.sidebar.caption(f"{info['note']} · [키 발급]({info['url']})")
  if env_key and not saved_key and not api_key_input:
    st.sidebar.caption(f"서버 환경변수 {info['env']} 를 사용합니다.")
  if Fernet is None:
    st.sidebar.warning(
        '`cryptography` 미설치: API 키가 암호화되지 않고 저장됩니다.'
        ' (pip install cryptography)'
    )

col_save, col_del = st.sidebar.columns(2)
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

# ==========================================
# 9. 메인 영역
# ==========================================
show_flash()

if st.session_state.is_admin:
  main_tab1, main_tab2, main_tab3 = st.tabs(
      ['📸 이미지 수집 및 관리', '📊 내 닉네임 목록', '👑 관리자 대시보드']
  )
else:
  main_tab1, main_tab2 = st.tabs(['📸 이미지 수집 및 관리', '📊 내 닉네임 목록'])

# ------------------------------------------
# [탭 1] 이미지 수집 및 관리
# ------------------------------------------
with main_tab1:
  st.header(f'📸 배틀그라운드 스크린샷 닉네임 추출 (스쿼드 최대 {MAX_SQUAD}명)')
  st.write(
      f'게임 스크린샷을 업로드하면 스쿼드 최대 인원인 **최대 {MAX_SQUAD}명까지만**'
      ' 닉네임을 추출합니다.'
  )

  uploaded_file = st.file_uploader(
      '스크린샷 업로드', type=['png', 'jpg', 'jpeg', 'webp']
  )

  image = None
  if uploaded_file:
    if uploaded_file.size > MAX_UPLOAD_MB * 1024 * 1024:
      st.error(f'파일 크기는 {MAX_UPLOAD_MB}MB 이하여야 합니다.')
    else:
      try:
        image = load_image(uploaded_file.getvalue())
      except Exception:
        st.error('이미지를 열 수 없습니다. 올바른 이미지 파일인지 확인해 주세요.')

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

    region = (
        crop_region(image, crop_x, crop_y)
        if use_crop
        else limit_size(image, MAX_FULL_SIDE)
    )
    target = enhance_image(region, UPSCALE_OPTIONS[scale_label], contrast, sharpen)
    caption = f'실제 인식 대상 · {target.width}×{target.height}px'

    if use_crop:
      col_a, col_b = st.columns([3, 2])
      with col_a:
        show_image(draw_region_overlay(image, crop_x, crop_y), '선택 영역 (밝은 부분이 인식 대상)')
      with col_b:
        show_image(target, caption)
    else:
      show_image(target, caption)

    if st.button('🤖 닉네임 자동 추출 시작'):
      label = custom_model if selected_model == CUSTOM_OPENROUTER else selected_model
      with st.spinner(f'[{label}] 엔진으로 닉네임을 분석 중입니다...'):
        try:
          names, notice = run_extraction(
              target, selected_model, custom_model, active_key
          )
        except Exception as e:
          st.error(friendly_error(e))
        else:
          if notice:
            st.info(notice)
          if names:
            st.session_state['extracted_nicknames'] = names
            st.session_state.extract_ver += 1
            st.success(f'닉네임 추출 완료! ({len(names)}명)')
          else:
            st.warning('인식된 닉네임이 없습니다.')

  # 추출된 닉네임 확인 및 등록
  if st.session_state.get('extracted_nicknames'):
    st.divider()
    st.subheader('✨ 추출된 닉네임 확인 및 선택 등록')
    ver = st.session_state.extract_ver

    with st.form('save_nicknames_form'):
      head1, head2, head3 = st.columns([1, 2, 4])
      head1.caption('선택')
      head2.caption('클랜')
      head3.caption('닉네임')

      picked = []
      for idx, entry in enumerate(st.session_state['extracted_nicknames']):
        col1, col2, col3 = st.columns([1, 2, 4])
        with col1:
          is_checked = st.checkbox('선택', value=True, key=f'chk_{ver}_{idx}')
        with col2:
          clan_edit = st.text_input(
              f'클랜 {idx + 1}',
              value=entry['clan'],
              key=f'clan_{ver}_{idx}',
              placeholder='(없음)',
              label_visibility='collapsed',
          )
        with col3:
          nick_edit = st.text_input(
              f'닉네임 {idx + 1}',
              value=entry['nickname'],
              key=f'txt_{ver}_{idx}',
              label_visibility='collapsed',
          )
        if is_checked and nick_edit.strip():
          picked.append({
              'clan': clan_edit.strip(' []()'),
              'nickname': nick_edit.strip(),
          })

      manual_add = st.text_input(
          '직접 추가 (쉼표로 구분, "[클랜] 닉네임" 형식 가능 · 전체 합계'
          f' 최대 {MAX_SQUAD}명)'
      )

      if st.form_submit_button('선택한 닉네임 DB에 저장하기'):
        extra = [make_entry(n) for n in manual_add.split(',') if n.strip()]
        final = dedupe_entries(picked + extra)
        if not final:
          st.warning('저장할 닉네임이 선택되지 않았습니다.')
        elif len(final) > MAX_SQUAD:
          st.error(f'한 번에 최대 {MAX_SQUAD}명까지만 저장할 수 있습니다.')
        elif any(len(e['nickname']) > MAX_NICK_LEN for e in final):
          st.error(f'닉네임은 {MAX_NICK_LEN}자 이하여야 합니다.')
        elif any(len(e['clan']) > MAX_CLAN_LEN for e in final):
          st.error(f'클랜 태그는 {MAX_CLAN_LEN}자 이하여야 합니다.')
        else:
          added, updated, skipped = save_nicknames(
              username, final, uploaded_file.name if uploaded_file else '직접 입력'
          )
          parts = [f'{added}개 저장']
          if updated:
            parts.append(f'클랜 정보 {updated}개 갱신')
          if skipped:
            parts.append(f'이미 등록된 {skipped}개 건너뜀')
          flash('✅ ' + ', '.join(parts))
          del st.session_state['extracted_nicknames']
          st.rerun()

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
    show_df(df_my)
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
      flash(f'{len(del_ids)}개의 닉네임이 삭제되었습니다.')
      st.rerun()

# ------------------------------------------
# [탭 3] 관리자 대시보드 (관리자 전용)
# ------------------------------------------
if st.session_state.is_admin:
  with main_tab3:
    st.header('👑 관리자 통합 대시보드')
    admin_sub1, admin_sub2 = st.tabs(['전체 수집 닉네임 관리', '회원 관리'])

    with admin_sub1:
      with db() as conn:
        df_all = pd.read_sql(
            "SELECT id, username, COALESCE(clan, '') AS clan, nickname,"
            ' source_image, created_at'
            ' FROM nicknames ORDER BY id DESC',
            conn,
        )

      query = st.text_input('🔍 닉네임 또는 수집자 검색').strip()
      if query:
        df_all = df_all[
            df_all['nickname'].str.contains(query, case=False, regex=False, na=False)
            | df_all['clan'].str.contains(query, case=False, regex=False, na=False)
            | df_all['username'].str.contains(query, case=False, regex=False, na=False)
        ]

      show_df(df_all)
      st.download_button(
          label='📥 전체 닉네임 데이터 CSV 다운로드',
          data=df_all.to_csv(index=False).encode('utf-8-sig'),
          file_name='pubg_all_nicknames.csv',
          mime='text/csv',
      )

      if not df_all.empty:
        st.divider()
        rows_by_id = {
            int(r.id): f'#{r.id} · {entry_label(r.clan, r.nickname)} ({r.username})'
            for r in df_all.itertuples()
        }
        admin_del = st.multiselect(
            '삭제할 데이터 선택',
            options=list(rows_by_id),
            format_func=lambda i: rows_by_id[i],
        )
        if st.button('관리자 권한으로 선택 데이터 삭제', disabled=not admin_del):
          with db() as conn:
            conn.executemany(
                'DELETE FROM nicknames WHERE id = ?', [(i,) for i in admin_del]
            )
          flash(f'{len(admin_del)}개의 데이터가 삭제되었습니다.')
          st.rerun()

    with admin_sub2:
      with db() as conn:
        df_users = pd.read_sql(
            'SELECT username, is_admin, created_at FROM users', conn
        )
      st.subheader('👥 가입된 회원 목록')
      show_df(df_users)
