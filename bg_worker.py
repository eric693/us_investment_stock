"""背景工作專用程序（systemd: us-investment-stock-bg）。

網頁 gunicorn worker 設 TW_BOARD_NO_BG=1 只處理請求；監控掃描、台股同步／即時輪詢、選股、
TDCC、各種警示推播都在這個程序跑。各模組原本就用檔案鎖與 JSON／SQLite 檔在程序之間共享結果，
所以網頁端讀到的資料不變；差別是背景工作膨脹不會再害網頁 worker 一直被回收重啟。
"""
import os
import time

os.environ.pop('TW_BOARD_NO_BG', None)
import app  # noqa: F401  匯入時各模組 init() 會啟動背景執行緒

print('[bg_worker] 背景工作已啟動', flush=True)
while True:
    time.sleep(3600)
