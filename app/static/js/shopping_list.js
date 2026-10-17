(function () {
  const getLang = () => (document.cookie.includes("lang=en") ? "en" : "ko");
  const currentLang = getLang();
  const tk = (ko, en) => (currentLang === "en" ? en : ko);

  // Toast Notification
  const toast = document.getElementById("toast");
  let toastTimer = null;
  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    toast.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.classList.remove("is-visible");
      setTimeout(() => {
        toast.hidden = true;
      }, 200);
    }, 2500);
  }

  // Share Page / Copy Link
  const shareBtn = document.querySelector("[data-share-page]");
  if (shareBtn) {
    shareBtn.addEventListener("click", async () => {
      const shareData = {
        title: document.title,
        url: window.location.href,
      };
      if (navigator.share) {
        try {
          await navigator.share(shareData);
          return;
        } catch (e) {
          if (e.name === "AbortError") return;
        }
      }
      try {
        await navigator.clipboard.writeText(window.location.href);
        showToast(tk("링크가 클립보드에 복사되었습니다!", "Link copied to clipboard!"));
      } catch (err) {
        showToast(tk("링크 복사에 실패했습니다.", "Failed to copy link."));
      }
    });
  }

  // Floating Back to Top Button
  const backToTop = document.getElementById("back-to-top");
  if (backToTop) {
    window.addEventListener(
      "scroll",
      () => {
        if (window.scrollY > 320) {
          backToTop.classList.add("is-visible");
        } else {
          backToTop.classList.remove("is-visible");
        }
      },
      { passive: true }
    );
    backToTop.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  // Filter Auto-Submit & Retailer Presets
  const filterForm = document.querySelector("[data-filter-form]");
  if (filterForm) {
    filterForm.addEventListener("change", (event) => {
      if (event.target.matches("[data-auto-submit]")) {
        filterForm.requestSubmit();
      }
    });

    filterForm.addEventListener("click", (event) => {
      const button = event.target.closest("[data-retailer-preset]");
      if (!button) {
        return;
      }
      const preset = button.dataset.retailerPreset;
      const checkboxes = Array.from(filterForm.querySelectorAll('input[name="retailer"]'));
      const defaultRetailers = new Set(
        (button.closest("[data-default-retailers]")?.dataset.defaultRetailers || "")
          .split(",")
          .filter(Boolean)
      );
      checkboxes.forEach((checkbox) => {
        checkbox.checked =
          preset === "all" || (preset === "default" && defaultRetailers.has(checkbox.value));
      });
      filterForm.requestSubmit();
    });
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replaceAll("`", "&#096;");
  }

  // Shared Modal Controller Helper
  function createModalController(modalEl, closeBtnEl, onOpen) {
    function open() {
      if (!modalEl) return;
      modalEl.hidden = false;
      document.body.classList.add("modal-open");
      closeBtnEl?.focus();
      if (onOpen) onOpen();
    }
    function close() {
      if (!modalEl) return;
      modalEl.hidden = true;
      document.body.classList.remove("modal-open");
    }
    closeBtnEl?.addEventListener("click", close);
    modalEl?.addEventListener("click", (e) => {
      if (e.target === modalEl) close();
    });
    return { open, close };
  }

  // 1. Price History Modal
  const historyModal = document.querySelector("[data-history-modal]");
  const historyModalBody = document.querySelector("[data-history-modal-body]");
  const historyModalClose = document.querySelector("[data-history-modal-close]");
  const { open: openHistoryModal, close: closeHistoryModal } = createModalController(
    historyModal,
    historyModalClose
  );

  async function loadPriceHistory(title, brand, currentPrice) {
    openHistoryModal();
    if (!historyModalBody) return;
    historyModalBody.innerHTML = `<p class="tracker-loading">${tk("가격 변동 이력을 불러오는 중...", "Loading price history...")}</p>`;

    try {
      const url = new URL("/api/price-history", window.location.origin);
      url.searchParams.append("q", title);
      if (brand) url.searchParams.append("brand", brand);
      if (currentPrice) url.searchParams.append("price", currentPrice);

      const res = await fetch(url);
      if (!res.ok) throw new Error("Price history request failed");
      const data = await res.json();
      renderPriceHistory(data);
    } catch (err) {
      historyModalBody.innerHTML = `<p class="tracker-error">${tk("가격 이력을 불러오지 못했습니다.", "Failed to load price history.")}</p>`;
    }
  }

  function renderPriceHistory(data) {
    const isAtl = data.is_all_time_low;
    const history = data.history || [];
    const avgPrice = data.average_price ?? data.avg_price;

    let statsHtml = `
      <div class="tracker-stats-grid">
        <div class="tracker-stat ${isAtl ? "is-best" : ""}">
          <span class="label">${tk("역대 최저가", "All-time Low")}</span>
          <strong>${data.all_time_low ? data.all_time_low.toFixed(2) + " €" : "-"}</strong>
          ${isAtl ? `<span class="badge-mini">${tk("현재 최저가", "Best price now")}</span>` : ""}
        </div>
        <div class="tracker-stat">
          <span class="label">${tk("평균 가격", "Average Price")}</span>
          <strong>${avgPrice ? avgPrice.toFixed(2) + " €" : "-"}</strong>
        </div>
        <div class="tracker-stat">
          <span class="label">${tk("현재/최근가", "Current Deal")}</span>
          <strong>${data.current_price ? data.current_price.toFixed(2) + " €" : "-"}</strong>
        </div>
        <div class="tracker-stat">
          <span class="label">${tk("기록 횟수", "Recorded")}</span>
          <strong>${data.records_count || history.length} ${tk("회", "times")}</strong>
        </div>
      </div>
    `;

    let historyTable = "";
    if (history.length) {
      historyTable = `
        <div class="tracker-history-table-wrap">
          <h4>${tk("가격 관측 이력 (수집 시점 & 출처)", "Price Observations & Sources")}</h4>
          <table class="tracker-history-table">
            <thead>
              <tr>
                <th>${tk("수집일", "Date")}</th>
                <th>${tk("매장", "Store")}</th>
                <th>${tk("가격", "Price")}</th>
                <th>${tk("할인율", "Discount")}</th>
                <th>${tk("전단지", "Flyer")}</th>
              </tr>
            </thead>
            <tbody>
              ${history
                .map(
                  (item) => `
                <tr>
                  <td>${item.recorded_at ? item.recorded_at.substring(0, 10) : "-"}</td>
                  <td><span class="retailer-badge">${escapeHtml(item.retailer)}</span></td>
                  <td><strong>${item.price ? item.price.toFixed(2) + " €" : "-"}</strong></td>
                  <td>${item.discount_percent ? item.discount_percent + "%" : "-"}</td>
                  <td><a href="${escapeAttribute(item.source_url || "#")}" target="_blank" rel="noopener noreferrer">Prospekt ↗</a></td>
                </tr>
              `
                )
                .join("")}
            </tbody>
          </table>
        </div>
      `;
    } else {
      historyTable = `<p class="empty-note">${tk("아직 충분한 관측 이력이 없습니다. 이번 주 첫 수집 상품입니다.", "First observation recorded this week.")}</p>`;
    }

    historyModalBody.innerHTML = `
      <div class="tracker-history-header">
        <h3>${escapeHtml(data.product_name || "")}</h3>
        ${data.brand ? `<p class="brand">${escapeHtml(data.brand)}</p>` : ""}
      </div>
      ${statsHtml}
      ${historyTable}
    `;
  }

  // 2. Store Comparison Modal
  const compareModal = document.querySelector("[data-compare-modal]");
  const compareModalBody = document.querySelector("[data-compare-modal-body]");
  const compareModalClose = document.querySelector("[data-compare-modal-close]");
  const compareForm = document.querySelector("[data-compare-form]");
  const compareSearchInput = document.querySelector("[data-compare-search-input]");
  const { open: openCompareModal, close: closeCompareModal } = createModalController(
    compareModal,
    compareModalClose
  );

  document.querySelector("[data-open-compare-modal]")?.addEventListener("click", () => {
    openCompareModal();
    if (compareSearchInput) compareSearchInput.focus();
  });

  compareForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = (compareSearchInput?.value || "").trim();
    if (query) runCompare(query);
  });

  async function runCompare(query) {
    openCompareModal();
    if (compareSearchInput) compareSearchInput.value = query;
    if (!compareModalBody) return;
    compareModalBody.innerHTML = `<p class="tracker-loading">${tk("독일 매장별 가격을 비교하는 중...", "Comparing stores across Germany...")}</p>`;

    try {
      const zipCode = document.querySelector('input[name="zip_code"]')?.value || "14195";
      const week = document.querySelector('input[name="week"]')?.value || "current";
      const url = new URL("/api/compare", window.location.origin);
      url.searchParams.append("q", query);
      url.searchParams.append("zip_code", zipCode);
      url.searchParams.append("week", week);

      const res = await fetch(url);
      if (!res.ok) throw new Error("Compare failed");
      const data = await res.json();
      renderStoreComparison(data);
    } catch (err) {
      compareModalBody.innerHTML = `<p class="tracker-error">${tk("비교 데이터를 가져오지 못했습니다.", "Failed to load store comparison.")}</p>`;
    }
  }

  function renderStoreComparison(data) {
    const stores = data.stores || [];
    if (!stores.length) {
      compareModalBody.innerHTML = `
        <div class="empty-state">
          <p><strong>"${escapeHtml(data.query)}"</strong> ${tk("에 대한 매장별 할인 상품을 찾지 못했습니다.", "No matching store offers found.")}</p>
        </div>
      `;
      return;
    }

    let summaryHtml = "";
    if (data.cheapest_store) {
      summaryHtml = `
        <div class="compare-summary-banner">
          <div class="csb-main">
            <span class="csb-badge">${tk("최저가 매장", "Cheapest Store")}</span>
            <strong>${escapeHtml(data.cheapest_store.retailer)}: ${data.cheapest_store.price.toFixed(2)} €</strong>
          </div>
          ${
            data.potential_savings > 0
              ? `
            <div class="csb-savings">
              ${tk("최고가 매장 대비", "Savings vs Highest")}: <strong>${data.potential_savings.toFixed(2)} € (${data.savings_percent}%) ${tk("절약", "off")}</strong>
            </div>
          `
              : ""
          }
        </div>
      `;
    }

    const cardsHtml = stores
      .map((s, idx) => {
        const isCheapest = idx === 0 && s.price !== null;
        return `
        <div class="compare-store-card ${isCheapest ? "is-best" : ""}">
          <div class="csc-header">
            <span class="retailer-badge">${escapeHtml(s.retailer)}</span>
            ${isCheapest ? `<span class="badge-mini">${tk("최저가", "Best")}</span>` : ""}
          </div>
          <h4>${escapeHtml(s.title)}</h4>
          <div class="csc-price-row">
            <strong>${s.price !== null ? s.price.toFixed(2) + " €" : "-"}</strong>
            ${s.old_price ? `<s>${s.old_price.toFixed(2)} €</s>` : ""}
            ${s.discount_percent ? `<span class="discount-badge">-${s.discount_percent}%</span>` : ""}
          </div>
          ${s.unit_price ? `<p class="csc-unit">${s.unit_price.toFixed(2)} € / ${escapeHtml(s.unit || "unit")}</p>` : ""}
          <div class="csc-footer">
            <a href="${escapeAttribute(s.source_url || "#")}" target="_blank" rel="noopener noreferrer">Zum Angebot ↗</a>
          </div>
        </div>
      `;
      })
      .join("");

    compareModalBody.innerHTML = `
      <div class="compare-results">
        ${summaryHtml}
        <div class="compare-grid">
          ${cardsHtml}
        </div>
      </div>
    `;
  }

  // Card Event Delegation
  document.addEventListener("click", (e) => {
    const historyBtn = e.target.closest("[data-price-history-btn]");
    if (historyBtn) {
      loadPriceHistory(
        historyBtn.dataset.title,
        historyBtn.dataset.brand,
        historyBtn.dataset.price
      );
      return;
    }

    const compareBtn = e.target.closest("[data-compare-btn]");
    if (compareBtn) {
      runCompare(compareBtn.dataset.title);
    }
  });

  // Global ESC Key Handler
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeHistoryModal();
      closeCompareModal();
    }
  });
})();
