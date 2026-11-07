# 法兰标准库 · Flange Standard Library

按标准 / 类型 / 尺寸 / 压力等级查询法兰规格，查看尺寸标注图，打印 A4 规格卡的生产查询站。

**线上：中文 https://std.ax-forge.com/ · English https://std.ax-forge.com/en/**（2026-11-05 从 ax-forge.com/std/ 迁移，旧地址 301；部署记录见 [docs/部署记录.md](docs/部署记录.md)）

- **数据范围**：ASME B16.5-2025、ASME B16.47-2025（Series A/B）、EN 1092-1:2018、JIS B2220:2012，
  共 5 个标准/系列、1,348 条尺寸规格、4 份参考数据（压力-温度、材料组、公差、标记、版本变更）。
- **P1 能力**：参考数据中文化、压力-温度按材料组切换、批量打印（多规格合并 PDF）、
  打印卡二维码、搜索结果筛选、最近查看/热门规格、后台 CSV 导入导出与修改日志。
- **英文版 /en/**：英文界面、英文标注图、英文打印卡/PDF；`/en/` 与中文页 hreflang 互指；
  英文规格页暂用 `noindex, follow`，SEO 主页面仍由 ax-forge.com 现有英文规格页承担；
  提供 `sitemap.xml`、`robots.txt`、canonical、Open Graph 和 JSON-LD。
- **技术栈**：Django 4.2 + SQLite（可切 PostgreSQL）+ 服务端渲染 + 原生 CSS。
- **打印**：A4 纵向单页，浏览器打印 / 保存 PDF；含标注图、尺寸表、压力-温度额定值、来源与免责声明。
- **PDF 下载**：`/spec/<id>/pdf/` 服务端生成，手机点一下即可保存或分享。
- **手机**：自适应布局；打印页有「下载 PDF」（服务端用 Playwright 生成，iPhone 也能直接保存），也可以分享/打印。
- **后台**：Django Admin 中文界面，可逐条维护尺寸规格、标准说明；参考数据只读。

## 本地开发

本地开发只需要 Python 和 Git，默认使用 SQLite，不需要额外的数据库服务或前端构建步骤。以下命令都在仓库根目录执行。

### 环境要求

- Python 3.10+（部署环境为 3.10–3.12）和 pip
- Git
- 可选：Playwright + Chromium，仅本地生成 PDF 时需要

### 1. 克隆仓库、安装依赖

```bash
git clone https://github.com/markqi121/flange-standards.git
cd flange-standards

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

不想每次都写 `./.venv/bin/` 时，可以先 `source .venv/bin/activate`，之后用 `python manage.py ...`。

### 2. 初始化数据库与标准数据

```bash
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py import_standards   # 从 flange-standards-data/ 导入标准数据
./.venv/bin/python manage.py createsuperuser    # 首次创建 /admin/ 后台账号
```

`import_standards` 可以重复执行，按「标准 + 类型 + 尺寸 + 等级」覆盖同名记录；数据包更新后用 `--reset` 清空尺寸和参考数据再全量导入。

### 3. 启动开发服务器

```bash
./.venv/bin/python manage.py runserver 127.0.0.1:8000
```

打开 <http://127.0.0.1:8000/>。默认 `DJANGO_DEBUG=1`，数据库是项目根目录的 `db.sqlite3`（已加入 `.gitignore`）。

启动后可访问的页面：

| 页面 | 地址 |
|---|---|
| 查询首页 | `/` |
| 搜索结果 | `/search/?q=DN100+PN16` |
| 标准尺寸表 | `/asme-b16-5/` |
| 规格卡 | `/spec/asme-b16-5-class-150-nps-6-weld-neck-flange/` |
| A4 打印页 | `/spec/348/print/` |
| PDF 下载（手机可用） | `/spec/348/pdf/` |
| 批量打印 | `/batch/` |
| 跨标准对照 | `/compare/?dn=100` |
| 参考数据（p/T、公差、标记） | `/asme-b16-5/ref/pressure-ratings/` |
| 后台录入 | `/admin/` |
| English home | `/en/` |
| English spec | `/en/spec/asme-b16-5-class-150-nps-6-weld-neck-flange/` |
| 英文 sitemap | `/sitemap.xml` |

截图与样例：`docs/images/`（首页、标准页、规格卡、打印页、对照、参考数据、标注图）；
A4 单页打印样例：`docs/样例-规格卡-A4.pdf`；完整设计说明：`docs/设计方案.md`。

### 4. 常用本地配置

项目不会自动读取 `.env`，需要时在启动前 `export` 环境变量：

| 变量 | 本地默认 | 说明 |
|---|---|---|
| `DJANGO_DEBUG` | `1` | 设为 `0` 模拟生产；同时需设置 `DJANGO_SECRET_KEY` 和 `DJANGO_ALLOWED_HOSTS` |
| `DJANGO_DB_PATH` | `db.sqlite3` | SQLite 数据库文件位置 |
| `DJANGO_DB_ENGINE` | 未设置（SQLite） | 设为 `django.db.backends.postgresql` 并配置 `DJANGO_DB_*` 可切 PostgreSQL |
| `FLANGE_DATA_DIR` | `flange-standards-data/` | 标准数据包目录 |
| `SITE_URL` | `http://127.0.0.1:8000` | 打印页和页面使用的站点地址 |
| `PDF_RENDER_BASE_URL` | `https://127.0.0.1/std` | 本地生成 PDF 时改为 `http://127.0.0.1:8000` |
| `PDF_CACHE_DIR` | `/var/lib/flange-std/pdf` | 本地生成 PDF 时改为可写目录，如 `$PWD/.tmp/pdf` |

要使用「下载 PDF」和批量打印 PDF，先安装 Playwright 的 Chromium（约 700 MB，可选）：

```bash
./.venv/bin/python -m playwright install chromium
export PDF_RENDER_BASE_URL=http://127.0.0.1:8000
export PDF_CACHE_DIR="$PWD/.tmp/pdf"
./.venv/bin/python manage.py runserver 127.0.0.1:8000
```

没有安装 Playwright 时，网页仍可直接用浏览器打印或另存为 PDF。

报价模块的演示数据（可选，**演示价禁止用于正式报价**）：

```bash
./.venv/bin/python manage.py seed_demo    # 材料价、工艺费率、客户 A/B/C
./.venv/bin/python manage.py seed_roles   # sales / engineering / finance / sales_manager / quote_admin 角色组
```

### 5. 检查与测试

```bash
./.venv/bin/python manage.py check
./.venv/bin/python manage.py test quote catalog
```

`manage.py test quote` 运行报价模块全部测试；`manage.py test catalog` 会先导入标准数据，再检查查询、打印页和标注图。

## 目录

```
标准库/
├── catalog/                       Django 应用（中英双语）
│   ├── models.py                  Standard / FlangeType / Rating / DimensionRow / ReferenceDoc
│   ├── views.py                   查询、标准页、规格卡、打印页、对照页
│   ├── search.py                  中文/英文混合查询解析
│   ├── drawings.py                服务端 SVG 尺寸标注图生成器
│   ├── pt.py                      压力-温度表结构化读取
│   ├── zh_content.py              中文文案与字段名
│   ├── en_content.py              英文 UI 文案与字段名
│   ├── i18n_utils.py              /en/ 语言选择
│   ├── admin.py                   后台
│   └── management/commands/import_standards.py
├── stdlib_site/                  Django 配置与路由
├── templates/                    页面模板（templates/en/ 为英文模板）
├── static/css/site.css           屏幕样式
├── static/css/print.css          A4 打印样式
├── flange-standards-data/        标准数据包（尺寸 JSON / CSV / SQLite / 参考数据）
├── docs/                         设计方案、截图、打印样例
└── manage.py
```

## 数据维护

```bash
# 数据包更新后重新导入（幂等，按 标准+类型+尺寸+等级 覆盖）
./.venv/bin/python manage.py import_standards

# 全量重建（清空尺寸/参考数据再导入）
./.venv/bin/python manage.py import_standards --reset

# 单条尺寸修改：登录 /admin/catalog/dimensionrow/ 直接编辑
# 标准说明、中文简介、材料选项：/admin/catalog/standard/
```

后台支持 CSV 导入/导出（尺寸规格列表右上角「导入 CSV」，列表动作「导出所选规格为 CSV」），
所有修改会进入 Django 的修改日志（`/admin/admin/logentry/`），尺寸字段在保存前做范围校验。

数据包本身的校验在 `flange-standards-data/` 内完成：

```bash
cd flange-standards-data && python3 scripts/validate.py
```

## 部署（单机 Nginx + Gunicorn）

完整步骤、服务器配置和检查清单见 **[docs/部署指南.md](docs/部署指南.md)**；
本次实际部署（`std.ax-forge.com` 子域名、服务器、备份、验证结果）见 **[docs/部署记录.md](docs/部署记录.md)**。最简流程：

```bash
# 1) 服务器初始化（Ubuntu 22.04/24.04，root）
ssh root@SERVER_IP 'mkdir -p /srv/flange-std/app'
rsync -az --delete --exclude '.venv/' --exclude 'db.sqlite3' --exclude 'staticfiles/' \
  ./ root@SERVER_IP:/srv/flange-std/app/
ssh root@SERVER_IP 'bash /srv/flange-std/app/deploy/setup-server.sh std.example.com'

# 2) 首次发布（含导入 1,348 条数据）
SSH_KEY=~/.ssh/ax_forge ./deploy/deploy.sh deploy@SERVER_IP --import

# 3) HTTPS
ssh root@SERVER_IP 'certbot --nginx -d std.example.com'
```

| 资源 | 要求 |
|---|---|
| 服务器 | 1 核 1G 起步；和现有站点共用建议 2 核 2G |
| 系统 | Ubuntu 22.04 / 24.04，Python 3.10+ |
| 端口 | 公网 22/80/443；Gunicorn 只听 127.0.0.1:8011 |
| 数据库 | 默认 SQLite（`/var/lib/flange-std/db.sqlite3`），可切 PostgreSQL |
| 内存占用 | Nginx ~20 MB + Gunicorn 3 worker ~300 MB |

部署文件在 [deploy/](deploy/)：`setup-server.sh`、`deploy.sh`、`nginx-flange-std.conf`、
`flange-std.service`、`gunicorn.conf.py`、`env.example`。建议把 `/admin/` 限制在内网或 VPN。

## 数据合规

尺寸与参考数据是公开标准表格的事实性整理，不含标准 PDF、正文段落与图表。页面与打印卡均带来源版本、
核对日期和“以标准原文复核”的声明。正式报价、投标和关键件加工前请以项目指定版本复核。
