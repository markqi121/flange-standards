"""法兰标准库路由。"""

from django.urls import path

from . import views

app_name = 'catalog'

urlpatterns = [
    path('', views.home, name='home'),
    path('search/', views.search, name='search'),
    path('spec/<int:pk>/', views.spec_detail_legacy, name='spec_detail_legacy'),
    path('spec/<slug:slug>/', views.spec_detail, name='spec_detail'),
    path('spec/<int:pk>/print/', views.spec_print, name='spec_print'),
    path('spec/<int:pk>/pdf/', views.spec_pdf, name='spec_pdf'),
    path('compare/', views.compare, name='compare'),
    path('batch/', views.batch, name='batch'),
    path('batch/print/', views.batch_print, name='batch_print'),
    path('batch/pdf/', views.batch_pdf, name='batch_pdf'),
    path('healthz', views.healthz, name='healthz'),
    path('healthz/', views.healthz),
    # 通用标准路由必须放在最后，避免吞掉 /compare/、/spec/ 等
    path('<slug:std_id>/ref/<slug:topic_key>/', views.reference_topic, name='reference_topic'),
    path('<slug:std_id>/', views.standard_detail, name='standard_detail'),
]
