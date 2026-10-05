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
