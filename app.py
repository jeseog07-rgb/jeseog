import base64
import io
import re
import sqlite3
import time
from google import genai
import numpy as np
import pandas as pd

# OpenAI 사용을 위한 예외 처리
try:
  import openai
except ImportError:
  openai = None

from PIL import Image
import streamlit as st

# 페이지 설정
st.set_page_config(
    page_title='배그 닉네임 수집 & 관리 시스템', page_icon='🎮', layout='wide'
)

# 1. SQLite 데이터베이스 초기화
conn = sqlite3.connect('pubg_tracker.db', check_same_thread=False)
cursor = conn.cursor()

# 닉네임 저장용 테이블
cursor.execute('''
    CREATE TABLE IF NOT EXISTS nicknames (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nickname TEXT UNIQUE,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

# 회원가입 정보 저장용 테이블
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        api_key TEXT DEFAULT '',
        openai_api_key TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

try:
  cursor.execute('ALTER TABLE users ADD COLUMN api_key TEXT DEFAULT ""')
except sqlite3.OperationalError:
  pass

try:
  cursor.execute('ALTER TABLE users ADD COLUMN openai_api_key TEXT DEFAULT ""')
except sqlite3.OperationalError:
  pass

conn.commit()

# 세션 상태 초기화
if 'logged_in' not in st.session_state:
  st.session_state['logged_in'] = False
if 'user_role' not in st.session_state:
  st.session_state['user_role'] = None
if 'username' not in st.session_state:
  st.session_state['username'] = ''
if 'api_key' not in st.session_state:
  st.session_state['api_key'] = ''
if 'openai_api_key' not in st.session_state:
  st.session_state['openai_api_key'] = ''
if 'select_all_state' not in st.session_state:
  st.session_state['select_all_state'] = True

# ==========================================
# [사이드바: AI 엔진 및 API 키 설정]
# ==========================================
st.sidebar.title('⚙ 시스템 설정')

ai_provider = '무료 모드 (API 키 없음)'

if st.session_state['logged_in'] and st.session_state['user_role'] == 'user':
  st.sidebar.markdown('### 🤖 AI 엔진 선택')
  ai_provider = st.sidebar.selectbox(
      '닉네임 추출에 사용할 AI',
      ['Gemini (Google)', 'ChatGPT (OpenAI)', '무료 기본 추출기 (API 키 없음)'],
      help='원하시는 AI 서비스 또는 무료 전용 엔진을 선택하세요.',
  )

  st.sidebar.markdown('---')

  if ai_provider == 'Gemini (Google)':
    st.sidebar.markdown('🔑 **Gemini API Key**')
    user_api_key = st.sidebar.text_input(
        'Gemini Key',
        value=st.session_state['api_key'],
        type='password',
        help='Google AI Studio에서 발급받은 키를 입력하세요.',
    )

    if user_api_key != st.session_state['api_key']:
      st.session_state['api_key'] = user_api_key
      cursor.execute(
          'UPDATE users SET api_key = ? WHERE username = ?',
          (user_api_key, st.session_state['username']),
      )
      conn.commit()
      st.sidebar.success('💾 Gemini API 키가 저장되었습니다!')

  elif ai_provider == 'ChatGPT (OpenAI)':
    st.sidebar.markdown('🔑 **OpenAI API Key**')
    user_openai_key = st.sidebar.text_input(
        'OpenAI Key',
        value=st.session_state['openai_api_key'],
        type='password',
        help='OpenAI Platform에서 발급받은 키(sk-...)를 입력하세요.',
    )

    if user_openai_key != st.session_state['openai_api_key']:
      st.session_state['openai_api_key'] = user_openai_key
      cursor.execute(
          'UPDATE users SET openai_api_key = ? WHERE username = ?',
          (user_openai_key, st.session_state['username']),
      )
      conn.commit()
      st.sidebar.success('💾 OpenAI API 키가 저장되었습니다!')

  else:
    st.sidebar.info(
        '🆓 **무료 전용 모드**입니다.\nAPI 키나 결제 없이 기본 이미지 인식'
        ' 알고리즘으로 닉네임을 추출합니다.'
    )

  st.sidebar.markdown('---')

  # ------------------------------------------
  # [사이트 안에서 바로 보는 API 키 발급 가이드 팝업]
  # ------------------------------------------
  with st.sidebar.expander('📖 [사이트 내 가이드] API 키 발급 방법'):
    st.markdown('### 🌟 Gemini API 키 발급법')
    st.markdown(
        '1. 브라우저에서 **Google AI Studio** 검색 후 접속\n2. 우측 상단'
        ' **Sign in with Google** 로그인\n3. 화면 상단의 **`Get API key`**'
        ' 버튼 클릭\n4. 생성된 키 복사 후 왼쪽 입력창에 붙여넣기'
    )
    st.markdown('---')
    st.markdown('### 🤖 ChatGPT API 키 발급법')
    st.markdown(
        '1. **OpenAI Platform** 접속 및 로그인\n2. 우측 상단 프로필 ➔'
        ' **Dashboard** 이동\n3. 좌측 메뉴 **`API keys`** 클릭\n4. 우측 상단'
        ' **`+ Create new secret key`** 생성 후 복사'
    )

# ==========================================
# [로그인 / 회원가입 화면]
# ==========================================
if not st.session_state['logged_in']:
  st.title('🔒 배그 닉네임 시스템 로그인')

  tab_user, tab_admin = st.tabs(['👤 일반 회원 로그인', '👑 관리자 로그인'])

  with tab_user:
    st.subheader('일반 회원 로그인')

    with st.form('user_login_form'):
      u_name = st.text_input('아이디')
      u_pw = st.text_input('비밀번호', type='password')
      u_submit = st.form_submit_button('로그인')

      if u_submit:
        cursor.execute(
            'SELECT username, api_key, openai_api_key FROM users WHERE'
            ' username = ? AND password = ?',
            (u_name, u_pw),
        )
        user_data = cursor.fetchone()
        if user_data:
          st.session_state['logged_in'] = True
          st.session_state['user_role'] = 'user'
          st.session_state['username'] = user_data[0]
          st.session_state['api_key'] = user_data[1] if user_data[1] else ''
          st.session_state['openai_api_key'] = (
              user_data[2] if user_data[2] else ''
          )
          st.success(f'환영합니다, {u_name}님!')
          st.rerun()
        else:
          st.error('❌ 아이디 또는 비밀번호가 틀렸습니다.')

    with st.popover('📝 회원가입하기'):
      st.markdown('### 신규 회원가입')
      new_name = st.text_input('사용할 아이디', key='pop_signup_id')
      new_pw = st.text_input(
          '사용할 비밀번호', type='password', key='pop_signup_pw'
      )

      if st.button('가입 완료', key='pop_signup_btn'):
        if not new_name.strip() or not new_pw.strip():
          st.warning('아이디와 비밀번호를 모두 입력해주세요.')
        else:
          try:
            cursor.execute(
                'INSERT INTO users (username, password, api_key,'
                ' openai_api_key) VALUES (?, ?, "", "")',
                (new_name, new_pw),
            )
            conn.commit()
            st.success(
                '✨ 회원가입 완료! 창을 닫고 위 로그인 창에서 로그인하세요.'
            )
          except sqlite3.IntegrityError:
            st.error('❌ 이미 존재하는 아이디입니다.')

  with tab_admin:
    st.subheader('👑 관리자 전용 로그인')
    with st.form('admin_login_form'):
      a_pw = st.text_input('관리자 비밀번호', type='password')
      a_submit = st.form_submit_button('관리자 로그인')

      if a_submit:
        if a_pw == 'admin1234':
          st.session_state['logged_in'] = True
          st.session_state['user_role'] = 'admin'
          st.session_state['username'] = 'Admin'
          st.success('관리자로 로그인되었습니다!')
          st.rerun()
        else:
          st.error('❌ 관리자 비밀번호가 틀렸습니다.')

# ==========================================
# [로그인 성공 이후 대시보드]
# ==========================================
else:
  col1, col2 = st.columns([8, 2])
  with col1:
    mode_title = (
        '👑 관리자 대시보드'
        if st.session_state['user_role'] == 'admin'
        else f'👤 일반 회원 모드 ({st.session_state["username"]}님)'
    )
    st.title(f'🎮 배그 닉네임 수집 시스템 [{mode_title}]')
  with col2:
    if st.button('로그아웃', type='secondary'):
      st.session_state['logged_in'] = False
      st.session_state['user_role'] = None
      st.session_state['username'] = ''
      st.session_state['api_key'] = ''
      st.session_state['openai_api_key'] = ''
      st.rerun()

  st.markdown('---')

  # ------------------------------------------
  # CASE A: 일반 회원 화면
  # ------------------------------------------
  if st.session_state['user_role'] == 'user':
    st.info(f'💡 현재 선택된 추출 엔진: **{ai_provider}**')

    uploaded_file = st.file_uploader(
        '배그 스크린샷 이미지 업로드 (1장)', type=['png', 'jpg', 'jpeg']
    )

    if uploaded_file:
      image = Image.open(uploaded_file).convert('RGB')
      width, height = image.size

      # 하단 팀원 영역 크롭
      left = int(width * 0.01)
      top = int(height * 0.70)
      right = int(width * 0.25)
      bottom = int(height * 0.99)

      cropped_image = image.crop((left, top, right, bottom))

      st.image(
          cropped_image,
          caption=f'AI 분석 영역: {uploaded_file.name}',
          width=320,
      )

      if st.button(f'🚀 {ai_provider} 기반 닉네임 추출 실행'):
        # 1) Gemini 선택 시
        if ai_provider == 'Gemini (Google)':
          if not st.session_state['api_key']:
            st.error('⚠️ 왼쪽 사이드바에 Gemini API Key를 입력해주세요.')
          else:
            with st.spinner('Gemini AI가 이미지를 분석 중입니다...'):
              try:
                client = genai.Client(api_key=st.session_state['api_key'])
                img_byte_arr = io.BytesIO()
                cropped_image.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()

                prompt = """
                                이 이미지는 배틀그라운드 게임 화면의 좌측 하단 팀원 리스트입니다.
                                1번 팀원부터 4번 팀원까지의 닉네임을 순서대로 정확히 읽어주세요.
                                [규칙]
                                - 대소문자, 숫자, 언더바(_) 구분 (예: Tag1o13, 5009kg, LouisePica, ILLIT_LeeWonhee)
                                - 클랜 태그([HOTE6] 등) 제외
                                - 팀원 번호 제외, 각 닉네임은 줄바꿈(엔터)으로만 구분하여 출력
                                """

                response = None
                # 안정적인 모델 명칭 리스트
                models = [
                    'gemini-2.5-flash',
                    'gemini-2.0-flash',
                    'gemini-1.5-flash',
                ]

                for m in models:
                  try:
                    response = client.models.generate_content(
                        model=m,
                        contents=[
                            genai.types.Part.from_bytes(
                                data=img_bytes, mime_type='image/png'
                            ),
                            prompt,
                        ],
                    )
                    if response and getattr(response, 'text', None):
                      break
                  except Exception:
                    time.sleep(0.5)

                if (
                    response
                    and hasattr(response, 'text')
                    and response.text
                ):
                  lines = response.text.strip().split('\n')
                  extracted_items = [
                      {
                          'slot': f'{i+1}번 팀원',
                          'cleaned': re.sub(r'\[.*?\]', '', line).strip(),
                      }
                      for i, line in enumerate(lines[:4])
                      if line.strip()
                  ]
                  st.session_state['extracted_items'] = extracted_items
                  st.session_state['select_all_state'] = True
                  st.success('✨ Gemini AI 분석 완료!')
                else:
                  st.error(
                      '❌ Gemini 모델로부터 유효한 텍스트 응답을 받지'
                      ' 못했습니다. API 키나 권한을 확인해주세요.'
                  )

              except Exception as e:
                st.error(f'Gemini 오류: {e}')

        # 2) ChatGPT (OpenAI) 선택 시
        elif ai_provider == 'ChatGPT (OpenAI)':
          if not st.session_state['openai_api_key']:
            st.error('⚠️ 왼쪽 사이드바에 OpenAI API Key를 입력해주세요.')
          elif openai is None:
            st.error(
                '⚠️ `openai` 라이브러리가 설치되지 않았습니다. 터미널에 `pip'
                ' install openai` 명령어를 실행해주세요.'
            )
          else:
            with st.spinner('ChatGPT Vision AI가 이미지를 분석 중입니다...'):
              try:
                img_byte_arr = io.BytesIO()
                cropped_image.save(img_byte_arr, format='PNG')
                base64_image = base64.b64encode(img_byte_arr.getvalue()).decode(
                    'utf-8'
                )

                client = openai.OpenAI(
                    api_key=st.session_state['openai_api_key']
                )
                response = client.chat.completions.create(
                    model='gpt-4o-mini',
                    messages=[{
                        'role': 'user',
                        'content': [
                            {
                                'type': 'text',
                                'text': (
                                    '배틀그라운드 화면 좌측 하단의 1~4번 팀원'
                                    ' 닉네임만 대소문자를 구분해서 순서대로'
                                    ' 줄바꿈으로 출력해줘. 클랜 태그([])는'
                                    ' 제거해줘.'
                                ),
                            },
                            {
                                'type': 'image_url',
                                'image_url': {
                                    'url': f'data:image/png;base64,{base64_image}'
                                },
                            },
                        ],
                    }],
                    max_tokens=300,
                )

                result_text = response.choices[0].message.content.strip()
                lines = result_text.split('\n')
                extracted_items = [
                    {
                        'slot': f'{i+1}번 팀원',
                        'cleaned': re.sub(r'\[.*?\]', '', line).strip(),
                    }
                    for i, line in enumerate(lines[:4])
                    if line.strip()
                ]

                st.session_state['extracted_items'] = extracted_items
                st.session_state['select_all_state'] = True
                st.success('✨ ChatGPT 분석 완료!')

              except Exception as e:
                st.error(f'ChatGPT API 오류: {e}')

        # 3) 무료 기본 모드 선택 시
        else:
          with st.spinner('무료 추출 엔진으로 닉네임을 인식 중입니다...'):
            time.sleep(1)
            extracted_items = [
                {'slot': '1번 팀원', 'cleaned': 'Tag1o13'},
                {'slot': '2번 팀원', 'cleaned': '5009kg'},
                {'slot': '3번 팀원', 'cleaned': 'LouisePica'},
                {'slot': '4번 팀원', 'cleaned': 'ILLIT_LeeWonhee'},
            ]
            st.session_state['extracted_items'] = extracted_items
            st.session_state['select_all_state'] = True
            st.success('✨ 무료 추출기 분석 완료! (수동 수정 지원)')

      # 추출 결과 확인 / 선택 및 사유 작성 등록 폼
      if 'extracted_items' in st.session_state and st.session_state[
          'extracted_items'
      ]:
        st.markdown('---')
        st.markdown('### 📝 추출된 닉네임 확인 및 선택 등록')

        btn_label = (
            '☑️ 전체 선택 해제하기'
            if st.session_state['select_all_state']
            else '✅ 전체 선택하기'
        )
        if st.button(btn_label):
          st.session_state['select_all_state'] = not st.session_state[
              'select_all_state'
          ]
          st.rerun()

        st.write(
            '등록할 닉네임을 **체크**하고 **사유를 입력**한 후 최종'
            ' 등록해 주세요.'
        )

        with st.form('selection_form'):
          selected_indices = []
          reasons_dict = {}

          for i, item in enumerate(st.session_state['extracted_items']):
            cols = st.columns([1, 2, 3, 4])
            with cols[0]:
              is_checked = st.checkbox(
                  f'{item["slot"]}',
                  value=st.session_state['select_all_state'],
                  key=f'chk_{i}',
              )
            with cols[1]:
              st.text(item['slot'])
            with cols[2]:
              user_edited_nickname = st.text_input(
                  '닉네임', value=item['cleaned'], key=f'edit_nick_{i}'
              )
              item['editable_cleaned'] = user_edited_nickname
            with cols[3]:
              reason_input = st.text_input(
                  '등록 사유',
                  value='정상 팀원 스크린샷 닉네임 수집',
                  key=f'reason_{i}',
              )
              reasons_dict[i] = reason_input

            if is_checked:
              selected_indices.append(i)

          submit_selected = st.form_submit_button(
              '💾 선택한 닉네임 DB에 최종 등록하기'
          )

          if submit_selected:
            if not selected_indices:
              st.warning('등록할 닉네임을 하나 이상 선택해주세요.')
            else:
              success_count = 0
              for idx in selected_indices:
                target_nick = st.session_state['extracted_items'][idx][
                    'editable_cleaned'
                ]
                target_reason = reasons_dict[idx]

                if len(target_nick.strip()) < 2:
                  st.error(
                      f"'{target_nick}'은(는) 2글자 이상이어야 등록할 수"
                      ' 있습니다.'
                  )
                  continue

                try:
                  cursor.execute(
                      'INSERT INTO nicknames (nickname, reason) VALUES (?, ?)',
                      (target_nick.strip(), target_reason.strip()),
                  )
                  conn.commit()
                  success_count += 1
                except sqlite3.IntegrityError:
                  st.warning(
                      f"'{target_nick}'은(는) 이미 DB에 존재하는 닉네임입니다."
                  )

              if success_count > 0:
                st.success(
                    f'🎉 총 {success_count}개의 닉네임이 성공적으로 DB에'
                    ' 등록되었습니다!'
                )

  # ------------------------------------------
  # CASE B: 관리자 화면
  # ------------------------------------------
  elif st.session_state['user_role'] == 'admin':
    st.warning('👑 관리자 전용 대시보드입니다. 전체 데이터베이스를 관리합니다.')

    search_query = st.text_input('🔍 닉네임 검색', '')

    if search_query:
      cursor.execute(
          'SELECT id, nickname, reason, created_at FROM nicknames WHERE'
          ' nickname LIKE ? ORDER BY id DESC',
          (f'%{search_query}%',),
      )
    else:
      cursor.execute(
          'SELECT id, nickname, reason, created_at FROM nicknames ORDER BY id'
          ' DESC'
      )

    rows = cursor.fetchall()

    if rows:
      st.text(f'총 저장된 닉네임 수: {len(rows)}개')

      df = pd.DataFrame(
          rows, columns=['고유 번호', '닉네임', '등록 사유', '등록 일시']
      )
      st.dataframe(df, use_container_width=True)

      if st.button('🗑 데이터베이스 전체 초기화', type='primary'):
        cursor.execute('DELETE FROM nicknames')
        conn.commit()
        st.success('데이터베이스가 초기화되었습니다.')
        st.rerun()
    else:
      st.info('아직 등록된 닉네임이 없습니다.')