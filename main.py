import datetime
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import FinanceDataReader as fdr
import pandas as pd
import requests

# 깃허브 시크릿에서 텔레그램 토큰 및 챗 ID 가져오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
  """텔레그램 메시지 전송 함수 (글자 수 제한 방지를 위해 분할 전송)"""
  if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("[경고] 텔레그램 토큰 또는 챗 ID가 설정되지 않았습니다.")
    return

  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

  if len(message) > 4000:
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
      payload = {
          "chat_id": TELEGRAM_CHAT_ID,
          "text": chunk,
          "parse_mode": "Markdown",
      }
      requests.post(url, data=payload, timeout=10)
      time.sleep(0.5)
    print("텔레그램 분할 알림 전송 성공!")
  else:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    response = requests.post(url, data=payload, timeout=10)
    if response.status_code == 200:
      print("텔레그램 알림 전송 성공!")
    else:
      print(f"텔레그램 알림 전송 실패: {response.text}")


def get_target_stocks():
  """코스피, 코스닥 각 시총 상위 400개 추출 (ETF, ETN, 스팩, 리츠 등 제외 / 우선주 허용)"""
  exclude_keywords = [
      "ETF",
      "ETN",
      "스팩",
      "SPAC",
      "리츠",
      "KODEX",
      "TIGER",
      "ARIRANG",
      "KBSTAR",
      "SOL",
      "HANARO",
      "KOSEF",
      "PLUS",
      "TIMEFOLIO",
      "RISE",
      "WOORI",
      "BNK",
      "히어로즈",
  ]
  target_dfs = []

  for market in ["KOSPI", "KOSDAQ"]:
    try:
      df = fdr.StockListing(market)
      if "Name" in df.columns:
        for kw in exclude_keywords:
          df = df[~df["Name"].str.contains(kw, na=False)]
      if "MarCap" in df.columns:
        df = df.sort_values(by="MarCap", ascending=False)
      target_dfs.append(df.head(400))
    except Exception as e:
      print(f"{market} 종목 리스트 로드 오류: {e}")

  if target_dfs:
    combined_df = pd.concat(target_dfs, ignore_index=True)
    return combined_df
  return pd.DataFrame()


def get_naver_institutional_detail(ticker, recent_df, marcap):
  """네이버 금융 HTML 테이블 파싱 오류 방지 및 정밀 수급 추출"""
  url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  for attempt in range(3):
    try:
      res = requests.get(url, headers=headers, timeout=5)
      if res.status_code == 200:
        # 멀티인덱스 헤더 대응을 위해 헤더 두 줄 파싱 시도
        dfs = pd.read_html(res.text, header=[0, 1])
        if not dfs:
          dfs = pd.read_html(res.text)

        for df in dfs:
          # 컬럼명을 문자열로 평탄화
          if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                "_".join([str(c) for c in col if "Unnamed" not in str(c)])
                for col in df.columns
            ]
          else:
            df.columns = [str(c) for c in df.columns]

          # '기관' 관련 컬럼 찾기
          inst_col = None
          for col in df.columns:
            if "기관" in col and (
                "순매매량" in col or "순매수" in col or "거래량" in col
            ):
              inst_col = col
              break
          if not inst_col:
            for col in df.columns:
              if "기관" in col:
                inst_col = col
                break

          if inst_col:
            df = df.dropna(subset=[df.columns[0]])
            if len(df) < 5:
              continue

            amounts = []
            for i in range(5):
              try:
                row_val = df.iloc[i][inst_col]
                close_price = recent_df["Close"].iloc[-(5 - i)]
                val_str = str(row_val).replace(",", "").strip()
                if val_str == "" or val_str == "nan":
                  amounts.append(0.0)
                  continue
                shares = (
                    int(val_str[1:])
                    if val_str.startswith("+")
                    else int(val_str)
                )
                amount_억 = (shares * close_price) / 100_000_000
                amounts.append(amount_억)
              except:
                amounts.append(0.0)

            if len(amounts) == 5:
              today_amt = amounts[0]
              total_5d = sum(amounts)

              # 연속 매수/매도 계산
              streak_count = 0
              is_buying = amounts[0] > 0
              for a in amounts:
                if (is_buying and a > 0) or (not is_buying and a < 0):
                  streak_count += 1
                else:
                  break
              streak_str = (
                  f"{streak_count}일 매수"
                  if is_buying
                  else f"{streak_count}일 매도"
              )

              marcap_억 = marcap / 100_000_000 if marcap > 0 else 1
              ratio_pct = round((total_5d / marcap_억) * 100, 2)

              return {
                  "today_amt": round(today_amt, 1),
                  "total_5d": round(total_5d, 1),
                  "ratio_pct": ratio_pct,
                  "streak": streak_str,
              }
      time.sleep(0.2)
    except Exception:
      time.sleep(0.3)
  return None


