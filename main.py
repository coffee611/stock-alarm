import datetime
import os
import FinanceDataReader as fdr
from pykrx import stock
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


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [박스권 18% + 수급 및 거래량 스크리닝] 실행: {now_str}")
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
  start_date_dt = end_date_dt - datetime.timedelta(days=35)
  start_str = start_date_dt.strftime("%Y%m%d")
  end_str = end_date_dt.strftime("%Y%m%d")

  # pykrx 로그인 에러 원천 차단을 위한 개별 종목 순매수 조회 방식 적용 (안전망)
  inst_net_dict = {}
  print(
      "기관 순매수 데이터 수집 중 (pykrx 개별 조회 및 예외 방어 적용)..."
  )
  for ticker in tickers[:50]:  # 부하 및 차단 방지를 위해 상위 50개 우선 수집
    try:
      df_net = stock.get_market_net_purchases_of_equities_by_ticker(
          start_str, end_str, ticker
      )
      if not df_net.empty and "기관합계" in df_net.columns:
        inst_net_dict[ticker] = df_net["기관합계"].sum()
    except Exception:
      pass

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
      if len(df) < 20:
        continue

      df_recent = df.tail(20)
      latest_price = df_recent["Close"].iloc[-1]

      stock_row = df_krx[df_krx["Code"] == ticker]
      stock_name = (
          stock_row["Name"].values[0] if not stock_row.empty else ticker
      )

      # 1. 공통 전제 조건: 20거래일 박스권 범위 18% 이내
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.18

      if not is_box:
        continue

      # 2. 거래량 우선 적용, 미흡/없을 시 거래대금 적용
      if "Volume" in df_recent.columns and df_recent["Volume"].sum() > 0:
        target_series = df_recent["Volume"]
      elif "Amount" in df_recent.columns:
        target_series = df_recent["Amount"]
      else:
        continue

      inst_net_buy = inst_net_dict.get(ticker, 0)
      inst_str = f"기관 누적 순매수: {inst_net_buy:,}주"

      # --- [조건 1] 박스권(18%) + 후반부 10일 평균 거래량이 전반부 10일 평균보다 20% 이상 증가 ---
      first_half = target_series.head(10).mean()
      second_half = target_series.tail(10).mean()
      growth_ratio = (
          (second_half - first_half) / first_half if first_half > 0 else 0
      )
      if growth_ratio >= 0.20:
        cond1_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 변동폭: {round(price_range_ratio*100, 1)}%\n  - 후반부 증가율:"
            f" +{round(growth_ratio*100, 1)}%\n  - 📊 {inst_str}"
        )

      # --- [조건 2] 박스권(18%) + 20거래일 중 전일 대비 증가한 날이 12일 이상 ---
      vol_diff = target_series.diff() > 0
      inc_days_count = vol_diff.sum()
      if inc_days_count >= 12:
        cond2_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 변동폭: {round(price_range_ratio*100, 1)}%\n  - 거래량 증가일수:"
            f" {inc_days_count}/20일\n  - 📊 {inst_str}"
        )

      # --- [조건 3] 박스권(18%) + 20일 평균 거래량 대비 2배 이상 터진 날이 1회 이상 존재 ---
      avg_val = target_series.mean()
      max_val = target_series.max()
      is_spike = avg_val > 0 and (max_val >= avg_val * 2.0)
      if is_spike:
        cond3_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 변동폭: {round(price_range_ratio*100, 1)}%\n  - 최대 거래량 폭발:"
            f" 평균 대비 {round(max_val / avg_val, 1)}배\n  - 📊 {inst_str}"
        )

    except Exception:
      pass

  # 조건별 메시지 독립 전송
  msg_intro = f"📊 *[박스권 18% + 수급/거래량 스크리닝 리포트]* ({now_str})\n💡 수급 출처: `pykrx` (기관 순매수)\n💡 지표 우선순위: 거래량 우선 (미흡 시 거래대금)"
  send_telegram_message(msg_intro)

  msg_cond1 = (
      f"📦 *[조건 1] 박스권(18%) + 후반부 10일 평균 거래량 20% 이상 증가*\n\n"
      + (
          "\n\n".join(cond1_results)
          if cond1_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )
  send_telegram_message(msg_cond1)

  msg_cond2 = (
      f"📈 *[조건 2] 박스권(18%) + 거래량 증가일 12일 이상*\n\n"
      + (
          "\n\n".join(cond2_results)
          if cond2_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )
  send_telegram_message(msg_cond2)

  msg_cond3 = (
      f"⚡ *[조건 3] 박스권(18%) + 평균 거래량 2배 이상 폭발 (1회 이상)*\n\n"
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
