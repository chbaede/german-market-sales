(function () {
  const getLang = () => (typeof document !== "undefined" && document.cookie && document.cookie.includes("lang=en") ? "en" : "ko");
  const currentLang = getLang();
  const tk = (ko, en) => (currentLang === "en" ? en : ko);

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

  function renderScopeBadge(item, lang) {
    if (!item) return "";
    const lTk = (ko, en) => ((lang || currentLang) === "en" ? en : ko);
    const scopeType = item.scope_type;
    const regionVerified = !!item.region_verified;
    const storeName = item.store_name;

    if (scopeType === "store" && regionVerified) {
      return `<span class="scope-badge scope-store" title="${escapeHtml(storeName || lTk("매장 전용", "Store Only"))}">📍 ${escapeHtml(storeName || lTk("매장 전용", "Store Only"))}</span>`;
    }
    if (scopeType === "store" && !regionVerified) {
      return `<span class="scope-badge scope-unverified" title="${escapeHtml(storeName || lTk("매장", "Store"))}">📍 ${escapeHtml(storeName || lTk("매장", "Store"))}</span>`;
    }
    if (scopeType === "region" && regionVerified) {
      return `<span class="scope-badge scope-region" title="${lTk("지역 검증 할인", "Verified Regional Deal")}">🗺️ ${lTk("지역 검증", "Regional")}</span>`;
    }
    if (scopeType === "nationwide" && regionVerified) {
      return `<span class="scope-badge scope-nationwide" title="${lTk("전국 공통 할인", "Nationwide Deal")}">🌐 ${lTk("전국공통", "Nationwide")}</span>`;
    }
    if (scopeType === "nationwide" && !regionVerified) {
      return `<span class="scope-badge scope-unverified" title="${lTk("전국 행사", "Nationwide Offer")}">🌐 ${lTk("전국 행사", "Nationwide Offer")}</span>`;
    }
    if (scopeType === "region" && !regionVerified) {
      return `<span class="scope-badge scope-unverified" title="${lTk("권역 행사", "Territory Offer")}">ℹ️ ${lTk("권역행사", "Territory")}</span>`;
    }
    return `<span class="scope-badge scope-unknown" title="${lTk("적용 범위 미확인", "Unknown Scope")}">❓ ${lTk("미확인", "Unknown")}</span>`;
  }

  function renderPriceHistoryHtml(data, lang) {
    const lTk = (ko, en) => ((lang || currentLang) === "en" ? en : ko);
    const isAtl = data.is_all_time_low;
    const history = data.history || [];
    const unverifiedHistory = data.unverified_history || [];
    const avgPrice = data.average_price ?? data.avg_price;

    let statsHtml = `
      <div class="tracker-stats-grid">
        <div class="tracker-stat ${isAtl ? "is-best" : ""}">
          <span class="label">${lTk("역대 최저가", "All-time Low")}</span>
          <strong>${data.all_time_low ? data.all_time_low.toFixed(2) + " €" : "-"}</strong>
          ${isAtl ? `<span class="badge-mini">${lTk("현재 최저가", "Best price now")}</span>` : ""}
        </div>
        <div class="tracker-stat">
          <span class="label">${lTk("평균 가격", "Average Price")}</span>
          <strong>${avgPrice ? avgPrice.toFixed(2) + " €" : "-"}</strong>
        </div>
        <div class="tracker-stat">
          <span class="label">${lTk("현재/최근가", "Current Deal")}</span>
          <strong>${data.current_price ? data.current_price.toFixed(2) + " €" : "-"}</strong>
        </div>
        <div class="tracker-stat">
          <span class="label">${lTk("기록 횟수", "Recorded")}</span>
          <strong>${data.records_count || history.length} ${lTk("회", "times")}</strong>
        </div>
      </div>
    `;

    let historyTable = "";
    if (history.length) {
      historyTable = `
        <div class="tracker-history-table-wrap">
          <h4>${lTk("가격 관측 이력 (검증된 지역/전국 데이터)", "Price Observations (Verified Regional/Nationwide)")}</h4>
          <table class="tracker-history-table">
            <thead>
              <tr>
                <th>${lTk("수집일", "Date")}</th>
                <th>${lTk("매장 및 범위", "Store & Scope")}</th>
                <th>${lTk("가격", "Price")}</th>
                <th>${lTk("할인율", "Discount")}</th>
                <th>${lTk("전단지", "Flyer")}</th>
              </tr>
            </thead>
            <tbody>
              ${history
                .map(
                  (item) => `
                <tr>
                  <td>${item.recorded_at ? item.recorded_at.substring(0, 10) : "-"}</td>
                  <td>
                    <span class="retailer-badge">${escapeHtml(item.retailer || "")}</span>
                    ${renderScopeBadge(item, lang)}
                  </td>
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
      historyTable = `<p class="empty-note">${lTk("아직 검증된 관측 이력이 없습니다. 이번 주 첫 수집 상품입니다.", "No verified observation history recorded yet.")}</p>`;
    }

    let unverifiedTable = "";
    if (unverifiedHistory.length) {
      unverifiedTable = `
        <div class="tracker-history-table-wrap unverified-history-wrap">
          <h4>${lTk("기타 관측 이력 (참고용)", "Other Observations (Reference Only)")}</h4>
          <p class="unverified-note">
            ${lTk("매장 또는 지역 적용 범위가 다른 관측 데이터로, 위 통계에는 포함되지 않습니다.", "Observations with a different store/regional scope; not included in the stats above.")}
          </p>
          <table class="tracker-history-table">
            <thead>
              <tr>
                <th>${lTk("수집일", "Date")}</th>
                <th>${lTk("매장 및 상태", "Store & Status")}</th>
                <th>${lTk("가격", "Price")}</th>
                <th>${lTk("할인율", "Discount")}</th>
                <th>${lTk("전단지", "Flyer")}</th>
              </tr>
            </thead>
            <tbody>
              ${unverifiedHistory
                .map(
                  (item) => `
                <tr>
                  <td>${item.recorded_at ? item.recorded_at.substring(0, 10) : "-"}</td>
                  <td>
                    <span class="retailer-badge">${escapeHtml(item.retailer || "")}</span>
                    ${renderScopeBadge(item, lang)}
                  </td>
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
    }

    return `
      <div class="tracker-history-header">
        <h3>${escapeHtml(data.product_name || "")}</h3>
        ${data.brand ? `<p class="brand">${escapeHtml(data.brand)}</p>` : ""}
      </div>
      ${statsHtml}
      ${historyTable}
      ${unverifiedTable}
    `;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      renderPriceHistoryHtml,
      renderScopeBadge,
      escapeHtml,
      escapeAttribute,
    };
  }

  if (typeof document === "undefined") {
    return;
  }

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

  // Filter Auto-Submit, Retailer Presets & Regional PLZ Selector
  const filterForm = document.querySelector("[data-filter-form]");
  const locationBar = document.querySelector("[data-location-bar]");
  const plzEditor = document.querySelector("[data-plz-editor]");
  const plzInput = document.querySelector("[data-plz-input]");
  const togglePlzBtn = document.querySelector("[data-toggle-plz-editor]");
  const detectLocationBtn = document.querySelector("[data-detect-location]");
  const locationPlzEl = document.querySelector("[data-location-plz]");
  const locationCityEl = document.querySelector("[data-location-city]");

  function savePlzCookie(plz) {
    if (!plz || !/^\d{4,5}$/.test(plz)) return;
    document.cookie = `plz=${plz}; path=/; max-age=31536000; samesite=Lax`;
  }

  function applyPlzAndSubmit(plz, cityText) {
    const cleaned = String(plz || "").trim();
    if (!/^\d{4,5}$/.test(cleaned)) return;
    savePlzCookie(cleaned);
    if (plzInput) plzInput.value = cleaned;
    if (locationPlzEl) locationPlzEl.textContent = cleaned;
    if (cityText && locationCityEl) locationCityEl.textContent = cityText;
    if (filterForm) {
      filterForm.requestSubmit();
    } else {
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("zip_code", cleaned);
      window.location.assign(nextUrl.toString());
    }
  }

  if (togglePlzBtn && plzEditor) {
    togglePlzBtn.addEventListener("click", () => {
      const willOpen = plzEditor.hidden;
      plzEditor.hidden = !willOpen;
      togglePlzBtn.setAttribute("aria-expanded", willOpen ? "true" : "false");
      if (willOpen && plzInput) {
        plzInput.focus();
        plzInput.select();
      }
    });
  }

  if (filterForm) {
    filterForm.addEventListener("submit", () => {
      if (plzInput && /^\d{4,5}$/.test(plzInput.value.trim())) {
        savePlzCookie(plzInput.value.trim());
      }
    });

    filterForm.addEventListener("change", (event) => {
      if (event.target.matches("[data-auto-submit]")) {
        filterForm.requestSubmit();
      }
    });

    filterForm.addEventListener("click", (event) => {
      const quickBtn = event.target.closest("[data-quick-plz]");
      if (quickBtn) {
        const targetPlz = quickBtn.dataset.quickPlz;
        applyPlzAndSubmit(targetPlz);
        return;
      }

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

  async function detectViaClientIp() {
    try {
      const ipRes = await fetch("https://ipwho.is/", { cache: "no-store" });
      if (ipRes.ok) {
        const ipData = await ipRes.json();
        if (ipData && ipData.success !== false && String(ipData.country_code || "").toUpperCase() === "DE") {
          const locateUrl = new URL("/api/locate", window.location.origin);
          if (ipData.postal) locateUrl.searchParams.set("zip_code", String(ipData.postal));
          if (ipData.city) locateUrl.searchParams.set("city", String(ipData.city));
          if (typeof ipData.latitude === "number" && typeof ipData.longitude === "number") {
            locateUrl.searchParams.set("lat", String(ipData.latitude));
            locateUrl.searchParams.set("lon", String(ipData.longitude));
          }
          const normRes = await fetch(locateUrl);
          if (normRes.ok) {
            return await normRes.json();
          }
        }
      }
    } catch (e) {
      // Fallback to server-side /api/locate
    }
    try {
      const res = await fetch("/api/locate");
      if (res.ok) return await res.json();
    } catch (e) {
      // Ignore
    }
    return null;
  }

  async function detectViaGpsOrIp() {
    if (navigator.geolocation) {
      try {
        const pos = await new Promise((resolve, reject) => {
          navigator.geolocation.getCurrentPosition(resolve, reject, {
            enableHighAccuracy: false,
            timeout: 5000,
            maximumAge: 300000,
          });
        });
        const locateUrl = new URL("/api/locate", window.location.origin);
        locateUrl.searchParams.set("lat", String(pos.coords.latitude));
        locateUrl.searchParams.set("lon", String(pos.coords.longitude));
        const res = await fetch(locateUrl);
        if (res.ok) {
          const data = await res.json();
          if (data && data.detected && data.zip_code) {
            return data;
          }
        }
      } catch (e) {
        // Fall through to IP detection
      }
    }
    return await detectViaClientIp();
  }

  if (detectLocationBtn) {
    detectLocationBtn.addEventListener("click", async () => {
      const originalText = detectLocationBtn.textContent;
      detectLocationBtn.disabled = true;
      detectLocationBtn.textContent = tk("확인중...", "Locating...");
      try {
        const result = await detectViaGpsOrIp();
        if (result && result.detected && result.zip_code) {
          showToast(
            tk(
              `📍 ${result.zip_code} ${result.city || ""} 지역으로 설정합니다.`,
              `📍 Set to ${result.zip_code} ${result.city || ""}.`
            )
          );
          applyPlzAndSubmit(result.zip_code, result.city);
          return;
        }
        showToast(
          tk(
            "독일 내 위치를 확인하지 못했습니다. 우편번호를 직접 입력해 주세요.",
            "Could not detect German location. Please enter PLZ manually."
          )
        );
        if (plzEditor && plzEditor.hidden) {
          plzEditor.hidden = false;
          plzInput?.focus();
        }
      } finally {
        detectLocationBtn.disabled = false;
        detectLocationBtn.textContent = originalText;
      }
    });
  }

  // Block uncontrolled in-page Google Auto Ads (.google-auto-placed) while keeping
  // the top slide-down anchor ad and explicit .ad-unit slots
  function pruneIntrusiveAutoAds() {
    document.querySelectorAll(".google-auto-placed").forEach((el) => {
      if (!el.closest(".ad-unit")) {
        el.remove();
      }
    });
    document
      .querySelectorAll("main ins.adsbygoogle:not([data-ad-slot]), .topbar ins.adsbygoogle:not([data-ad-slot])")
      .forEach((el) => {
        if (!el.closest(".ad-unit") && !el.hasAttribute("data-anchor-status")) {
          el.remove();
        }
      });
  }

  pruneIntrusiveAutoAds();
  if (typeof MutationObserver !== "undefined" && document.body) {
    const adObserver = new MutationObserver(() => {
      pruneIntrusiveAutoAds();
    });
    adObserver.observe(document.body, { childList: true, subtree: true });
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
    if (!historyModalBody) return;
    historyModalBody.innerHTML = renderPriceHistoryHtml(data);
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
            ${renderScopeBadge(s)}
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
