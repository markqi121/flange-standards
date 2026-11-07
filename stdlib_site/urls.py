"""法兰标准库 URL 配置。"""

from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

from catalog import views as catalog_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('sitemap.xml', catalog_views.sitemap_xml, name='sitemap_xml'),
    path('robots.txt', catalog_views.robots_txt, name='robots_txt'),
]

urlpatterns += i18n_patterns(
    path('quote/', include('quote.urls')),
    path('', include('catalog.urls')),
    prefix_default_language=False,
)
