"""法兰报价路由（T1 API 已挂载，Lead 集成版）。

顺序约定：
- 静态路径 options/calc/create/list/prices 必须在 <str:number>/ 之前；
- /quote/ 的 GET 是工作台，创建走 /quote/create/（契约 §6 2026-11-06 更新）；
- <number>/status/ 由 T1 quote/api.py 提供。
"""

from django.urls import path

from . import api, views

app_name = 'quote'

urlpatterns = [
    path('', views.workbench, name='index'),
    path('list/', views.quote_list, name='list'),
    path('prices/', views.prices, name='prices'),
    path('customers/', views.customers, name='customers'),
    path('options/', api.api_options, name='api_options'),
    path('calc/', api.api_calc, name='api_calc'),
    path('create/', api.api_create, name='api_create'),
    path('export/', views.quote_export, name='export'),
    path('<str:number>/status/', api.api_status, name='api_status'),
    path('<str:number>/excel/', views.quote_excel, name='excel'),
    path('<str:number>/', views.quote_detail, name='detail'),
    path('<str:number>/print/', views.quote_print, name='print'),
    path('<str:number>/pdf/', views.quote_pdf, name='pdf'),
]
