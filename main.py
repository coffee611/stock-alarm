import datetime
import os
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


def get_naver_institutional_amount_5days(ticker, recent_df):
  """네이버 금융에서 최근 5거래일 기관 순매수 금액(억원) 추정 및 분석"""
  try:
    url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    res = requests.get(url, headers=headers, timeout=5)
    if res.status_code == 200:
      dfs = pd.read_html(res.text)
      for df in dfs:
        cols = [str(c) for c in df.columns]
        inst_col_candidates = [c for c in cols if "기관" in c]
        if inst_col_candidates:
          inst_col = inst_col_candidates[0]
          df = df.dropna(subset=[df.columns[0]])
          if len(df) < 5:
            continue

          amounts = []
          for i in range(5):
            row_val = df.iloc[i][inst_col]
            close_price = recent_df["Close"].iloc[-(5 - i)]
            try:
              val_str = str(row_val).replace(",", "").strip()
              shares = int(val_str[1:]) if val_str.startswith("+") else int(val_str)
              amount_억 = (shares * close_price) / 100_000_000
              amounts.append(amount_억)
            except:
              amounts.append(0.0)

          if not amounts:
            return 0.0, 0.0, 0, []

          today_amount = amounts[0]
          positive_days = sum(1 for a in amounts if a > 0)

          return today_amount, sum(amounts), positive_days, amounts
  except Exception:
    pass
  return 0.0, 0.0, 0, []


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [단기 5일 박스권(20%) + 큰형님 수급 스크리닝] 실행: {now_str}")
  print("==================================================")

  try:
    df_krx = fdr.StockListing("KRX")
    if "MarCap" in df_krx.columns:
      df_krx = df_krx.sort_values(by="MarCap", ascending=False)
      target_stocks = df_krx.head(200)
      tickers = target_stocks["Code"].dropna().tolist()
      print("시가총액 상위 200개 종목 대상 스캔 시작")
    else:
      tickers = df_krx["Code"].dropna().tolist()[:200]
  except Exception as e:
    print(f"종목 리스트 불러오기 오류: {e}")
    tickers = ["005930", "000660", "035420", "035720", "051910"]

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=20)

  cond1_results = []
  cond2_results = []
  cond3_results = []

  for ticker in tickers:
    try:
      df = fdr.DataReader(
          ticker,
          start_date_dt.strftime("%Y-%m-%d"),
          end_date_dt.strftime("%Y-%m-%d"),
      )
      if len(df) < 5:
        continue

      df_recent = df.tail(5)
      latest_price = df_recent["Close"].iloc[-1]

      stock_row = df_krx[df_krx["Code"] == ticker]
      stock_name = (
          stock_row["Name"].values[0] if not stock_row.empty else ticker
      )

      # 1. 공통 전제 조건: 최근 5거래일 박스권 범위 20% 이내 (수정됨)
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      if low_min <= 0:
        continue

      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.20

      if not is_box:
        continue

      today_amt, total_amt_5d, positive_days, daily_amounts = (
          get_naver_institutional_amount_5days(ticker, df_recent)
      )
      if not daily_amounts or len(daily_amounts) < 5:
        continue

      # 5일 데이터 분할 (전반 2일 vs 후반 3일)
      first_half_avg = sum(daily_amounts[:2]) / 2.0
      second_half_avg = sum(daily_amounts[2:]) / 3.0
      growth_ratio = (
          (second_half_avg - first_half_avg) / abs(first_half_avg)
          if first_half_avg != 0
          else 0
      )

      mean_amt = sum(daily_amounts) / 5.0
      spike_count = sum(
          1 for a in daily_amounts if mean_amt > 0 and a >= mean_amt * 2.0
      )

      # 사용자 요청 반영된 완화된 조건 매칭 검증
      is_cond1 = growth_ratio >= 0.20  # 조건 1: 후반부 20% 이상 증가 (수정됨)
      is_cond2 = positive_days >= 3  # 조건 2: 5일 중 3일 이상 순매수 유입 (수정됨)
      is_cond3 = spike_count >= 2  # 조건 3: 평균 대비 2배 폭발 2회 이상 (수정됨)

      if is_cond1 or is_cond2 or is_cond3:
        today_mark = (
            "🔥 당일 순매수 유입" if today_amt > 0 else "💤 당일 관망/매도"
        )
        inst_str = (
            f"기관 순매수 금액: 5일 누적 +{round(total_amt_5d, 1)}억"
            f" ({positive_days}/5일 유입)\n  - 당일 추정: +{round(today_amt, 1)}억"
            f" [{today_mark}]"
        )

        # --- [조건 1] 5일 박스권(20%) + 후반부 순매수 금액 20% 이상 증가 ---
        if is_cond1:
          cond1_results.append(
              f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n"
              f"  - 5일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 후반부 금액"
              f" 증가율: +{round(growth_ratio*100, 1)}%\n  - 💰 {inst_str}"
          )

        # --- [조건 2] 5일 박스권(20%) + 3일 이상 순매수 유입 ---
        if is_cond2:
          cond2_results.append(
              f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n"
              f"  - 5일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 순매수 유입"
              f" 일수: {positive_days}/5일\n  - 💰 {inst_str}"
          )

        # --- [조건 3] 5일 박스권(20%) + 평균 순매수 금액 2배 폭발 2회 이상 ---
        if is_cond3:
          cond3_results.append(
              f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n"
              f"  - 5일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 2배 폭발 횟수:"
              f" {spike_count}회\n  - 💰 {inst_str}"
          )

    except Exception:
      pass

  # 조건별 메시지 독립 전송
  msg_intro = f"📊 *[단기 5일 박스권(20%) + 큰형님 순매수 금액 리포트]* ({now_str})\n💡 단기 집중 전략: 최근 5거래일 기관 자금 집중도 및 폭발 강도 분석"
  send_telegram_message(msg_intro)

  msg_cond1 = (
      f"📦 *[조건 1] 5일 박스권(20%) + 후반부 순매수 금액 20% 이상 증가*\n\n"
      + (
          "\n\n".join(cond1_results)
          if cond1_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )
  send_telegram_message(msg_cond1)

  msg_cond2 = (
      f"📈 *[조건 2] 5일 박스권(20%) + 3일 이상 순매수 유입*\n\n"
      + (
          "\n\n".join(cond2_results)
          if cond2_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )
  send_telegram_message(msg_cond2)

  msg_cond3 = (
      f"⚡ *[조건 3] 5일 박스권(20%) + 평균 순매수 금액 2배 폭발 2회 이상*\n\n"
      + (
          "\n\n".join(cond3_results)
          if cond3_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )
  send_telegram_message(msg_cond3)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
