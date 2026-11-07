from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    """SQLite 生产配置：WAL + busy_timeout + NORMAL。

    SQLite 仍然只有一个写者；WAL 让读不阻塞写、写冲突在 busy_timeout 内排队。
    并发录单超过 3–5 人时建议切 PostgreSQL（见 docs/部署指南.md）。
    """
    if connection.vendor != 'sqlite':
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode=WAL;')
            cursor.execute('PRAGMA busy_timeout=20000;')
            cursor.execute('PRAGMA synchronous=NORMAL;')
    except Exception:
        # 只读库/测试内存库/权限不足时不要让连接失败；业务仍可运行
        pass


class QuoteConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'quote'
    verbose_name = '报价'

    def ready(self):
        connection_created.connect(
            _sqlite_pragmas,
            dispatch_uid='quote.sqlite_pragmas',
            weak=False,
        )
