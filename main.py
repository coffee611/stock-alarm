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
  print(
      f" [20일 박스권 + 거래량 40%↑ + 꾸준함(10일+) 스크리닝 Bot] 실행 시각:"
      f" {now_str}"
  )
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
      start_date = end_date - datetime.timedelta(days=45)
      df = fdr.DataReader(ticker, start_date, end_date)

      if len(df) < 20:
        continue

      # 최근 20거래일 데이터 추출
      df_recent = df.tail(20)

      # 1. 박스권 조건 (20거래일 기준 최고가-최저가 변동폭 15% 이내)
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.15

      # 거래대금 우선 사용, 없으면 거래량 사용
      if "Amount" in df_recent.columns:
        target_series = df_recent["Amount"]
      else:
        target_series = df_recent["Volume"]

      # 2. 전반부(앞 10일) vs 후반부(뒤 10일) 평균 거래량 비교 조건 (40% 이상 증가)
      first_half = target_series.head(10).mean()
      second_half = target_series.tail(10).mean()

      if first_half > 0:
        vol_growth_ratio = (second_half - first_half) / first_half
      else:
        vol_growth_ratio = 0

      is_volume_expanded = vol_growth_ratio >= 0.40

      # 3. 꾸준한 수급 조건 (20거래일 중 10일 이상 전일 대비 증가) - 수정됨
      vol_diff = target_series.diff() > 0
      inc_days_count = vol_diff.sum()
      is_steady_accumulating = inc_days_count >= 10

      # 4. 거래량 급증 조건 (20일 평균 대비 2.5배 이상 폭발 1회 이상)
      avg_vol = target_series.mean()
      max_vol = target_series.max()
      is_spike = avg_vol > 0 and (max_vol >= avg_vol * 2.5)

      # 최종 필터링: 박스권 + [후반부 거래량 40% 이상 증가] + (꾸준한 수급 10일+ OR 거래량 급증)
      if is_box and is_volume_expanded and (is_steady_accumulating or is_spike):
        latest_price = df_recent["Close"].iloc[-1]
        stock_row = df_krx[df_krx["Code"] == ticker]
        stock_name = (
            stock_row["Name"].values[0] if not stock_row.empty else ticker
        )

        pattern_type = (
            "🔥 거래량 폭발 + 후반부 집중"
            if is_spike
            else "📈 꾸준한 수급 유입 (10일+)"
        )

        results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 유형: {pattern_type}\n -"
            f" 현재가: {latest_price:,}원\n  - 20일 변동폭:"
            f" {round(price_range_ratio*100, 1)}%\n  - 거래량 증가일:"
            f" {inc_days_count}/20일 (후반부 성장이율:"
            f" +{round(vol_growth_ratio*100, 1)}%)"
        )
    except Exception:
      pass

  # 결과 처리 및 텔레그램 전송
  if results:
    msg = (
        f"🚨 *[핵심 수급 집중 포착 알림]* ({now_str})\n\n"
        + "\n".join(results)
        + "\n\n시총 상위 200개 중 20일 박스권 + 후반부 거래량 40% 이상 증가 +"
        " 꾸준한 수급(10일 이상) 조건을 만족하는 종목입니다."
    )
    print("\n" + "\n".join(results))
  else:
    msg = (
        f"ℹ️ *[주식 스크리닝 결과]* ({now_str})\n\n조건을 만족하는 종목이"
        " 없습니다."
    )
    print("조건을 만족하는 종목이 없습니다.")

  # 스마트폰 텔레그램으로 전송
  send_telegram_message(msg)
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
