"""Gunicorn 生产配置：只监听本机，由 Nginx 反代。"""

import os

bind = '127.0.0.1:8011'
workers = int(os.environ.get('GUNICORN_WORKERS', '3'))
threads = 2
worker_class = 'sync'
timeout = 60
graceful_timeout = 30
keepalive = 5

accesslog = '-'
errorlog = '-'
loglevel = 'info'
access_log_format = '%({x-forwarded-for}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" %(D)sus'

# 信任 Nginx 转发的客户端 IP
forwarded_allow_ips = '127.0.0.1'