def process_single_stock(row, start_date_str, end_date_str):
  """개별 종목 분석"""
  ticker = row["Code"]
  stock_name = row["Name"]
  marcap = row.get("MarCap", 0)

  try:
    df = fdr.DataReader(ticker, start_date_str, end_date_str)
    if len(df) < 10:
      return None, None

    df_recent = df.tail(10)
    df_5d = df.tail(5)
    latest_price = df_5d["Close"].iloc[-1]
    prev_price = df_5d["Close"].iloc[0]
    change_rate = round(((latest_price - prev_price) / prev_price) * 100, 1)

    high_max = df_recent["High"].max()
    low_min = df_recent["Low"].min()
    if low_min <= 0:
      return None, None

    price_range_ratio = (high_max - low_min) / low_min
    if price_range_ratio > 0.23:
      return None, None

    naver_data = get_naver_institutional_detail(ticker, df_5d, marcap)
    if not naver_data:
      return None, None

    total_10d = naver_data["total_5d"] * 2
    is_cond1 = True
    is_cond2 = naver_data["total_5d"] > 0

    screen_result = None
    if is_cond1 or is_cond2:
      screen_result = {
          "name": stock_name,
          "total_amt_10d": total_10d,
          "change_rate": change_rate,
          "tags": "박스권수급",
      }

    top40_data = {
        "ticker": ticker,
        "name": stock_name,
        "price": latest_price,
        "today_amt": naver_data["today_amt"],
        "total_5d": naver_data["total_5d"],
        "ratio_pct": naver_data["ratio_pct"],
        "streak": naver_data["streak"],
        "change_rate": change_rate,
    }

    return screen_result, top40_data

  except Exception:
    pass

  return None, None


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [병렬 스캔] 분할 리포트 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=30)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  screened_results = []
  all_top40_data = []

  with ThreadPoolExecutor(max_workers=8) as executor:
    futures = [
        executor.submit(
            process_single_stock, row, start_date_str, end_date_str
        )
        for _, row in df_krx.iterrows()
    ]
    for future in as_completed(futures):
      res = future.result()
      if res is not None:
        screen_res, top40_res = res
        if screen_res:
          screened_results.append(screen_res)
        if top40_res:
          all_top40_data.append(top40_res)

  # -------------------------------------------------------------------------
  # [메시지 1] 조건 스크리닝 결과 (별도 전송)
  # -------------------------------------------------------------------------
  screened_results = sorted(
      screened_results, key=lambda x: x["total_amt_10d"], reverse=True
  )

  msg_screen = (
      f"📊 *[10일 박스권 수급 스크리닝 리포트]*\n⏱️ `{now_str}`\n\n"
  )
  if not screened_results:
    msg_screen += "❌ 조건을 만족하는 종목이 없습니다."
    send_telegram_message(msg_screen)
  else:
    msg_screen += "`종목명` | `누적금액` | `등락률` | `부합조건`\n"
    msg_screen += "---|---|---|---\n"
    for item in screened_results[:25]:
      msg_screen += f"**{item['name']}** | +{item['total_amt_10d']}억 | {item['change_rate']}% | `{item['tags']}`\n"
    send_telegram_message(msg_screen)

  # -------------------------------------------------------------------------
  # [메시지 2 & 3] 기관 5일 순매수 금액 TOP 40 리포트 (별도 전송, 20개씩 분할)
  # -------------------------------------------------------------------------
  all_top40_data = sorted(
      all_top40_data, key=lambda x: x["total_amt_5d"], reverse=True
  )
  top_40_list = all_top40_data[:40]

  msg_top40_intro = (
      f"🔥 *[기관 5일 순매수 금액 TOP 40 리포트]*\n⏱️ `{now_str}` (네이버 금융"
      " 기준)\n\n"
  )
  send_telegram_message(msg_top40_intro)

  for chunk_idx, sub_list in enumerate(
      [top_40_list[:20], top_40_list[20:40]], 1
  ):
    if not sub_list:
      continue
    table_msg = (
        f"📋 *TOP 40 순위 ({1 if chunk_idx==1 else 21} ~"
        f" {20 if chunk_idx==1 else 40}위)*\n"
    )
    table_msg += "`#` | `종목명` | `당일(억)` | `5일(억)` | `시총비율` | `연속` | `등락`\n"
    table_msg += "---|---|---|---|---|---|---\n"
    for idx, item in enumerate(sub_list, 1):
      real_idx = idx if chunk_idx == 1 else idx + 20
      today_mark = (
          f"+{item['today_amt']}" if item["today_amt"] > 0 else f"{item['today_amt']}"
      )
      table_msg += f"{real_idx} | **{item['name']}** | {today_mark} | +{item['total_amt_5d']} | {item['ratio_pct']}% | {item['streak']} | {item['change_rate']}%\n"
    send_telegram_message(table_msg)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
