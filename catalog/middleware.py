"""给公开页面加浏览器缓存头，减少手机重复打开时的等待。

后台、健康检查、PDF 下载不缓存；公开页面的内容不随登录用户变化。
"""


class PublicCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method != 'GET' or response.status_code != 200:
            return response
        path = request.path_info
        if path.startswith('/admin') or path.startswith('/healthz') or path.startswith('/spec/') and path.endswith('/pdf/'):
            return response
        content_type = response.get('Content-Type', '')
        if 'text/html' not in content_type:
            return response
        if not response.has_header('Cache-Control'):
            response['Cache-Control'] = 'public, max-age=120, stale-while-revalidate=300'
        return response
