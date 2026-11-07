#!/usr/bin/env bash
# 报价功能 · 一键验收（QA / T4）
#
#   scripts/acceptance/run_acceptance.sh
#
# 顺序执行并把每个步骤的原始输出保存到 scripts/acceptance/artifacts/<时间戳>/：
#   1. manage.py check
#   2. manage.py test quote（含所有开发者单测 + tests_acceptance）
#   3. tests_acceptance 单独跑（A–J 验收）
#   4. 安全配置（security_check.sh）
#   5. 性能/并发冒烟（perf_smoke.py，对开发库临时副本）
#   6. 备份/恢复演练（backup_restore_drill.py）
#   7. PDF/Playwright 环境探测
#
# 退出码：0 = 全部步骤通过；1 = 有失败步骤（详情见 summary.txt）。
set -uo pipefail
cd "$(dirname "$0")/../.."

PY=".venv/bin/python"
STAMP="$(date +%Y%m%d-%H%M%S)"
ART="scripts/acceptance/artifacts/${STAMP}"
mkdir -p "$ART"
SUMMARY="$ART/summary.txt"
: > "$SUMMARY"
FAILED=0

log_step() {
  local name="$1"
  shift
  echo "===== ${name} ====="
  if "$@" >"$ART/${name}.log" 2>&1; then
    echo "[PASS] ${name}" | tee -a "$SUMMARY"
  else
    echo "[FAIL] ${name}（见 ${ART}/${name}.log）" | tee -a "$SUMMARY"
    FAILED=$((FAILED + 1))
  fi
}

{
  echo "报价功能验收 · ${STAMP}"
  echo "workspace: $(pwd)"
  echo "python: $($PY -V 2>&1)"
  echo
} | tee -a "$SUMMARY"

log_step "01_django_check" "$PY" manage.py check
log_step "02_test_quote" "$PY" manage.py test quote -v 2
log_step "03_test_acceptance" "$PY" manage.py test quote.tests_acceptance -v 2
log_step "04_security" bash scripts/acceptance/security_check.sh
log_step "05_perf" "$PY" scripts/acceptance/perf_smoke.py --json
# 备份产物放临时目录，避免把开发库副本留在仓库里；
# 用仓库里的真实 A4 PDF 样本验证 PDF 缓存备份（生产 Playwright 未就绪时也覆盖该流程）。
BACKUP_DIR="${TMPDIR:-/tmp}/quote-acceptance-$STAMP"
PDF_SAMPLE_DIR="${TMPDIR:-/tmp}/quote-pdf-sample-$STAMP"
mkdir -p "$PDF_SAMPLE_DIR"
if [ -f "docs/样例-规格卡-A4.pdf" ]; then
  cp "docs/样例-规格卡-A4.pdf" "$PDF_SAMPLE_DIR/fq-sample-1.pdf"
  cp "docs/样例-规格卡-A4.pdf" "$PDF_SAMPLE_DIR/fq-sample-2.pdf"
fi
log_step "06_backup_restore" "$PY" scripts/acceptance/backup_restore_drill.py \
  --source db.sqlite3 --backup-dir "$BACKUP_DIR" --pdf-dir "$PDF_SAMPLE_DIR" --json
"$PY" scripts/acceptance/playwright_probe.py --json >"$ART/07_playwright.json" 2>&1
PLAYWRIGHT_RC=$?
if [ $PLAYWRIGHT_RC -eq 0 ]; then
  echo "[PASS] 07_playwright（PDF 环境可用）" | tee -a "$SUMMARY"
elif [ $PLAYWRIGHT_RC -eq 2 ]; then
  echo "[ENV]  07_playwright：Playwright/Chromium 未就绪（环境阻塞，见报告）" | tee -a "$SUMMARY"
else
  echo "[FAIL] 07_playwright（见 ${ART}/07_playwright.json）" | tee -a "$SUMMARY"
  FAILED=$((FAILED + 1))
fi

{
  echo
  echo "测试结果摘要："
  for log in "$ART"/02_test_quote.log "$ART"/03_test_acceptance.log; do
    [ -f "$log" ] || continue
    echo "  $(basename "$log"): $(grep -E '^(OK|FAILED|Ran )' "$log" | tr '\n' ' ')"
  done
  echo
  echo "产物：$ART"
} | tee -a "$SUMMARY"

echo
if [ $FAILED -eq 0 ]; then
  echo "验收执行完成：全部步骤 PASS"
else
  echo "验收执行完成：${FAILED} 个步骤 FAIL（不伪造通过，详见 summary.txt 与报告）"
fi
exit $FAILED
