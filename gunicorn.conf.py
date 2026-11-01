# Gunicorn 設定（systemd: us-investment-stock.service 用 -c gunicorn.conf.py 啟動）
bind = '127.0.0.1:5999'
workers = 4                 # 4 核心
worker_class = 'gthread'    # 每個 worker 多執行緒，慢的 yfinance 請求不會卡住其他人
threads = 8
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 2000         # 定期換新 worker，避免長時間執行的記憶體累積
max_requests_jitter = 200


# 記憶體上限：請求量少的 worker（例如負責背景監控的那一個）很久都不會達到 max_requests，
# 記憶體會一直累積（2026-11 曾到 2 GB）。每個請求結束後檢查，超過上限就讓它處理完後優雅地換新。
import os
MAX_WORKER_RSS_MB = 700

def post_request(worker, req, environ, resp):
    with open('/proc/self/statm') as f:
        rss_mb = int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE') / 1048576
    if rss_mb > MAX_WORKER_RSS_MB:
        worker.log.info('worker %s 使用 %.0f MB，超過 %s MB，處理完後重新啟動', worker.pid, rss_mb, MAX_WORKER_RSS_MB)
        worker.alive = False
