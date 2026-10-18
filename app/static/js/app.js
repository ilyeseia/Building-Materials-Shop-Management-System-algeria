/* Building Materials Management — own JavaScript (no CDN, CSP-friendly).
   All money/quantity maths done here is a *preview* only: the server recomputes
   every total in Decimal and is the single source of truth. */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  function parseNumber(raw) {
    if (raw === null || raw === undefined) return 0;
    var text = String(raw).trim().replace(/\u00a0/g, "").replace(/\s/g, "");
    if (text.indexOf(",") !== -1 && text.indexOf(".") !== -1) {
      if (text.lastIndexOf(",") > text.lastIndexOf(".")) {
        text = text.replace(/\./g, "").replace(",", ".");
      } else {
        text = text.replace(/,/g, "");
      }
    } else if (text.indexOf(",") !== -1) {
      text = text.replace(",", ".");
    }
    var value = parseFloat(text);
    return isNaN(value) ? 0 : value;
  }

  function formatNumber(value, places) {
    if (!isFinite(value)) value = 0;
    return value.toFixed(places === undefined ? 2 : places);
  }

  function linePayload() {
    var holder = document.getElementById("line-data");
    if (!holder || !holder.dataset.products) return [];
    try {
      return JSON.parse(holder.dataset.products);
    } catch (error) {
      return [];
    }
  }

  function productById(products, id) {
    for (var i = 0; i < products.length; i++) {
      if (String(products[i].id) === String(id)) return products[i];
    }
    return null;
  }

  function setupLines() {
    var table = document.querySelector("table.lines");
    if (!table) return;
    var body = table.querySelector("tbody");
    var products = linePayload();

    function recalc() {
      var subtotal = 0;
      var priced = false;
      var rows = body.querySelectorAll("tr.line-row");
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        var qtyInput = row.querySelector('[data-line="quantity"]');
        var priceInput = row.querySelector('[data-line="unit_price"]');
        var discountInput = row.querySelector('[data-line="discount"]');
        var qty = parseNumber(qtyInput ? qtyInput.value : 0);
        var net = null;
        if (priceInput) {
          var price = parseNumber(priceInput.value);
          var discount = parseNumber(discountInput ? discountInput.value : 0);
          var gross = qty * price;
          net = gross - (gross * discount / 100);
          priced = true;
        }
        var cell = row.querySelector('[data-line="total"]');
        if (cell && net !== null) cell.textContent = formatNumber(net, 2);
        if (net !== null) subtotal += net;
      }
      var subtotalCell = document.getElementById("preview-subtotal");
      if (subtotalCell && priced) subtotalCell.textContent = formatNumber(subtotal, 2);
    }

    function fillProducts(select) {
      select.innerHTML = '<option value="">—</option>';
      for (var i = 0; i < products.length; i++) {
        var option = document.createElement("option");
        option.value = products[i].id;
        option.textContent = products[i].name + (products[i].sku ? " — " + products[i].sku : "");
        select.appendChild(option);
      }
    }

    function fillUnits(row, product) {
      var unitSelect = row.querySelector('[data-line="unit"]');
      if (!unitSelect) return;
      unitSelect.innerHTML = "";
      if (!product) return;
      for (var i = 0; i < product.units.length; i++) {
        var option = document.createElement("option");
        option.value = product.units[i].id;
        option.textContent = product.units[i].code;
        unitSelect.appendChild(option);
      }
    }

    function applyProduct(row, product) {
      fillUnits(row, product);
      var priceInput = row.querySelector('[data-line="unit_price"]');
      var list = document.getElementById("price-list");
      var priceList = list ? list.value : "retail";
      if (priceInput && product) {
        var value = product.price;
        if (priceList === "wholesale" && product.wholesale) value = product.wholesale;
        if (priceList === "contractor" && product.contractor) value = product.contractor;
        priceInput.value = value;
      }
      var hint = row.querySelector('[data-line="available"]');
      if (hint && product) {
        hint.textContent = product.stock_unit ? product.available + " " + product.stock_unit : product.available;
      }
    }

    function bindRow(row) {
      var productSelect = row.querySelector('[data-line="product"]');
      if (productSelect) {
        productSelect.addEventListener("change", function () {
          applyProduct(row, productById(products, productSelect.value));
          recalc();
        });
      }
      row.querySelectorAll('input').forEach(function (input) {
        input.addEventListener("input", recalc);
      });
      var remove = row.querySelector(".remove-line");
      if (remove) {
        remove.addEventListener("click", function () {
          var rows = body.querySelectorAll("tr.line-row");
          if (rows.length <= 1) {
            row.querySelectorAll("input").forEach(function (input) { input.value = ""; });
            row.querySelectorAll("select").forEach(function (select) { select.value = ""; });
          } else {
            row.parentNode.removeChild(row);
          }
          recalc();
        });
      }
    }

    body.querySelectorAll("tr.line-row").forEach(function (row) {
      fillProducts(row.querySelector('[data-line="product"]'));
      bindRow(row);
    });

    var addButton = document.getElementById("add-line");
    if (addButton) {
      addButton.addEventListener("click", function () {
        var template = body.querySelector("tr.line-row");
        if (!template) return;
        var clone = template.cloneNode(true);
        clone.querySelectorAll("input").forEach(function (input) {
          if (input.type === "checkbox") input.checked = false;
          else input.value = input.dataset.default || "";
        });
        clone.querySelectorAll("select").forEach(function (select) { select.value = ""; });
        var totalCell = clone.querySelector('[data-line="total"]');
        if (totalCell) totalCell.textContent = "0.00";
        var hint = clone.querySelector('[data-line="available"]');
        if (hint) hint.textContent = "";
        body.appendChild(clone);
        fillProducts(clone.querySelector('[data-line="product"]'));
        bindRow(clone);
        recalc();
      });
    }

    recalc();
  }

  function setupConfirm() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.dataset.confirm)) {
          event.preventDefault();
        }
      });
    });
  }

  function setupPrint() {
    document.querySelectorAll("[data-print]").forEach(function (button) {
      button.addEventListener("click", function () { window.print(); });
    });
  }

  function setupAutoSubmit() {
    document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
      select.addEventListener("change", function () {
        if (select.form) select.form.submit();
      });
    });
  }

  function setupPaymentToggle() {
    var direction = document.getElementById("direction-field");
    if (!direction) return;
    function update() {
      var inbound = direction.value === "inbound";
      var customer = document.querySelectorAll("[data-party='customer']");
      var supplier = document.querySelectorAll("[data-party='supplier']");
      customer.forEach(function (el) { el.style.display = inbound ? "" : "none"; });
      supplier.forEach(function (el) { el.style.display = inbound ? "none" : ""; });
    }
    direction.addEventListener("change", update);
    update();
  }

  ready(function () {
    setupLines();
    setupConfirm();
    setupPrint();
    setupAutoSubmit();
    setupPaymentToggle();
  });
})();
