import datetime
import os
import FinanceDataReader as fdr
import requests

# 깃허브 시크릿에서 텔레그램 토큰 및 챗 ID 가져오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
  """텔레그램 메시지 전송 함수"""
  if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("[경고] 텔레그램 토큰 또는 챗 ID가 설정되지 않았습니다.")
    return

  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }

  try:
    response = requests.post(url, data=payload, timeout=10)
    if response.status_code == 200:
      print("텔레그램 알림 전송 성공!")
    else:
      print(f"텔레그램 알림 전송 실패: {response.text}")
  except Exception as e:
    print(f"텔레그램 전송 중 오류 발생: {e}")


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [기관 수급 + 박스권 스크리닝 Bot] 실행 시각: {now_str}")
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

  results = []

  for ticker in tickers:
    try:
      end_date = datetime.datetime.now()
      start_date = end_date - datetime.timedelta(days=25)
      df = fdr.DataReader(ticker, start_date, end_date)

      if len(df) < 10:
        continue

      df_recent = df.tail(10)
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.15

      vol_first = df_recent["Volume"].iloc[:5].mean()
      vol_second = df_recent["Volume"].iloc[5:].mean()
      is_vol_inc = vol_second > vol_first

      if is_box and is_vol_inc:
        latest_price = df_recent["Close"].iloc[-1]
        stock_row = df_krx[df_krx["Code"] == ticker]
        stock_name = (
            stock_row["Name"].values[0] if not stock_row.empty else ticker
        )

        results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n  -"
            f" 변동폭: {round(price_range_ratio*100, 1)}%"
        )
    except Exception:
      pass

  # 결과 처리 및 텔레그램 전송
  if results:
    msg = (
        f"🚨 *[주식 포착 알림]* ({now_str})\n\n"
        + "\n".join(results)
        + "\n\n시총 상위 200개 중 박스권+수급 조건을 만족하는 종목입니다."
    )
    print("\n" + "\n".join(results))
  else:
    msg = (
        f"ℹ️ *[주식 스크리닝 결과]* ({now_str})\n\n오늘 조건을 만족하는"
        " 종목이 없습니다."
    )
    print("오늘 조건을 만족하는 종목이 없습니다.")

  # 스마트폰 텔레그램으로 전송
  send_telegram_message(msg)
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
