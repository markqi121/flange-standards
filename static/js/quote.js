/* ==========================================================================
   法兰报价 · 工作台交互（T2）
   - 级联：标准 → 类型 → 压力等级 → 口径（数据来自标准库，服务端注入）
   - 计算：POST /quote/calc/（契约 §3.2）；未就绪时回退到《接口契约》§9 示例夹具
   - 清单 / A4 预览 / 打印 / CSV 全部在浏览器完成
   注意：本文件不实现成本、重量、定价算法；所有金额均来自 /quote/calc/ 或静态夹具。
   ========================================================================== */
(function () {
  'use strict';

  var bootEl = document.getElementById('quote-bootstrap');
  if (!bootEl || !document.getElementById('stdSel')) { return; }

  var DATA;
  try {
    DATA = JSON.parse(bootEl.textContent);
  } catch (err) {
    if (window.console) { console.error('[quote] bootstrap JSON 解析失败', err); }
    return;
  }

  /* 行数据结构（与服务端 quote/views.py 的 row_payload 顺序一致） */
  var R = {
    std: 0, type: 1, size: 2, label: 3, dn: 4, rating: 5,
    od: 6, th: 7, rf: 8, rfh: 9, bc: 10, bolts: 11, hole: 12,
    hub: 13, len: 14, hubw: 15, weight: 16, id: 17
  };

  var MATERIAL = {};
  (DATA.materials || []).forEach(function (m) { MATERIAL[m.code] = m; });
  var TYPE = {};
  (DATA.types || []).forEach(function (t) { TYPE[t.code] = t; });
  var STANDARD = {};
  (DATA.standards || []).forEach(function (s) { STANDARD[s.id] = s; });

  var surfaceOptions = { none: '不做处理', rust: '防锈油', zinc: '热镀锌', paint: '喷涂' };
  var packOptions = { carton: '纸箱', wood: '木箱', fumigated: '熏蒸木箱', none: '不包装' };
  var inspOptions = [
    ['pmi', 'PMI 光谱'], ['ut', 'UT 探伤'], ['hydro', '水压试验'],
    ['hardness', '硬度检测'], ['third', '第三方见证']
  ];
  var DEFAULT_PARAMS = DATA.params || {};

  var state = {
    std: (DATA.defaults && DATA.defaults.std) || (DATA.standards[0] && DATA.standards[0].id),
    type: (DATA.defaults && DATA.defaults.type) || '',
    rating: (DATA.defaults && DATA.defaults.rating) || '',
    size: (DATA.defaults && DATA.defaults.size) || '',
    material: (DATA.defaults && DATA.defaults.material) || '',
    calc: null,
    calcSource: 'pending',
    calcError: '',
    items: [],
    seq: 0,
    requestId: 0,
    apiDown: false,
    loggedFallback: false,
    lastFixtureLabel: '',
    lastApiError: null
  };

  /* ------------------------------------------------------------------ */
  /* 基础工具                                                             */
  /* ------------------------------------------------------------------ */

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function isNum(value) { return value !== null && value !== '' && isFinite(Number(value)); }
  function numOf(value, fallback) { return isNum(value) ? Number(value) : fallback; }
  function fmt(value, digits) {
    if (!isNum(value)) { return '—'; }
    var d = digits == null ? 2 : digits;
    try {
      return Number(value).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
    } catch (err) {
      return Number(value).toFixed(d);
    }
  }
  function pct(value) { return isNum(value) ? fmt(value, 1) + '%' : '—'; }
  function curSym() { return (document.getElementById('currencySel') || {}).value === 'USD' ? 'US$' : '¥'; }
  function quoteCurrency() { return (document.getElementById('currencySel') || {}).value === 'USD' ? 'USD' : 'CNY'; }
  function quoteFxRate() { var rate = numField('fxIn', 7.10); return rate > 0 ? rate : 7.10; }
  function symbolFor(currency) { return currency === 'USD' ? 'US$' : '¥'; }
  function priceMoney(value, digits, currency, fxRate) {
    if (!isNum(value)) { return '—'; }
    var rate = (currency === 'USD' && isNum(fxRate) && fxRate > 0) ? Number(fxRate) : 1;
    return symbolFor(currency || 'CNY') + fmt(Number(value) / rate, digits == null ? 2 : digits);
  }
  function cnyMoney(value, digits) { return isNum(value) ? '¥' + fmt(value, digits == null ? 2 : digits) : '—'; }
  function itemMoney(item, value, digits) {
    return priceMoney(value, digits, item.currency || 'CNY', item.fx || 7.10);
  }
  function strNum(value) { return isNum(value) ? String(Number(value)) : ''; }
  function numField(id, fallback) { var el = $(id); return el && isNum(el.value) ? Number(el.value) : fallback; }
  function valField(id) { var el = $(id); return el ? el.value : ''; }
  function setText(id, text) { var el = $(id); if (el) { el.textContent = text; } }
  function setDisabled(id, disabled) { var el = $(id); if (el) { el.disabled = !!disabled; } }
  function show(id, visible) { var el = $(id); if (el) { el.hidden = !visible; } }
  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(null, args); }, wait);
    };
  }
  function toast(message, bad) {
    var el = $('toast');
    if (!el) { return; }
    el.textContent = message;
    el.className = 'toast show' + (bad ? ' bad' : '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { el.className = 'toast'; }, 2600);
  }

  var toastEl = $('toast');

  /* ------------------------------------------------------------------ */
  /* 级联                                                                 */
  /* ------------------------------------------------------------------ */

  function standardObj() { return STANDARD[state.std] || (DATA.standards[0] || null); }
  function typeObj(code) { return TYPE[code] || { code: code, name: code }; }
  function ratingLabel(code) {
    var std = standardObj();
    if (!std) { return code; }
    for (var i = 0; i < std.ratings.length; i += 1) {
      if (std.ratings[i][0] === code) { return std.ratings[i][1]; }
    }
    return code;
  }
  function materialObj(code) { return MATERIAL[code] || { code: code, label: code, family: '' }; }
  function rowsFor(std, type, rating) {
    return (DATA.rows || []).filter(function (row) {
      return row[R.std] === std && row[R.type] === type && row[R.rating] === rating;
    });
  }
  function currentRow() {
    for (var i = 0; i < DATA.rows.length; i += 1) {
      var row = DATA.rows[i];
      if (row[R.std] === state.std && row[R.type] === state.type
        && row[R.rating] === state.rating && row[R.size] === state.size) {
        return row;
      }
    }
    return null;
  }
  function fillSelect(el, items, preferred) {
    if (!el) { return; }
    var html = items.map(function (item) {
      return '<option value="' + esc(item.value) + '">' + esc(item.label) + '</option>';
    }).join('');
    el.innerHTML = html;
    var hasPreferred = items.some(function (item) { return item.value === preferred; });
    if (hasPreferred) {
      el.value = preferred;
    } else if (items.length) {
      el.value = items[0].value;
    }
  }
  function refreshTypes() {
    var std = standardObj();
    if (!std) { return; }
    var items = std.types.map(function (code) {
      var t = typeObj(code);
      return { value: code, label: t.name || code };
    });
    fillSelect($('typeSel'), items, state.type);
    state.type = $('typeSel').value;
  }
  function refreshRatings() {
    var std = standardObj();
    if (!std) { return; }
    var items = std.ratings.map(function (pair) { return { value: pair[0], label: pair[1] }; });
    fillSelect($('ratingSel'), items, state.rating);
    state.rating = $('ratingSel').value;
  }
  function refreshMaterials() {
    var std = standardObj();
    if (!std) { return; }
    var items = std.materials.map(function (code) {
      var m = materialObj(code);
      return { value: code, label: m.label + (m.family ? ' · ' + m.family : '') };
    });
    fillSelect($('matSel'), items, state.material);
    state.material = $('matSel').value;
    syncMaterialPrice();
  }
  function canViewCost() { return DATA.can_view_cost !== false; }

  function materialBasePrice() {
    var m = materialObj(state.material);
    return m && isNum(m.demo_price) ? Number(m.demo_price) : null;
  }

  function isMaterialOverride() {
    var checkbox = $('matPriceOverride');
    return !!(canViewCost() && checkbox && checkbox.checked);
  }

  function materialOverrideValue() {
    if (!isMaterialOverride()) { return null; }
    var value = Number(valField('matPriceIn'));
    return (isFinite(value) && value > 0) ? value : null;
  }

  function machiningInputValid() {
    var raw = machiningRateValue();
    if (raw === '') { return true; }
    var value = Number(raw);
    return isFinite(value) && value >= 0;
  }

  function machiningRateValue() {
    var input = $('machiningRateIn');
    if (!canViewCost() || !input) { return ''; }
    return (input.value || '').trim();
  }

  function materialLibrarySourceLabel() {
    var m = materialObj(state.material);
    var label = m.price_source_label || '';
    if (!label || label === '价格库') { return '价格库'; }
    return '价格库（' + label + '）';
  }

  function updatePriceSourceTags() {
    if (!canViewCost()) {
      setText('pcMatSource', '无权限');
      setText('pcMachRate', '无权限');
      setText('machiningRateSource', '无权限');
      setText('matPriceSource', '无权限');
      return;
    }
    var materialSource = isMaterialOverride()
      ? (materialOverrideValue() != null ? '本次' : '本次（待修正）')
      : materialLibrarySourceLabel();
    setText('pcMatSource', materialSource);
    var rate = machiningRateValue();
    var rateInvalid = !machiningInputValid();
    setText('pcMachRate', rate !== ''
      ? (rateInvalid ? '本次（待修正）' : '本次 ' + rate + ' 元/件')
      : '价格库/档位');
    setText('machiningRateSource', rate !== '' ? (rateInvalid ? '本次（待修正）' : '本次覆盖') : '默认：价格库/档位');
    setText('matPriceSource', materialSource);
  }

  function setMaterialOverride(active, resetToBase) {
    var checkbox = $('matPriceOverride');
    var input = $('matPriceIn');
    if (!canViewCost() || !checkbox || !input) { return; }
    if (active) { checkbox.checked = true; }
    if (!active || resetToBase) {
      checkbox.checked = !!active;
      var base = materialBasePrice();
      input.value = base != null ? base : '—';
    }
    input.readOnly = !checkbox.checked;
    syncMaterialPrice();
    scheduleCalc();
  }

  function syncMaterialPrice() {
    var m = materialObj(state.material);
    var priceEl = $('matPriceIn');
    var note = $('matPriceNote');
    setText('matHint', m.label || m.code || '—');
    if (!canViewCost()) {
      if (priceEl) { priceEl.value = '—'; priceEl.readOnly = true; }
      setText('matPriceSource', '无权限');
      if (note) { note.textContent = '当前角色不可查看材料价；本次覆盖与成本参数仅技术 / 财务 / 经理 / 管理员可用。'; }
      updatePriceSourceTags();
      return;
    }
    var base = materialBasePrice();
    if (priceEl && !isMaterialOverride()) {
      priceEl.value = base != null ? base : '—';
      priceEl.readOnly = true;
    } else if (priceEl && isMaterialOverride()) {
      priceEl.readOnly = false;
    }
    setText('matPriceSource', isMaterialOverride()
      ? '本次'
      : materialLibrarySourceLabel());
    if (note) {
      if (isMaterialOverride()) {
        var valid = materialOverrideValue() != null;
        note.textContent = valid
          ? '本次覆盖材料价 ' + (priceEl ? priceEl.value : '—') + ' 元/kg；基准价 ' +
            (base != null ? base : '—') + ' 元/kg（保存时写入成本快照）。'
          : '本次覆盖材料价必须大于 0；当前无效值不会提交覆盖，仍按价格库价计算。';
      } else if (m.price_effective_from) {
        note.textContent = '价格库生效 ' + m.price_effective_from + (m.price_is_demo ? ' · 演示价（禁止用于生产）' : '');
      } else {
        note.textContent = '价格库无生效价时 /quote/calc/ 会返回 price_unavailable';
      }
    }
    updatePriceSourceTags();
  }
  function refreshSizes(preferredDn) {
    var previous = currentRow();
    var targetDn = preferredDn || (previous ? previous[R.dn] : null) || (currentRow() ? currentRow()[R.dn] : null);
    var rows = rowsFor(state.std, state.type, state.rating);
    rows.sort(function (a, b) { return a[R.dn] - b[R.dn]; });
    var items = rows.map(function (row) {
      return { value: row[R.size], label: row[R.label] + ' · DN' + row[R.dn] };
    });
    var preferred = state.size;
    if (!rows.some(function (row) { return row[R.size] === preferred; }) && targetDn != null) {
      var closest = null;
      rows.forEach(function (row) {
        if (closest === null || Math.abs(row[R.dn] - targetDn) < Math.abs(closest[R.dn] - targetDn)) {
          closest = row;
        }
      });
      if (closest) { preferred = closest[R.size]; }
    }
    fillSelect($('sizeSel'), items, preferred);
    state.size = $('sizeSel').value;
  }

  function refreshCascade() {
    refreshTypes();
    refreshRatings();
    refreshMaterials();
    refreshSizes();
    renderSpec();
    scheduleCalc();
  }

  /* ------------------------------------------------------------------ */
  /* 规格面板                                                             */
  /* ------------------------------------------------------------------ */

  function specBadges(row) {
    var std = standardObj();
    var t = typeObj(state.type);
    var m = materialObj(state.material);
    var chips = [
      std ? std.short : state.std,
      t.name,
      ratingLabel(state.rating),
      row ? row[R.label] : state.size,
      row ? 'DN' + row[R.dn] : '',
      m.code
    ];
    return chips.filter(Boolean).map(function (text) {
      return '<span>' + esc(text) + '</span>';
    }).join('');
  }
  function dimensionCells(row) {
    if (!row) { return ''; }
    var cells = [
      ['外径 D', isNum(row[R.od]) ? fmt(row[R.od], 1) : '—'],
      ['厚度 C', isNum(row[R.th]) ? fmt(row[R.th], 1) : '—'],
      ['密封面 D2', isNum(row[R.rf]) ? fmt(row[R.rf], 1) : '—'],
      ['密封面 f', isNum(row[R.rfh]) ? fmt(row[R.rfh], 1) : '—'],
      ['中心圆 K', isNum(row[R.bc]) ? fmt(row[R.bc], 1) : '—'],
      ['螺栓孔', isNum(row[R.bolts]) && isNum(row[R.hole]) ? row[R.bolts] + ' × Ø' + fmt(row[R.hole], 1) : '—'],
      ['轮毂 A', isNum(row[R.hub]) ? fmt(row[R.hub], 1) : '—'],
      ['长度 H', isNum(row[R.len]) ? fmt(row[R.len], 1) : '—'],
      ['焊接端', isNum(row[R.hubw]) ? fmt(row[R.hubw], 1) : '—'],
      ['参考重量', isNum(row[R.weight]) ? fmt(row[R.weight], 3) + ' kg' : '—']
    ];
    return cells.map(function (cell) {
      return '<div><span>' + esc(cell[0]) + '</span><b>' + esc(cell[1]) + '</b></div>';
    }).join('');
  }
  function renderSpec() {
    var row = currentRow();
    var std = standardObj();
    var t = typeObj(state.type);
    var hint = std
      ? std.name + ' · ' + ratingLabel(state.rating) + ' · ' + (row ? row[R.label] : state.size) + ' · ' + t.name
      : '—';
    setText('selectionHint', hint);
    var badges = $('specBadges');
    if (badges) { badges.innerHTML = specBadges(row); }
    var grid = $('dimGrid');
    if (grid) { grid.innerHTML = dimensionCells(row); }

    var weightEl = $('weightValue');
    var hintEl = $('weightHint');
    var tagEl = $('weightTag');
    var checkEl = $('weightCheck');
    if (!row) {
      if (weightEl) { weightEl.innerHTML = '—<small>kg</small>'; }
      if (hintEl) { hintEl.textContent = '该标准 / 类型 / 等级 / 口径组合在标准库中不存在。'; }
      if (tagEl) { tagEl.textContent = '无数据'; }
      if (checkEl) { checkEl.innerHTML = ''; }
      return;
    }
    if (isNum(row[R.weight])) {
      if (weightEl) { weightEl.innerHTML = fmt(row[R.weight], 3) + '<small>kg</small>'; }
      if (hintEl) { hintEl.textContent = '标准库参考重量 · 实际净重按材质密度由 T1 重量引擎修正（B 级）'; }
      if (tagEl) { tagEl.textContent = '标准值'; }
      if (checkEl) {
        checkEl.innerHTML = '<span>重量来源 <b>标准参考重量</b></span>'
          + '<span>置信度 <b>B</b></span>'
          + '<span>规则 <b>w0.3</b></span>'
          + '<span>材质密度 <b>' + fmt(materialObj(state.material).density, 2) + ' g/cm³</b></span>';
      }
    } else {
      if (weightEl) { weightEl.innerHTML = '—<small>kg</small>'; }
      if (hintEl) { hintEl.textContent = '标准库无参考重量；等待 T1 重量引擎按几何模型估算（C 级，以称重为准）'; }
      if (tagEl) { tagEl.textContent = '几何估算（C 级）'; }
      if (checkEl) {
        checkEl.innerHTML = '<span>重量来源 <b>几何模型（待 T1）</b></span>'
          + '<span>置信度 <b>C</b></span>'
          + '<span>规则 <b>w0.3</b></span>';
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* 计算请求 / 契约夹具回退                                              */
  /* ------------------------------------------------------------------ */

  function checkedInspections() {
    return [
      ['inspPMI', 'pmi'], ['inspUT', 'ut'], ['inspHydro', 'hydro'],
      ['inspHardness', 'hardness'], ['inspThird', 'third']
    ].filter(function (pair) { return $(pair[0]) && $(pair[0]).checked; })
      .map(function (pair) { return pair[1]; });
  }
  function buildPayload() {
    var params = {
      yield: numField('yieldIn', DEFAULT_PARAMS.yield || 0.7),
      machining_factor: numField('machIn', DEFAULT_PARAMS.machining_factor || 1.0),
      management_pct: numField('mgmtIn', DEFAULT_PARAMS.management_pct || 6),
      profit_pct: numField('profitIn', DEFAULT_PARAMS.profit_pct || 12),
      tax_pct: numField('taxIn', DEFAULT_PARAMS.tax_pct || 13)
    };
    // 动态覆盖：只有成本权限角色才会发送，sales 永远只用价格库/后端默认值
    if (canViewCost() && isMaterialOverride()) {
      var manualPrice = Number(valField('matPriceIn'));
      if (isFinite(manualPrice) && manualPrice > 0) {
        params.material_price_per_kg = manualPrice;
      }
    }
    if (canViewCost()) {
      var manualRate = machiningRateValue();
      if (manualRate !== '' && isFinite(Number(manualRate)) && Number(manualRate) >= 0) {
        params.machining_rate = Number(manualRate);
      }
    }
    return {
      std: state.std,
      type: state.type,
      size: state.size,
      rating: state.rating,
      material: state.material,
      qty: Math.max(1, Math.round(numField('qtyIn', 10))),
      design_temp_c: numField('tempIn', 38),
      design_pressure_bar: numField('pressIn', 16),
      surface: valField('surfaceSel') || 'none',
      packaging: valField('packSel') || 'wood',
      inspections: checkedInspections(),
      freight_per_kg: numField('freightIn', 0.6),
      discount_pct: numField('discountIn', 0),
      currency: valField('currencySel') || 'CNY',
      fx_rate: numField('fxIn', 7.10),
      params: params
    };
  }
  function calcKey(p) {
    var key = [
      p.std, p.type, p.size, p.rating, p.material,
      strNum(p.qty), strNum(p.design_temp_c), strNum(p.design_pressure_bar),
      p.surface, p.packaging, (p.inspections || []).join('+'),
      strNum(p.freight_per_kg), strNum(p.discount_pct), p.currency, strNum(p.fx_rate),
      strNum(p.params.yield), strNum(p.params.machining_factor),
      strNum(p.params.management_pct), strNum(p.params.profit_pct), strNum(p.params.tax_pct)
    ].join('|');
    if (p.params.material_price_per_kg != null) {
      key += '|manual-price:' + strNum(p.params.material_price_per_kg);
    }
    if (p.params.machining_rate != null) {
      key += '|manual-rate:' + strNum(p.params.machining_rate);
    }
    return key;
  }
  function findFixture(key) {
    var fixtures = DATA.fixtures || [];
    for (var i = 0; i < fixtures.length; i += 1) {
      if (fixtures[i].key === key) { return fixtures[i]; }
    }
    return null;
  }

  var scheduleCalc = debounce(function () { requestCalc(); }, 220);
  var abortController = null;

  function requestCalc() {
    var payload = buildPayload();
    state.requestId += 1;
    var requestId = state.requestId;
    if (abortController && abortController.abort) {
      try { abortController.abort(); } catch (err) { /* ignore */ }
    }
    abortController = typeof AbortController !== 'undefined' ? new AbortController() : null;
    setCalcLoading();

    if (state.apiDown) {
      fallbackCalc(payload, '接口暂不可用（页面加载时检测到 /quote/calc/ 未注册或网络失败）');
      return;
    }

    var options = {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': DATA.csrf_token || '',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify(payload)
    };
    if (abortController) { options.signal = abortController.signal; }

    fetch(DATA.endpoints.calc, options)
      .then(function (response) {
        var contentType = response.headers.get('content-type') || '';
        if (!response.ok) {
          if (contentType.indexOf('application/json') >= 0) {
            return response.json().then(function (body) {
              var apiErr = new Error((body && body.message) || ('HTTP ' + response.status));
              apiErr.quoteError = body || { ok: false, error: 'http_' + response.status };
              apiErr.httpStatus = response.status;
              throw apiErr;
            });
          }
          var httpErr = new Error('HTTP ' + response.status);
          httpErr.httpStatus = response.status;
          throw httpErr;
        }
        if (contentType.indexOf('application/json') < 0) {
          var typeErr = new Error('返回的不是 JSON');
          typeErr.httpStatus = response.status;
          throw typeErr;
        }
        return response.json();
      })
      .then(function (json) {
        if (requestId !== state.requestId) { return; }
        if (!json || json.ok === false) {
          var apiErr = new Error((json && json.message) || '计算失败');
          apiErr.quoteError = json || { ok: false, error: 'invalid_response' };
          throw apiErr;
        }
        state.apiDown = false;
        state.calcSource = 'api';
        state.calcError = '';
        state.lastApiError = null;
        state.calc = json;
        state.lastFixtureLabel = '';
        renderCalc(json, payload, null);
      })
      .catch(function (err) {
        if (requestId !== state.requestId) { return; }
        if (err && err.name === 'AbortError') { return; }
        if (err && err.quoteError) {
          // 契约 §8：{ok:false,error,message,field} 原样展示，不回退到示例夹具
          state.apiDown = false;
          state.calcSource = 'error';
          state.calc = null;
          state.lastApiError = err.quoteError;
          state.calcError = err.quoteError.message || err.message;
          renderApiError(payload, err.quoteError);
          return;
        }
        // 非 JSON 的 HTTP 错误（如 CSRF 403 / 500）：明确报错，不伪装成正常计算
        if (err && err.httpStatus && err.httpStatus !== 404 && err.httpStatus !== 405) {
          renderApiError(payload, {
            ok: false,
            error: 'http_' + err.httpStatus,
            message: (err && err.message) || ('接口返回 HTTP ' + err.httpStatus)
          });
          return;
        }
        // 404/405/网络错误：接口未挂载或不可达，回退到契约示例夹具供版式预览
        state.apiDown = true;
        fallbackCalc(payload, (err && err.message) || '网络错误');
      });
  }

  function fallbackCalc(payload, errorMessage) {
    var fixture = findFixture(calcKey(payload));
    if (!state.loggedFallback) {
      state.loggedFallback = true;
      if (window.console) {
        console.info('[quote] /quote/calc/ 未就绪（' + errorMessage + '），已回退到《接口契约》§9 示例夹具；T1 接入后刷新页面即自动改用真实接口。');
      }
    }
    if (fixture) {
      state.calcSource = 'mock';
      state.calcError = '';
      state.calc = fixture.response;
      state.lastFixtureLabel = fixture.label || '契约夹具';
      renderCalc(fixture.response, payload, fixture);
    } else {
      state.calcSource = 'unavailable';
      state.calc = null;
      state.calcError = errorMessage || '';
      renderUnavailable(payload, errorMessage);
    }
  }

  function setCalcLoading() {
    var list = $('costList');
    if (list) {
      list.innerHTML = '<div class="calc-placeholder" id="calcPlaceholder">'
        + '<b>正在计算…</b><p>请求 <code>/quote/calc/</code>；未就绪时会回退到契约示例夹具。</p></div>';
    }
    setText('calcSource', '数据源：计算中…');
    var sourceEl = $('calcSource');
    if (sourceEl) { sourceEl.className = 'calc-source'; }
    setDisabled('addBtn', true);
  }

  function resetPriceCard() {
    ['pcUnit', 'pcNet', 'pcTotal', 'pcMargin', 'pcPerKg', 'pcLead', 'pcQtyDisc', 'pcApproval'].forEach(function (id) {
      setText(id, '—');
    });
    setText('pcValid', '15 天');
    var pt = $('ptCheck');
    if (pt) {
      pt.className = 'pc-check na';
      pt.innerHTML = '压力–温度校验：等待计算';
    }
    ['gaugeC', 'gaugeP', 'gaugeT'].forEach(function (id) {
      var el = $(id);
      if (el) { el.style.width = '0%'; }
    });
  }

  function renderUnavailable(payload, errorMessage) {
    resetPriceCard();
    var label = standardObj() ? standardObj().name : payload.std;
    var list = $('costList');
    if (list) {
      list.innerHTML = ''
        + '<div class="calc-placeholder is-error">'
        + '<b>示例夹具未覆盖当前组合</b>'
        + '<p>' + esc(label + ' · ' + typeObj(payload.type).name + ' · ' + ratingLabel(payload.rating) + ' · ' + payload.size + ' · ' + payload.material) + '</p>'
        + '<p>Wave 1 的可视化回退仅覆盖《接口契约》§9 夹具：<strong>4″ Class 150 WN A105、数量 10、默认参数</strong>。'
        + '成本、价格与 p/T 校验由 T1 的 <code>/quote/calc/</code> 提供；接口就绪后本面板自动显示实算结果。</p>'
        + (errorMessage ? '<p class="calc-error">接口状态：' + esc(errorMessage) + '</p>' : '')
        + '</div>';
    }
    var sourceEl = $('calcSource');
    if (sourceEl) {
      sourceEl.className = 'calc-source is-mock';
      sourceEl.textContent = '数据源：等待 T1 /quote/calc/ · 当前无可用示例夹具';
    }
    var pt = $('ptCheck');
    if (pt) {
      pt.className = 'pc-check na';
      pt.innerHTML = '压力–温度校验：<b>等待后端</b> · 不显示未经验证的结论';
    }
    setDisabled('addBtn', true);
    setText('addBtn', '等待后端计算');
    setText('pcMatSource', isMaterialOverride() ? '本次' : '价格库');
    setText('pcMachRate', machiningRateValue() !== '' ? '本次 ' + machiningRateValue() + ' 元/件' : '价格库/档位');
    var snapshot = $('snapGrid');
    if (snapshot) {
      snapshot.innerHTML = ''
        + '<div class="snap-item"><span>标准版本</span><b>' + esc(standardObj() ? standardObj().edition : '—') + '</b></div>'
        + '<div class="snap-item"><span>重量规则</span><b>w0.3</b></div>'
        + '<div class="snap-item"><span>材料价</span><b>等待价格库</b></div>'
        + '<div class="snap-item"><span>汇率 / 税率</span><b>' + fmt(payload.fx_rate, 2) + ' / ' + fmt(payload.params.tax_pct, 0) + '%</b></div>';
    }
  }

  function renderApiError(payload, info) {
    resetPriceCard();
    var message = (info && info.message) || '计算失败';
    var code = (info && info.error) || 'error';
    var field = info && info.field;
    var list = $('costList');
    if (list) {
      list.innerHTML = ''
        + '<div class="calc-placeholder is-error">'
        + '<b>/quote/calc/ 计算失败</b>'
        + '<p>' + esc(message) + '</p>'
        + '<p class="calc-error mono">error: ' + esc(code) + (field ? ' · field: ' + esc(field) : '') + '</p>'
        + '<p>请检查价格库是否已维护、组合是否有效、参数是否越界；修正后会自动重新计算。'
        + '本页不会用前端算法替代后端结果。</p>'
        + '</div>';
    }
    var sourceEl = $('calcSource');
    if (sourceEl) {
      sourceEl.className = 'calc-source is-error';
      sourceEl.textContent = '数据源：/quote/calc/ 返回错误 · ' + code + (field ? '（' + field + '）' : '');
    }
    var pt = $('ptCheck');
    if (pt) {
      pt.className = 'pc-check bad';
      pt.innerHTML = '压力–温度校验：<b>未完成</b> · 计算接口返回错误，不做前端推断';
    }
    var snapshot = $('snapGrid');
    if (snapshot) {
      snapshot.innerHTML = ''
        + '<div class="snap-item"><span>标准版本</span><b>—</b></div>'
        + '<div class="snap-item"><span>重量规则</span><b>w0.3</b></div>'
        + '<div class="snap-item"><span>材料价</span><b>—</b></div>'
        + '<div class="snap-item"><span>汇率 / 税率</span><b>' + fmt(payload.fx_rate, 2) + ' / ' + fmt(payload.params.tax_pct, 0) + '%</b></div>';
    }
    setDisabled('addBtn', true);
    setText('addBtn', '计算失败 · 请修正');
  }

  function renderCalc(calc, payload, fixture) {
    renderPriceCard(calc, payload);
    renderCosts(calc, payload);
    renderValidation(calc, payload);
    renderSnapshot(calc, payload);
    var sourceEl = $('calcSource');
    if (sourceEl) {
      if (state.calcSource === 'api') {
        sourceEl.className = 'calc-source is-ready';
        sourceEl.textContent = '数据源：/quote/calc/ 实算结果';
      } else {
        sourceEl.className = 'calc-source is-mock';
        sourceEl.textContent = '数据源：离线示例夹具 · ' + (state.lastFixtureLabel || (fixture && fixture.label) || '契约 §9') + '（/quote/calc/ 未连接；示例参数，非正式报价）';
      }
    }
    var addDisabled = !(calc && calc.price && isNum(calc.price.unit_gross));
    setDisabled('addBtn', addDisabled);
    if (!addDisabled) {
      setText('addBtn', '加入报价单');
    } else {
      setText('addBtn', calc && calc.partial ? '仅重量夹具 · 等后端成本' : '等待后端计算');
    }
  }

  function renderPriceCard(calc, payload) {
    var price = calc.price || {};
    var item = calc.item || {};
    var currency = payload.currency === 'USD' ? 'USD' : 'CNY';
    setText('pcTag', currency + ' · 含税');
    var currency = payload.currency === 'USD' ? 'USD' : 'CNY';
    var fxRate = isNum(payload.fx_rate) ? payload.fx_rate : 7.10;
    setText('pcUnit', isNum(price.unit_gross) ? priceMoney(price.unit_gross, 2, currency, fxRate) : '—');
    setText('pcNet', isNum(price.unit_net) ? priceMoney(price.unit_net, 2, currency, fxRate) : '—');
    setText('pcTotal', isNum(price.total_gross) ? priceMoney(price.total_gross, 2, currency, fxRate) : '—');
    setText('pcMargin', isNum(price.margin_pct) ? pct(price.margin_pct) : '—');
    setText('pcPerKg', isNum(price.per_kg) ? priceMoney(price.per_kg, 2, currency, fxRate) : '—');
    setText('pcLead', isNum(item.lead_time_days) ? item.lead_time_days + ' 天' : '—');
    setText('pcValid', '15 天');
    setText('pcQtyDisc', isNum(price.quantity_discount_pct) ? pct(price.quantity_discount_pct) : (isNum(payload.discount_pct) && payload.discount_pct ? '折扣 ' + pct(payload.discount_pct) : '—'));
    setText('pcApproval', calc.approval_required ? '需审批' : '无需审批（自动）');
    var qtyDisc = $('qtyDiscOut');
    if (qtyDisc) {
      qtyDisc.value = isNum(price.quantity_discount_pct) ? pct(price.quantity_discount_pct) : '—';
    }

    var unit = isNum(price.unit_gross) ? price.unit_gross : null;
    var cost = calc.costs && isNum(calc.costs.total) ? calc.costs.total : null;
    var tax = isNum(price.unit_tax) ? price.unit_tax : null;
    var c = 0; var p = 0; var t = 0;
    if (unit && cost != null) { c = Math.max(0, Math.min(100, (cost / unit) * 100)); }
    if (unit && tax != null) { t = Math.max(0, Math.min(100, (tax / unit) * 100)); }
    p = Math.max(0, Math.min(100, 100 - c - t));
    var gaugeC = $('gaugeC'); var gaugeP = $('gaugeP'); var gaugeT = $('gaugeT');
    if (gaugeC) { gaugeC.style.width = c + '%'; }
    if (gaugeP) { gaugeP.style.width = p + '%'; }
    if (gaugeT) { gaugeT.style.width = t + '%'; }
  }

  function renderCosts(calc, payload) {
    var list = $('costList');
    if (!list) { return; }
    if (!calc.costs && !calc.partial) {
      list.innerHTML = ''
        + '<div class="calc-placeholder">'
        + '<b>成本明细不可见</b>'
        + '<p>当前角色的报价接口不返回成本明细（T7 权限矩阵）：你仍可查看单价、总价、毛利率与 p/T 校验结论。'
        + '成本明细仅技术 / 财务 / 经理 / 管理员可见。</p>'
        + '</div>';
      return;
    }
    if (calc.partial || !calc.costs || !isNum(calc.costs.total)) {
      var item = calc.item || {};
      list.innerHTML = ''
        + '<div class="calc-placeholder">'
        + '<b>' + (calc.message || '该组合仅有重量夹具，成本与价格等待后端') + '</b>'
        + (isNum(item.net_weight)
          ? '<p>夹具净重：<strong>' + fmt(item.net_weight, 3) + ' kg</strong> · 来源 '
            + esc(item.weight_source || '—') + ' · 置信度 ' + esc(item.weight_confidence || '—') + '</p>'
          : '')
        + '<p>点击「恢复默认参数」可查看契约 §9 的完整 4″ Class 150 WN A105 示例。</p>'
        + '</div>';
      return;
    }
    var costs = calc.costs;
    var rows = [
      ['材料', 'material', '材料价 × 净重 / 利用率'],
      ['加工', 'machining', '按 DN 档 + 类型系数'],
      ['表面处理', 'surface', '按工艺费率'],
      ['检测', 'inspection', '按检测项'],
      ['包装', 'packaging', '按包装方式'],
      ['运费', 'freight', '按元/kg'],
      ['管理费', 'management', '按成本基数']
    ];
    var html = rows.map(function (row) {
      var value = costs[row[1]];
      return '<div class="cost-row"><span>' + esc(row[0]) + '<i>' + esc(row[2]) + '</i></span><b>' + cnyMoney(value) + '</b></div>';
    }).join('');
    html += '<div class="cost-row sum"><span>单件成本合计</span><b>' + cnyMoney(costs.total) + '</b></div>';
    if (calc.validation && calc.validation.messages && calc.validation.messages.length) {
      html += '<div class="calc-warning">' + calc.validation.messages.map(function (message) {
        return '<p>' + esc(message) + '</p>';
      }).join('') + '</div>';
    }
    list.innerHTML = html;
  }

  function renderValidation(calc, payload) {
    var el = $('ptCheck');
    if (!el) { return; }
    var validation = calc.validation || {};
    var pt = validation.pt || null;
    var status = (pt && pt.status) || validation.status || 'unknown';
    var tone = status === 'pass' ? 'ok' : (status === 'fail' ? 'bad' : (status === 'warn' ? 'na' : 'na'));
    var statusText = { pass: '通过', warn: '警告', fail: '不通过', na: '未接入 / 待补充' }[status] || esc(status);
    el.className = 'pc-check ' + tone;
    if (pt) {
      var text = '压力–温度校验：<b>' + statusText + '</b>'
        + (isNum(pt.allowed_bar) ? ' · 允许 <b>' + fmt(pt.allowed_bar, 1) + ' bar</b>' : '')
        + (pt.material_group ? ' · 材料组 <b>' + esc(pt.material_group) + '</b>' : '');
      var messages = (validation.messages || []).filter(Boolean);
      if (messages.length) {
        text += '<br>' + messages.map(esc).join('<br>');
      }
      el.innerHTML = text;
    } else {
      el.className = 'pc-check ' + tone;
      el.innerHTML = '压力–温度校验：<b>' + statusText + '</b> · 未返回 p/T 明细';
    }
  }

  function renderSnapshot(calc, payload) {
    var grid = $('snapGrid');
    if (!grid) { return; }
    var snapshot = calc.snapshot || {};
    var params = snapshot.params || {};
    var editions = snapshot.standard_editions || {};
    var editionText = '';
    Object.keys(editions).some(function (key) {
      editionText = editions[key];
      return true;
    });
    if (!editionText) {
      var std = standardObj();
      editionText = std ? std.edition : '—';
    }
    var materialPrice = params.material_price_per_kg;
    var materialBase = params.material_price_base != null
      ? params.material_price_base
      : materialBasePrice();
    var matSourceRaw = params.material_price_source || (isMaterialOverride() ? 'manual' : 'library');
    var matSourceText = matSourceRaw === 'manual' ? '本次' : (matSourceRaw === 'library' ? '价格库' : '价格库');
    var effectiveFrom = params.material_price_effective_from;
    var ratesDemo = params.demo_rates;
    var machiningActual = params.machining_rate;
    if (!isNum(machiningActual) && machiningRateValue() !== '' && isNum(machiningRateValue())) {
      machiningActual = Number(machiningRateValue());
    }
    var machiningSourceRaw = params.machining_rate_source || (machiningRateValue() !== '' ? 'manual' : 'process');
    var machiningSourceText = machiningSourceRaw === 'manual'
      ? '本次' : (machiningSourceRaw === 'process' ? '价格库/档位' : '默认档位');
    var qtyDiscount = params.quantity_discount_pct != null ? params.quantity_discount_pct : (calc.price && calc.price.quantity_discount_pct);
    grid.innerHTML = ''
      + '<div class="snap-item"><span>标准版本</span><b>' + esc(editionText) + '</b></div>'
      + '<div class="snap-item"><span>重量规则</span><b>' + esc(snapshot.weight_rule || params.weight_rule || 'w0.3') + '</b></div>'
      + '<div class="snap-item"><span>材料利用率</span><b>' + fmt(params.yield, 2) + '</b></div>'
      + '<div class="snap-item"><span>管理费 / 利润率</span><b>' + fmt(params.management_pct, 1) + '% / ' + fmt(params.profit_pct, 1) + '%</b></div>'
      + '<div class="snap-item"><span>数量优惠 / 折扣</span><b>' + (isNum(qtyDiscount) ? fmt(qtyDiscount, 1) + '%' : '—') + ' / ' + fmt(params.discount_pct != null ? params.discount_pct : payload.discount_pct, 1) + '%</b></div>'
      + '<div class="snap-item"><span>税率 / 汇率</span><b>' + fmt(params.tax_pct, 1) + '% / ' + fmt(params.fx_rate != null ? params.fx_rate : payload.fx_rate, 2) + '</b></div>'
      + '<div class="snap-item"><span>材料价 本次 / 基准</span><b>' + (isNum(materialPrice) ? cnyMoney(materialPrice) : '待价格库') + ' / ' + (isNum(materialBase) ? cnyMoney(materialBase) : '—') + '</b></div>'
      + '<div class="snap-item"><span>材料价来源</span><b>' + esc(matSourceText) + (effectiveFrom ? ' · ' + esc(effectiveFrom) : '') + '</b></div>'
      + '<div class="snap-item"><span>工资率（本次）</span><b>' + (isNum(machiningActual) ? cnyMoney(machiningActual) + ' 元/件' : '价格库/档位') + '</b></div>'
      + '<div class="snap-item"><span>工资率来源</span><b>' + esc(machiningSourceText) + '</b></div>'
      + '<div class="snap-item"><span>费率来源</span><b>' + (ratesDemo == null ? '权限受限 / —' : (ratesDemo ? '演示默认费率' : '价格库（按生效日期）')) + '</b></div>';
    setText('pcMatSource', matSourceText);
    setText('pcMachRate', isNum(machiningActual) ? cnyMoney(machiningActual) + ' 元/件' : '价格库/档位');
    updatePriceSourceTags();
  }

  /* ------------------------------------------------------------------ */
  /* 报价清单 / A4 预览 / CSV                                             */
  /* ------------------------------------------------------------------ */

  function selectedCustomer() {
    var select = $('qCustomer');
    if (!select || !select.value) { return null; }
    var customers = DATA.customers || [];
    for (var i = 0; i < customers.length; i += 1) {
      if (customers[i].code === select.value) { return customers[i]; }
    }
    var option = select.options[select.selectedIndex];
    if (!option) { return null; }
    return {
      code: option.value,
      name: option.getAttribute('data-name') || option.textContent
    };
  }

  function applyCustomerDefaults() {
    var customer = selectedCustomer();
    var hint = $('customerHint');
    if (!customer) {
      if (hint) { hint.textContent = '未选客户：按手填折扣报价；选择客户后自动带入默认折扣、币种与账期。'; }
      return;
    }
    var option = $('qCustomer').options[$('qCustomer').selectedIndex];
    var discount = option.getAttribute('data-discount');
    var currency = option.getAttribute('data-currency');
    var payment = option.getAttribute('data-payment');
    if (discount !== null && discount !== '' && $('discountIn')) { $('discountIn').value = discount; }
    if (currency && $('currencySel')) { $('currencySel').value = currency; }
    if (payment && $('qPayment')) { $('qPayment').value = payment; }
    if (hint) {
      hint.textContent = '已带入 ' + customer.name + '：折扣 ' + (discount || '—') + '% · 币种 ' +
        (currency || 'CNY') + '；折扣仍可手改，保存时写入客户 ' + customer.code + '。';
    }
    scheduleCalc();
  }

  function quoteMeta() {
    var customer = selectedCustomer();
    return {
      customer: customer ? customer.name : '未填写客户',
      customerCode: customer ? customer.code : '',
      project: (valField('qProject') || '').trim() || '—',
      incoterm: valField('qIncoterm') || 'FOB',
      payment: (valField('qPayment') || '').trim() || '—'
    };
  }

  function buildItem(calc, payload) {
    var item = calc.item || {};
    var price = calc.price || {};
    var row = currentRow();
    var std = standardObj();
    var t = typeObj(state.type);
    var m = materialObj(state.material);
    state.seq += 1;
    return {
      id: state.seq,
      description: item.description || ((std ? std.short : '') + ' ' + t.name + ' ' + (row ? row[R.label] : payload.size)),
      stdName: std ? std.name : payload.std,
      stdShort: std ? std.short : payload.std,
      edition: item.standard_edition || (std ? std.edition : ''),
      typeName: t.name,
      sizeLabel: item.size_label || (row ? row[R.label] : payload.size),
      dn: row ? row[R.dn] : null,
      ratingLabel: item.rating_label || ratingLabel(payload.rating),
      material: item.material || m.label,
      qty: item.qty || payload.qty,
      net: item.net_weight,
      source: item.weight_source || '',
      confidence: item.weight_confidence || '',
      weightRule: item.weight_rule || 'w0.3',
      unitNet: price.unit_net,
      unitTax: price.unit_tax,
      unitGross: price.unit_gross,
      amountNet: price.amount_net,
      taxAmount: price.tax_amount,
      totalGross: price.total_gross,
      lead: item.lead_time_days,
      margin: price.margin_pct,
      currency: payload.currency,
      fx: payload.fx_rate,
      taxPct: payload.params.tax_pct,
      cost: calc.costs || null,
      surface: surfaceOptions[payload.surface] || payload.surface,
      pack: packOptions[payload.packaging] || payload.packaging,
      insp: payload.inspections.length ? payload.inspections.map(function (code) {
        for (var i = 0; i < inspOptions.length; i += 1) {
          if (inspOptions[i][0] === code) { return inspOptions[i][1]; }
        }
        return code;
      }).join(' + ') : '无',
      snapshot: '预览 · 保存后固化',
      params: payload.params,
      payload: payload
    };
  }

  function addCurrent() {
    if (!state.calc || !state.calc.price || !isNum(state.calc.price.unit_gross)) {
      toast('当前组合需要 T1 /quote/calc/ 返回成本与价格后才能加入清单', true);
      return;
    }
    var payload = buildPayload();
    var item = buildItem(state.calc, payload);
    state.items.push(item);
    renderList();
    renderSheet();
    toast('已加入清单：' + item.description + ' × ' + item.qty);
  }

  function removeItem(id) {
    state.items = state.items.filter(function (item) { return item.id !== id; });
    renderList();
    renderSheet();
  }

  function renderList() {
    var list = state.items;
    setText('quoteCount', list.length + ' 项');
    var empty = $('quoteEmpty');
    var wrap = $('quoteTableWrap');
    var body = $('quoteItems');
    var foot = $('quoteFoot');
    var cards = $('quoteCards');
    var mobileFoot = $('quoteMobileFoot');
    setDisabled('printBtn', !list.length);
    setDisabled('csvBtn', !list.length);
    setDisabled('saveBtn', !list.length || DATA.can_create === false);
    var hint = $('saveHint');
    if (hint) {
      hint.textContent = list.length
        ? '保存后由后端生成单号、版本与成本快照，并跳转详情页（' + list.length + ' 项，币种 ' + (valField('currencySel') || 'CNY') + '）。'
        : '保存后由后端生成单号、版本与成本快照，并跳转详情页。';
    }
    if (empty) { empty.hidden = list.length > 0; }
    if (wrap) { wrap.hidden = list.length === 0; }
    var sumNet = 0; var sumTax = 0; var sumGross = 0; var maxLead = 0;
    var firstItem = list[0] || null;
    var currency = firstItem ? (firstItem.currency || 'CNY') : quoteCurrency();
    var fxRate = firstItem ? (firstItem.fx || 7.10) : quoteFxRate();
    var sym = symbolFor(currency);
    if (body) {
      body.innerHTML = list.map(function (item, index) {
        sumNet += numOf(item.amountNet, 0);
        sumTax += numOf(item.taxAmount, 0);
        sumGross += numOf(item.totalGross, 0);
        maxLead = Math.max(maxLead, numOf(item.lead, 0));
        return '<tr>'
          + '<td class="num">' + (index + 1) + '</td>'
          + '<td><b>' + esc(item.description) + '</b><span class="sub">' + esc(item.material + ' · DN' + (item.dn == null ? '—' : item.dn)) + ' · 单重 ' + fmt(item.net, 3) + ' kg · 快照 ' + esc(item.snapshot) + '</span></td>'
          + '<td class="r num">' + item.qty + '</td>'
          + '<td class="r num">' + fmt(item.net, 3) + '</td>'
          + '<td class="r num">' + itemMoney(item, item.unitGross, 2) + '</td>'
          + '<td class="r num">' + itemMoney(item, item.amountNet, 2) + '</td>'
          + '<td class="c"><button class="del" type="button" title="删除" data-del="' + item.id + '">×</button></td>'
          + '</tr>';
      }).join('');
    }
    if (foot) {
      foot.innerHTML = list.length
        ? '<tr><td colspan="5" class="r">不含税合计</td><td class="r num">' + priceMoney(sumNet, 2, currency, fxRate) + '</td><td></td></tr>'
          + '<tr><td colspan="5" class="r">税额</td><td class="r num">' + priceMoney(sumTax, 2, currency, fxRate) + '</td><td></td></tr>'
          + '<tr><td colspan="5" class="r"><b>含税合计</b></td><td class="r num"><b>' + priceMoney(sumGross, 2, currency, fxRate) + '</b></td><td></td></tr>'
        : '';
    }
    if (cards) {
      cards.innerHTML = list.map(function (item) {
        return '<div class="qcard">'
          + '<div class="qcard-head"><div><b>' + esc(item.description) + '</b>'
          + '<div class="qcard-sub">' + esc(item.material + ' · DN' + (item.dn == null ? '—' : item.dn)) + ' · 单重 ' + fmt(item.net, 3) + ' kg</div></div>'
          + '<button class="del" type="button" title="删除" data-del="' + item.id + '">×</button></div>'
          + '<div class="qcard-grid">'
          + '<div><span>数量</span><b>' + item.qty + '</b></div>'
          + '<div><span>含税单价</span><b>' + itemMoney(item, item.unitGross, 2) + '</b></div>'
          + '<div><span>金额（不含税）</span><b>' + itemMoney(item, item.amountNet, 2) + '</b></div>'
          + '<div><span>税额</span><b>' + itemMoney(item, item.taxAmount, 2) + '</b></div>'
          + '<div><span>交期</span><b>' + (item.lead || '—') + ' 天</b></div>'
          + '<div><span>快照</span><b style="font-size:9.5px">' + esc(item.snapshot) + '</b></div>'
          + '</div></div>';
      }).join('');
    }
    if (mobileFoot) {
      mobileFoot.innerHTML = list.length
        ? '<div><span>不含税合计</span><b>' + priceMoney(sumNet, 2, currency, fxRate) + '</b></div>'
          + '<div><span>税额</span><b>' + priceMoney(sumTax, 2, currency, fxRate) + '</b></div>'
          + '<div class="total"><span>含税合计</span><b>' + priceMoney(sumGross, 2, currency, fxRate) + '</b></div>'
        : '';
    }
  }

  function emptySheetHtml() {
    return '<div class="quote-sheet is-empty"><div class="empty-box">'
      + '<b>报价单预览</b><p>加入报价项后，这里会生成 A4 报价单；打印只输出这一页。</p>'
      + '</div></div>';
  }

  function uniqueValues(items, key) {
    var out = [];
    items.forEach(function (item) {
      var value = item[key];
      if (value && out.indexOf(value) < 0) { out.push(value); }
    });
    return out;
  }
  function oneOr(items, key) {
    var values = uniqueValues(items, key);
    if (!values.length) { return '—'; }
    return values.length === 1 ? values[0] : '见明细（' + values.length + ' 项）';
  }

  function sheetHtml() {
    var items = state.items;
    if (!items.length) { return emptySheetHtml(); }
    var meta = quoteMeta();
    var firstItem = items[0];
    var currency = firstItem.currency || 'CNY';
    var fxRate = firstItem.fx || quoteFxRate();
    var sym = symbolFor(currency);
    var sumNet = 0; var sumTax = 0; var sumGross = 0;
    items.forEach(function (item) {
      sumNet += numOf(item.amountNet, 0);
      sumTax += numOf(item.taxAmount, 0);
      sumGross += numOf(item.totalGross, 0);
    });
    var first = items[0];
    var dateStr = DATA.today || '';
    var validStr = DATA.valid_until || '';
    var maxLead = 0;
    items.forEach(function (item) { maxLead = Math.max(maxLead, numOf(item.lead, 0)); });
    var sources = uniqueValues(items, 'source');
    var weightText = sources.length === 1
      ? (sources[0] === 'standard' ? '标准参考重量（B 级）' : (sources[0] === 'geometry' ? '几何估算（C 级，以称重为准）' : sources[0]))
      : 'B / C 级（见明细）';
    var rows = items.map(function (item, index) {
      return '<tr>'
        + '<td class="r sh-seq">' + (index + 1) + '</td>'
        + '<td class="sh-desc"><b>' + esc(item.description) + '</b><span>'
        + esc(item.material) + ' · 快照 ' + esc(item.snapshot) + ' · 重量来源 ' + esc(item.confidence ? (item.source === 'standard' ? '标准参考（' : '几何估算（') + item.confidence + ' 级）' : '—')
        + '</span></td>'
        + '<td class="r" data-label="数量">' + item.qty + '</td>'
        + '<td class="r" data-label="单重">' + fmt(item.net, 3) + ' kg</td>'
        + '<td class="r" data-label="含税单价">' + itemMoney(item, item.unitGross, 2) + '</td>'
        + '<td class="r" data-label="金额（不含税）">' + itemMoney(item, item.amountNet, 2) + '</td>'
        + '<td class="r" data-label="交期">' + (item.lead || '—') + ' 天</td>'
        + '</tr>';
    }).join('');
    return '<div class="quote-sheet">'
      + '<div class="sh-top">'
      + '<div class="sh-co"><b>示例法兰制造有限公司</b><span>中国 · 江苏 · 示例工业园区 · 0510-0000 0000 · quote@example.com</span><span>报价单样张 · 演示主体（上线前替换为真实公司抬头）</span></div>'
      + '<div class="sh-title"><b>报 价 单</b><span>QUOTATION · 预览单（保存后生成单号）</span></div>'
      + '</div>'
      + '<div class="sh-meta">'
      + '<div><span>客户</span><b>' + esc(meta.customer) + '</b></div>'
      + '<div><span>项目 / 询价号</span><b>' + esc(meta.project) + '</b></div>'
      + '<div><span>报价日期</span><b>' + esc(dateStr) + '</b></div>'
      + '<div><span>有效期至</span><b>' + esc(validStr) + '</b></div>'
      + '<div><span>币种</span><b>' + esc(currency) + '</b></div>'
      + '<div><span>贸易条款</span><b>' + esc(meta.incoterm) + '</b></div>'
      + '<div><span>付款条件</span><b>' + esc(meta.payment) + '</b></div>'
      + '<div><span>报价人 / 版本</span><b>预览 · V1</b></div>'
      + '</div>'
      + '<table class="sh-table"><thead><tr><th class="sh-seq">#</th><th>产品描述</th><th class="r">数量</th><th class="r">单重 kg</th><th class="r">含税单价</th><th class="r">金额（不含税）</th><th class="r">交期</th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table>'
      + '<div class="sh-totals"><table>'
      + '<tr><td>不含税金额</td><td>' + priceMoney(sumNet, 2, currency, fxRate) + '</td></tr>'
      + '<tr><td>税额（' + fmt(first.taxPct, 0) + '%）</td><td>' + priceMoney(sumTax, 2, currency, fxRate) + '</td></tr>'
      + '<tr class="grand"><td>含税合计</td><td>' + priceMoney(sumGross, 2, currency, fxRate) + '</td></tr>'
      + '<tr><td>合计大写</td><td class="upper">保存后生成</td></tr>'
      + '</table></div>'
      + '<div class="sh-terms"><div class="cols">'
      + '<div><b>技术要求</b>：执行标准 ' + esc(oneOr(items, 'edition')) + '；材质 ' + esc(oneOr(items, 'material'))
      + '；压力等级 ' + esc(oneOr(items, 'ratingLabel')) + '；检测 / 表面 / 包装见明细；重量来源 ' + esc(weightText) + '。</div>'
      + '<div><b>商务条款</b>：交期约 ' + maxLead + ' 天；报价有效期至 ' + esc(validStr) + '；付款 ' + esc(meta.payment)
      + '；运输 ' + esc(meta.incoterm) + '；质保 12 个月；报价含税 ' + fmt(first.taxPct, 0) + '%。'
      + (currency === 'USD' ? ' 汇率基准 1 USD = ' + fmt(fxRate, 2) + ' CNY。' : '') + '</div>'
      + '</div><div class="sh-note"><b>说明</b>：本报价单为系统预览，材料单价、加工费、利润率、折扣与汇率为示例参数，不构成正式报价或合同承诺。'
      + '单号、版本、成本快照与状态在保存后由后端生成。' + (sources.indexOf('geometry') >= 0 ? ' 本单含几何估算重量（C 级），最终材料用量与价格以实际过磅 / 图纸复核为准。' : '')
      + '</div></div>'
      + '<div class="sh-sign"><div>报价人：____________________</div><div>审批：____________________</div><div>客户确认：____________________</div><div>日期：____________________</div></div>'
      + '<div class="sh-foot">数据来源：法兰标准库（ASME B16.5 / B16.47 / EN 1092-1 / JIS B2220，事实性表格整理）· 重量规则 '
      + esc(first.weightRule || 'w0.3') + ' · 第 1 页 / 共 1 页</div>'
      + '</div>';
  }

  function renderSheet() {
    var preview = $('quotePreview');
    if (!preview) { return; }
    preview.innerHTML = sheetHtml();
  }

  function clearCreateError() {
    var box = $('createError');
    if (box) { box.hidden = true; box.innerHTML = ''; }
  }

  function showCreateError(info) {
    var box = $('createError');
    if (!box) { toast((info && info.message) || '保存失败', true); return; }
    var message = (info && info.message) || '保存失败（未知错误）';
    var code = (info && info.error) || 'error';
    var field = info && info.field;
    box.hidden = false;
    box.innerHTML = '保存失败：' + esc(message)
      + ' <span class="mono">[' + esc(code) + (field ? ' · ' + esc(field) : '') + ']</span>';
    if (box.scrollIntoView) { box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  }

  function setSaving(saving) {
    var btn = $('saveBtn');
    if (!btn) { return; }
    btn.disabled = !!saving || !state.items.length;
    btn.textContent = saving ? '保存中…' : '保存报价单';
  }

  function saveQuote() {
    if (DATA.can_create === false) {
      toast('当前角色没有建单权限，保存按钮已禁用', true);
      return;
    }
    if (!state.items.length) {
      toast('先加入报价项，再保存报价单', true);
      return;
    }
    clearCreateError();
    var currencies = {};
    state.items.forEach(function (item) {
      currencies[(item.payload && item.payload.currency) || 'CNY'] = true;
    });
    if (Object.keys(currencies).length > 1) {
      showCreateError({ error: 'invalid_payload', message: '同一报价单的币种必须一致，请先清空清单后统一币种再录入。', field: 'currency' });
      return;
    }
    var meta = quoteMeta();
    var notes = ['项目 / 询价号：' + meta.project];
    if (!meta.customerCode) { notes.push('客户（未建档）：' + meta.customer); }
    var payload = {
      incoterm: meta.incoterm,
      payment_terms: meta.payment,
      notes: notes.join('；'),
      items: state.items.map(function (item) { return item.payload; })
    };
    if (meta.customerCode) { payload.customer = meta.customerCode; }
    setSaving(true);
    fetch(DATA.endpoints.create, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': DATA.csrf_token || '',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        var contentType = response.headers.get('content-type') || '';
        if (contentType.indexOf('application/json') < 0) {
          var httpErr = new Error('保存接口返回非 JSON（HTTP ' + response.status + '）');
          httpErr.quoteError = { ok: false, error: 'http_' + response.status, message: httpErr.message };
          throw httpErr;
        }
        return response.json().then(function (body) {
          if (!response.ok || !body || body.ok === false) {
            var apiErr = new Error((body && body.message) || ('HTTP ' + response.status));
            apiErr.quoteError = body || { ok: false, error: 'http_' + response.status };
            throw apiErr;
          }
          return body;
        });
      })
      .then(function (body) {
        setSaving(false);
        state.items = [];
        renderList();
        renderSheet();
        var base = DATA.endpoints.workbench || '/quote/';
        if (base.charAt(base.length - 1) !== '/') { base += '/'; }
        var target = base + encodeURIComponent(body.number) + '/';
        toast('已保存：' + body.number + ' · 正在打开详情');
        window.setTimeout(function () { window.location.assign(target); }, 450);
      })
      .catch(function (err) {
        setSaving(false);
        showCreateError((err && err.quoteError) || { error: 'network_error', message: (err && err.message) || '网络错误' });
      });
  }

  function exportCsv() {
    if (!state.items.length) { return; }
    var meta = quoteMeta();
    var lines = [];
    var firstItem = state.items[0];
    var currency = firstItem.currency || 'CNY';
    var fxRate = firstItem.fx || quoteFxRate();
    var sym = symbolFor(currency);
    function row(cells) {
      lines.push(cells.map(function (cell) {
        var text = String(cell == null ? '' : cell).replace(/"/g, '""');
        return '"' + text + '"';
      }).join(','));
    }
    row(['报价单', '预览单（保存后生成单号）']);
    row(['客户', meta.customer]);
    row(['项目 / 询价号', meta.project]);
    row(['报价日期', DATA.today || '']);
    row(['有效期至', DATA.valid_until || '']);
    row(['币种', valField('currencySel') || 'CNY']);
    row(['贸易条款', meta.incoterm]);
    row(['付款条件', meta.payment]);
    row([]);
    row(['序号', '产品描述', '材质', '数量', '单重 kg', '重量来源', '含税单价', '不含税金额', '税额', '含税金额', '交期（天）', '备注']);
    var sumNet = 0; var sumTax = 0; var sumGross = 0;
    state.items.forEach(function (item, index) {
      sumNet += numOf(item.amountNet, 0);
      sumTax += numOf(item.taxAmount, 0);
      sumGross += numOf(item.totalGross, 0);
      row([
        index + 1, item.description, item.material, item.qty,
        fmt(item.net, 3), (item.source === 'geometry' ? '几何估算 C' : '标准参考 B'),
        Number((item.unitGross / (item.currency === 'USD' ? item.fx : 1)) || 0).toFixed(2),
        Number((item.amountNet / (item.currency === 'USD' ? item.fx : 1)) || 0).toFixed(2),
        Number((item.taxAmount / (item.currency === 'USD' ? item.fx : 1)) || 0).toFixed(2),
        Number((item.totalGross / (item.currency === 'USD' ? item.fx : 1)) || 0).toFixed(2),
        item.lead == null ? '' : item.lead, meta.incoterm
      ]);
    });
    row([]);
    row(['不含税合计', priceMoney(sumNet, 2, currency, fxRate)]);
    row(['税额', priceMoney(sumTax, 2, currency, fxRate)]);
    row(['含税合计', priceMoney(sumGross, 2, currency, fxRate)]);
    row(['币种 / 汇率', currency + ' / 1 USD = ' + fmt(fxRate, 2) + ' CNY']);
    row(['说明', '示例参数，不构成正式报价；成本、价格与 p/T 校验以后端 /quote/calc/ 结果为准。']);
    var csv = '\uFEFF' + lines.join('\r\n');
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    var stamp = (DATA.today || '').replace(/-/g, '') || 'preview';
    link.href = url;
    link.download = '法兰报价单-预览-' + stamp + '.csv';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1200);
    toast('CSV 已导出（' + state.items.length + ' 项）');
  }

  function printSheet() {
    if (!state.items.length) {
      toast('先加入报价项，再打印 A4 报价单', true);
      return;
    }
    renderSheet();
    if (typeof window.print === 'function') {
      window.print();
    } else {
      toast('当前浏览器不支持直接打印，请使用 A4 打印页', true);
    }
  }

  function resetDefaults() {
    state.std = DATA.defaults.std;
    state.type = DATA.defaults.type;
    state.rating = DATA.defaults.rating;
    state.size = DATA.defaults.size;
    state.material = DATA.defaults.material;
    if ($('qtyIn')) { $('qtyIn').value = DATA.defaults.qty; }
    if ($('tempIn')) { $('tempIn').value = DATA.defaults.design_temp_c; }
    if ($('pressIn')) { $('pressIn').value = DATA.defaults.design_pressure_bar; }
    if ($('surfaceSel')) { $('surfaceSel').value = DATA.defaults.surface; }
    if ($('packSel')) { $('packSel').value = DATA.defaults.packaging; }
    if ($('freightIn')) { $('freightIn').value = DATA.defaults.freight_per_kg; }
    if ($('discountIn')) { $('discountIn').value = DATA.defaults.discount_pct; }
    if ($('currencySel')) { $('currencySel').value = DATA.defaults.currency; }
    if ($('fxIn')) { $('fxIn').value = DATA.defaults.fx_rate; }
    if ($('yieldIn')) { $('yieldIn').value = DEFAULT_PARAMS.yield; }
    if ($('machIn')) { $('machIn').value = DEFAULT_PARAMS.machining_factor; }
    if ($('mgmtIn')) { $('mgmtIn').value = DEFAULT_PARAMS.management_pct; }
    if ($('profitIn')) { $('profitIn').value = DEFAULT_PARAMS.profit_pct; }
    if ($('taxIn')) { $('taxIn').value = DEFAULT_PARAMS.tax_pct; }
    if ($('matPriceOverride')) { $('matPriceOverride').checked = false; }
    if ($('matPriceIn')) { $('matPriceIn').readOnly = true; }
    if ($('machiningRateIn')) { $('machiningRateIn').value = ''; }
    ['inspPMI', 'inspUT', 'inspHydro', 'inspHardness', 'inspThird'].forEach(function (id) {
      if ($(id)) { $(id).checked = false; }
    });
    if ($('qCustomer')) {
      $('qCustomer').value = '';
      var hint = $('customerHint');
      if (hint) { hint.textContent = '未选客户：按手填折扣报价；选择客户后自动带入默认折扣、币种与账期。'; }
    }
    state.apiDown = false;
    state.lastApiError = null;
    refreshCascade();
    updatePriceSourceTags();
    toast('已恢复契约夹具默认参数');
  }

  /* ------------------------------------------------------------------ */
  /* 事件绑定 & 初始化                                                    */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    var stdSel = $('stdSel');
    if (stdSel) {
      stdSel.addEventListener('change', function () {
        state.std = this.value;
        var std = standardObj();
        state.type = std && std.types.length ? std.types[0] : state.type;
        state.rating = std && std.ratings.length ? std.ratings[0][0] : state.rating;
        state.material = std && std.materials.length ? std.materials[0] : state.material;
        state.size = '';
        var overrideBox = $('matPriceOverride');
        if (overrideBox) { overrideBox.checked = false; }
        refreshCascade();
      });
    }
    ['typeSel', 'ratingSel'].forEach(function (id) {
      var el = $(id);
      if (!el) { return; }
      el.addEventListener('change', function () {
        state[id === 'typeSel' ? 'type' : 'rating'] = this.value;
        state.size = '';
        refreshSizes();
        renderSpec();
        scheduleCalc();
      });
    });
    var sizeSel = $('sizeSel');
    if (sizeSel) {
      sizeSel.addEventListener('change', function () {
        state.size = this.value;
        renderSpec();
        scheduleCalc();
      });
    }
    var matSel = $('matSel');
    if (matSel) {
      matSel.addEventListener('change', function () {
        state.material = this.value;
        var overrideBox = $('matPriceOverride');
        if (overrideBox) { overrideBox.checked = false; }
        syncMaterialPrice();
        scheduleCalc();
      });
    }
    ['qtyIn', 'tempIn', 'pressIn', 'freightIn', 'discountIn', 'yieldIn', 'machIn',
      'mgmtIn', 'profitIn', 'taxIn', 'fxIn', 'surfaceSel', 'packSel', 'currencySel',
      'inspPMI', 'inspUT', 'inspHydro', 'inspHardness', 'inspThird'].forEach(function (id) {
      var el = $(id);
      if (el) { el.addEventListener('change', scheduleCalc); }
    });
    var overrideBox = $('matPriceOverride');
    if (overrideBox) {
      overrideBox.addEventListener('change', function () {
        var input = $('matPriceIn');
        if (this.checked) {
          var base = materialBasePrice();
          if (input) {
            input.readOnly = false;
            if (!isNum(input.value) || Number(input.value) <= 0) {
              input.value = base != null ? base : '';
            }
          }
        } else if (input) {
          input.readOnly = true;
          var baseValue = materialBasePrice();
          input.value = baseValue != null ? baseValue : '—';
        }
        syncMaterialPrice();
        scheduleCalc();
      });
    }
    var resetMaterialBtn = $('matPriceResetBtn');
    if (resetMaterialBtn) {
      resetMaterialBtn.addEventListener('click', function () {
        setMaterialOverride(false, true);
        toast('已重置为价格库材料价');
      });
    }
    var materialPriceInput = $('matPriceIn');
    if (materialPriceInput) {
      materialPriceInput.addEventListener('input', function () {
        if (!isMaterialOverride()) { return; }
        syncMaterialPrice();
        scheduleCalc();
      });
    }
    var machiningRateInput = $('machiningRateIn');
    if (machiningRateInput) {
      machiningRateInput.addEventListener('input', function () {
        updatePriceSourceTags();
        scheduleCalc();
      });
      machiningRateInput.addEventListener('change', scheduleCalc);
    }
    var addBtn = $('addBtn');
    if (addBtn) { addBtn.addEventListener('click', addCurrent); }
    var clearBtn = $('clearBtn');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        if (!state.items.length) { return; }
        state.items = [];
        renderList();
        renderSheet();
        toast('清单已清空');
      });
    }
    var csvBtn = $('csvBtn');
    if (csvBtn) { csvBtn.addEventListener('click', exportCsv); }
    var printBtn = $('printBtn');
    if (printBtn) { printBtn.addEventListener('click', printSheet); }
    var saveBtn = $('saveBtn');
    if (saveBtn) { saveBtn.addEventListener('click', saveQuote); }
    var resetBtn = $('resetBtn');
    if (resetBtn) { resetBtn.addEventListener('click', resetDefaults); }
    ['qCustomer', 'qProject', 'qIncoterm', 'qPayment'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('input', function () {
          if (state.items.length) { renderSheet(); }
        });
        el.addEventListener('change', function () {
          if (id === 'qCustomer') { applyCustomerDefaults(); }
          if (state.items.length) { renderSheet(); }
        });
      }
    });
    if ($('qCustomer') && DATA.customers && DATA.customers.length === 0) {
      setText('customerHint', '价格库暂无客户；可在 Django Admin 维护后刷新，或直接按手填折扣报价。');
    }
    document.addEventListener('click', function (event) {
      var target = event.target;
      if (target && target.getAttribute) {
        var id = target.getAttribute('data-del');
        if (id) { removeItem(Number(id)); }
      }
    });
  }

  function init() {
    var std = standardObj();
    if (std) {
      state.std = std.id;
      if (!std.types.some(function (code) { return code === state.type; })) { state.type = std.types[0]; }
      if (!std.ratings.some(function (pair) { return pair[0] === state.rating; })) { state.rating = std.ratings[0][0]; }
      if (!std.materials.some(function (code) { return code === state.material; })) { state.material = std.materials[0]; }
    }
    refreshTypes();
    refreshRatings();
    refreshMaterials();
    refreshSizes();
    syncMaterialPrice();
    renderSpec();
    updatePriceSourceTags();
    bindEvents();
    requestCalc();
  }

  window.quoteWorkbench = {
    state: state,
    payload: buildPayload,
    calcKey: calcKey,
    requestCalc: requestCalc,
    addCurrent: addCurrent
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ==========================================================================
   状态动作（详情页 / 列表页共用）
   按钮由服务端按当前状态渲染，data-status-url 指向 /quote/<no>/status/；
   成功刷新页面，失败按契约 §8 原样展示 {ok:false,error,message,field}。
   ========================================================================== */
(function () {
  'use strict';

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) { return meta.content; }
    var app = document.querySelector('.quote-app');
    if (app && app.getAttribute('data-csrf')) { return app.getAttribute('data-csrf'); }
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function statusToast(message, bad) {
    var el = document.getElementById('toast');
    if (!el) { return; }
    el.textContent = message;
    el.className = 'toast show' + (bad ? ' bad' : '');
    window.clearTimeout(statusToast._timer);
    statusToast._timer = window.setTimeout(function () { el.className = 'toast'; }, 3000);
  }

  function showStatusError(info) {
    var box = document.getElementById('statusError');
    var message = (info && info.message) || '状态操作失败';
    var code = (info && info.error) || 'error';
    var field = info && info.field;
    if (!box) {
      statusToast(message + ' [' + code + ']', true);
      return;
    }
    box.hidden = false;
    box.innerHTML = '状态操作失败：' + String(message).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    }) + ' <span class="mono">[' + code + (field ? ' · ' + field : '') + ']</span>';
    if (box.scrollIntoView) { box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  }

  function clearStatusError() {
    var box = document.getElementById('statusError');
    if (box) { box.hidden = true; box.innerHTML = ''; }
  }

  function handleClick(event) {
    var target = event.target;
    var button = target && target.closest ? target.closest('[data-status-action]') : null;
    if (!button || button.disabled) { return; }
    event.preventDefault();

    var action = button.getAttribute('data-status-action');
    var label = button.getAttribute('data-action-label') || action;
    var url = button.getAttribute('data-status-url');
    var number = button.getAttribute('data-quote-number') || '';
    var needsConfirm = button.getAttribute('data-confirm') === '1';
    var needsComment = button.getAttribute('data-comment') === '1';

    if (!url) { return; }
    if (needsConfirm && !window.confirm('确定要对报价单 ' + number + ' 执行「' + label + '」吗？此操作会记录审批日志。')) {
      return;
    }
    var comment = '';
    if (needsComment) {
      comment = window.prompt('「' + label + '」意见（可选，留空跳过）：', '') || '';
    }

    var originalText = button.textContent;
    button.disabled = true;
    button.textContent = '处理中…';
    clearStatusError();

    fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken(),
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify({ action: action, comment: comment })
    })
      .then(function (response) {
        var contentType = response.headers.get('content-type') || '';
        if (contentType.indexOf('application/json') < 0) {
          var httpErr = new Error('状态接口返回非 JSON（HTTP ' + response.status + '）');
          httpErr.quoteError = { ok: false, error: 'http_' + response.status, message: httpErr.message };
          throw httpErr;
        }
        return response.json().then(function (body) {
          if (!response.ok || !body || body.ok === false) {
            var apiErr = new Error((body && body.message) || ('HTTP ' + response.status));
            apiErr.quoteError = body || { ok: false, error: 'http_' + response.status };
            throw apiErr;
          }
          return body;
        });
      })
      .then(function (body) {
        statusToast('已' + label + '：' + (body.number || number) + ' → ' + (body.status || '已更新'));
        window.setTimeout(function () { window.location.reload(); }, 400);
      })
      .catch(function (err) {
        showStatusError((err && err.quoteError) || { error: 'network_error', message: (err && err.message) || '网络错误' });
        button.disabled = false;
        button.textContent = originalText;
      });
  }

  document.addEventListener('click', handleClick);
})();
