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
          // Fallback to clipboard if share was cancelled or unsupported
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

  // Filter Retailer Presets (Standard / Alle / Keine)
  const filterForm = document.querySelector("[data-filter-form]");
  if (filterForm) {
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
    });
  }

  // Basket Advisor Modal & Recommendations Copy
  const basketForm = document.querySelector("[data-basket-form]");
  const basketInput = basketForm?.querySelector('[name="basket"]');
  const basketModal = document.querySelector("[data-basket-modal]");
  const basketModalBody = document.querySelector("[data-basket-modal-body]");
  const basketModalStatus = document.querySelector("[data-basket-modal-status]");
  const basketModalClose = document.querySelector("[data-basket-modal-close]");
  const basketCopy = document.querySelector("[data-basket-copy]");
  let lastBasketText = "";

  if (basketForm && basketInput) {
    basketForm.addEventListener("click", (event) => {
      const button = event.target.closest("[data-basket-template]");
      if (!button || !basketForm.contains(button)) {
        return;
      }
      basketInput.value = button.dataset.basketTemplate || "";
      basketInput.focus();
      const cursor = basketInput.value.length;
      basketInput.setSelectionRange(cursor, cursor);
    });
  }

  if (basketForm && basketModal && basketModalBody && basketModalStatus && basketModalClose) {
    basketForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = buildBasketRequestData();
      const query = String(formData.get("basket") || "").trim();
      openBasketModal();
      lastBasketText = "";
      if (basketCopy) {
        basketCopy.disabled = true;
        basketCopy.classList.add("is-disabled");
      }
      basketModalBody.innerHTML = "";
      if (!query) {
        basketModalStatus.textContent = tk("장보기 목록을 먼저 입력해주세요.", "Please enter your shopping list first.");
        return;
      }

      basketModalStatus.textContent = tk("할인 품목을 찾는 중입니다...", "Finding deals...");
      try {
        const url = new URL(basketForm.dataset.endpoint || "/api/basket", window.location.origin);
        formData.forEach((value, key) => {
          if (String(value).trim()) {
            url.searchParams.append(key, value);
          }
        });
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        if (!response.ok) {
          throw new Error("Basket recommendation request failed.");
        }
        renderBasketRecommendations(await response.json());
      } catch (error) {
        basketModalStatus.textContent = tk(
          "추천을 가져오지 못했습니다. 잠시 후 다시 시도해주세요.",
          "Failed to fetch recommendations. Please try again."
        );
      }
    });

    basketModalClose.addEventListener("click", closeBasketModal);
    basketModal.addEventListener("click", (event) => {
      if (event.target === basketModal) {
        closeBasketModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !basketModal.hidden) {
        closeBasketModal();
      }
    });

    if (basketCopy) {
      basketCopy.addEventListener("click", async () => {
        if (!lastBasketText) return;
        try {
          await navigator.clipboard.writeText(lastBasketText);
          showToast(tk("추천 목록이 복사되었습니다!", "Recommendations copied to clipboard!"));
        } catch (err) {
          showToast(tk("복사에 실패했습니다.", "Failed to copy."));
        }
      });
    }
  }

  function buildBasketRequestData() {
    const formData = new FormData(basketForm);
    if (!filterForm) {
      return formData;
    }

    copyCurrentFilterValue(formData, "zip_code");
    copyCurrentFilterValue(formData, "week");
    formData.delete("priced");
    const pricedOnly = filterForm.querySelector('input[name="priced"]:checked');
    if (pricedOnly) {
      formData.append("priced", pricedOnly.value || "1");
    }
    formData.delete("ending");
    const endingSoon = filterForm.querySelector('input[name="ending"]:checked');
    if (endingSoon) {
      formData.append("ending", endingSoon.value || "1");
    }
    formData.delete("retailer");
    filterForm.querySelectorAll('input[name="retailer"]:checked').forEach((input) => {
      formData.append("retailer", input.value);
    });
    return formData;
  }

  function copyCurrentFilterValue(formData, name) {
    const field = filterForm.querySelector(`[name="${name}"]`);
    if (!field || !field.value) {
      return;
    }
    formData.delete(name);
    formData.append(name, field.value);
  }

  function openBasketModal() {
    basketModal.hidden = false;
    document.body.classList.add("modal-open");
    basketModal.scrollTop = 0;
    basketModalBody.scrollTop = 0;
    basketModalClose.focus();
  }

  function closeBasketModal() {
    basketModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  function renderBasketRecommendations(payload) {
    const weekLabel = payload.week_label ? `${payload.week_label.de} / ${payload.week_label.ko}` : "";
    basketModalStatus.textContent = `${payload.query || ""} · ${weekLabel}`;
    lastBasketText = buildBasketText(payload);
    if (basketCopy && lastBasketText) {
      basketCopy.disabled = false;
      basketCopy.classList.remove("is-disabled");
    }
    const recommendations = payload.recommendations || [];
    if (!recommendations.length) {
      basketModalBody.innerHTML = `<p class="basket-miss">${tk("입력한 장보기 항목이 없습니다.", "No shopping items entered.")}</p>`;
      return;
    }

    const storeTotalsHtml = payload.store_totals ? renderBasketStoreTotals(payload.store_totals) : "";

    basketModalBody.innerHTML = `
      ${storeTotalsHtml}
      <div class="basket-results basket-results-modal">
        ${recommendations.map(renderBasketRecommendation).join("")}
      </div>
    `;
    window.requestAnimationFrame(() => {
      basketModal.scrollTop = 0;
      basketModalBody.scrollTop = 0;
    });
  }

  function buildBasketText(payload) {
    const weekLabel = payload.week_label ? `${payload.week_label.de} / ${payload.week_label.ko}` : "";
    const lines = [
      `🛒 ${tk("마트 할인 장보기 추천", "Supermarkt Deals - Basket Advisor")}`,
      weekLabel ? `${tk("기간: ", "Period: ")}${weekLabel}` : "",
      payload.query ? `${tk("입력 목록: ", "Shopping Query: ")}${payload.query}` : "",
      "",
    ].filter(Boolean);

    const grouped = groupBasketCandidatesByRetailer(payload.recommendations || []);
    grouped.forEach((group) => {
      lines.push(`📍 [${group.retailer}]`);
      group.items.forEach(({ item, candidate }) => {
        lines.push(`• ${item.raw || item.name || tk("항목", "Item")}: ${candidate.title || ""}`);
        appendDetailLine(lines, candidateDetails(candidate));
      });
      lines.push("");
    });

    const missed = (payload.recommendations || []).filter((recommendation) => {
      return !(recommendation.candidates || []).length;
    });
    if (missed.length) {
      lines.push(`❓ ${tk("[할인 미발견 항목]", "[No Deals Found]")}`);
      missed.forEach((recommendation) => {
        const item = recommendation.item || {};
        lines.push(`• ${item.raw || item.name || tk("항목", "Item")}`);
      });
      lines.push("");
    }

    return lines.join("\n").trim();
  }

  function groupBasketCandidatesByRetailer(recommendations) {
    const groups = new Map();
    for (const recommendation of recommendations) {
      const item = recommendation.item || {};
      const candidate = (recommendation.candidates || [])[0];
      if (!candidate) {
        continue;
      }
      const key = retailerKey(candidate.retailer_slug, candidate.retailer);
      ensureRetailerGroup(groups, key, candidate.retailer, "items").items.push({ item, candidate });
    }
    return Array.from(groups.values());
  }

  function retailerKey(slug, retailer) {
    return slug || retailer || "unknown";
  }

  function ensureRetailerGroup(groups, key, retailer, collectionName) {
    if (!groups.has(key)) {
      groups.set(key, { retailer: retailer || tk("마트 미정", "Unknown retailer"), [collectionName]: [] });
    }
    return groups.get(key);
  }

  function appendDetailLine(lines, details) {
    const visibleDetails = details.filter(Boolean);
    if (visibleDetails.length) {
      lines.push(`    ${visibleDetails.join(" · ")}`);
    }
  }

  function candidateDetails(candidate) {
    const details = [];
    if (candidate.old_price_text) {
      details.push(`${tk("원가 ", "Reg. ")}${candidate.old_price_text}${candidate.old_price_estimated ? tk(" (추정)", " (est.)") : ""}`);
    }
    if (candidate.price_text) {
      details.push(`${tk("할인가 ", "Deal ")}${candidate.price_text}`);
    }
    if (candidate.discount_text) {
      details.push(`${tk("할인 ", "Save ")}${candidate.discount_text}`);
    }
    if (candidate.estimated_total_text) {
      details.push(`${tk("예상 ", "Total ")}${candidate.estimated_total_text}`);
    }
    if (candidate.unit_price_text) {
      details.push(`${tk("단위 ", "Unit ")}${candidate.unit_price_text}`);
    }
    if (candidate.valid_text) {
      details.push(`${tk("유효 ", "Valid ")}${candidate.valid_text}`);
    }
    return details;
  }

  function renderBasketRecommendation(recommendation) {
    const item = recommendation.item || {};
    const candidates = recommendation.candidates || [];
    return `
      <article class="basket-result">
        <div class="basket-result-heading">
          <div>
            <h3>${escapeHtml(item.raw || "")}</h3>
            ${item.amountText || item.amount_text ? `<p>${escapeHtml(item.amountText || item.amount_text)}</p>` : ""}
          </div>
          <span>${candidates.length}</span>
        </div>
        ${
          candidates.length
            ? `<ol class="basket-candidates">${candidates.map(renderBasketCandidate).join("")}</ol>`
            : `<p class="basket-miss">Aktuell kein passendes Angebot. <span class="ko">${tk("현재 선택한 주 할인 목록에서 찾지 못했습니다.", "Not found in the current week's deals.")}</span></p>`
        }
      </article>
    `;
  }

  function renderBasketCandidate(candidate) {
    const oldPriceLabel = candidate.old_price_estimated
      ? `Normalpreis ${tk("추정 원가", "Estimated Reg.")}`
      : `Normalpreis ${tk("원가", "Regular Price")}`;
    const hasImage = Boolean(candidate.image_url);
    return `
      <li class="retailer-theme ${themeClass(candidate.retailer_slug)}">
        <div class="basket-candidate-main ${hasImage ? "" : "is-without-image"}">
          ${
            hasImage
              ? `<img class="basket-candidate-image" src="${escapeAttribute(candidate.image_url)}" alt="${escapeAttribute(candidate.title || "")}" loading="lazy">`
              : ""
          }
          <div class="basket-candidate-details">
            <span class="retailer-badge">${escapeHtml(candidate.retailer || "")}</span>
            <strong>${escapeHtml(candidate.title || "")}</strong>
            ${candidate.title_ko ? `<p class="deal-translation">${escapeHtml(candidate.title_ko)}</p>` : ""}
            <div class="basket-price-details">
              ${
                candidate.old_price_text
                  ? `<span>${oldPriceLabel}: <s>${escapeHtml(candidate.old_price_text)}</s></span>`
                  : ""
              }
              ${candidate.price_text ? `<span>Angebot ${tk("할인가:", "Deal:")} <strong>${escapeHtml(candidate.price_text)}</strong></span>` : ""}
              ${candidate.discount_text ? `<span class="basket-discount">${escapeHtml(candidate.discount_text)}</span>` : ""}
            </div>
            ${candidate.unit_price_text ? `<p>${escapeHtml(candidate.unit_price_text)}</p>` : ""}
            ${candidate.valid_text ? `<p>${escapeHtml(candidate.valid_text)}</p>` : ""}
          </div>
        </div>
        <div class="basket-estimate">
          <strong>${escapeHtml(candidate.estimated_total_text || "-")}</strong>
          <span>${tk(escapeHtml(candidate.estimate_note || ""), {"가격 정보 없음": "No price", "단위가격 기준 예상": "Est. by unit", "수량 기준 단순 예상": "Est. by qty", "행사가 기준": "Deal price", "단위가격 기준": "Unit price"}[candidate.estimate_note] || escapeHtml(candidate.estimate_note || ""))}</span>
          <a href="${escapeAttribute(candidate.source_url || "#")}" target="_blank" rel="noopener noreferrer">Quelle <span>${tk("출처", "Source")}</span></a>
        </div>
      </li>
    `;
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

  function themeClass(value) {
    const slug = String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "");
    return slug ? `retailer-${slug}` : "";
  }

  // Basket Store Totals Rendering
  function renderBasketStoreTotals(storeTotals) {
    const stores = storeTotals.stores || [];
    const smart = storeTotals.smart_split || {};
    if (!stores.length) return "";

    const storeCards = stores.map((s, idx) => {
      const badge = idx === 0 ? "🥇" : idx === 1 ? "🥈" : idx === 2 ? "🥉" : "🏬";
      return `
        <div class="basket-store-card ${idx === 0 ? "is-cheapest" : ""}">
          <div class="bsc-head">
            <span class="bsc-badge">${badge}</span>
            <strong>${escapeHtml(s.retailer)}</strong>
            <span class="bsc-coverage">${s.covered_items_count}/${s.total_requested_items} (${s.coverage_percent}%)</span>
          </div>
          <div class="bsc-price">
            <strong>${s.total_cost.toFixed(2)} €</strong>
          </div>
          ${s.missing_item_names && s.missing_item_names.length ? `<p class="bsc-missing">${tk("미발견: ", "Missing: ")}${escapeHtml(s.missing_item_names.join(", "))}</p>` : ""}
        </div>
      `;
    }).join("");

    let smartHtml = "";
    if (smart.total_cost > 0) {
      smartHtml = `
        <div class="basket-smart-split">
          <div class="bss-header">
            <strong>💡 ${tk("스마트 분할 구매 시: ", "Smart Split: ")}<span class="highlight-price">${smart.total_cost.toFixed(2)} €</span></strong>
            ${smart.potential_savings > 0 ? `<span class="savings-tag">${tk("단일 매장 대비 최대", "Save up to")} ${smart.potential_savings.toFixed(2)} € (${smart.savings_percent}%) ${tk("절약", "off")}</span>` : ""}
          </div>
          <p class="bss-desc">${tk("각 마트의 최저가 할인 상품만 골라 살 경우의 총액입니다.", "Best price combined across all stores.")}</p>
        </div>
      `;
    }

    return `
      <section class="basket-store-comparison">
        <h4>🏪 ${tk("매장별 예상 구매 비용 비교", "Estimated Total Cost by Store")}</h4>
        <div class="basket-store-grid">
          ${storeCards}
        </div>
        ${smartHtml}
      </section>
    `;
  }

  // 1. Price History Modal
  const historyModal = document.querySelector("[data-history-modal]");
  const historyModalBody = document.querySelector("[data-history-modal-body]");
  const historyModalClose = document.querySelector("[data-history-modal-close]");

  function openHistoryModal() {
    if (!historyModal) return;
    historyModal.hidden = false;
    document.body.classList.add("modal-open");
    historyModalClose?.focus();
  }

  function closeHistoryModal() {
    if (!historyModal) return;
    historyModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  historyModalClose?.addEventListener("click", closeHistoryModal);
  historyModal?.addEventListener("click", (e) => {
    if (e.target === historyModal) closeHistoryModal();
  });

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
    
    let statsHtml = `
      <div class="tracker-stats-grid">
        <div class="tracker-stat ${isAtl ? 'is-best' : ''}">
          <span class="label">${tk("역대 최저가", "All-time Low")}</span>
          <strong>${data.all_time_low ? data.all_time_low.toFixed(2) + " €" : "-"}</strong>
          ${isAtl ? `<span class="badge-mini">🔥 ${tk("현재 최저가!", "Best price now!")}</span>` : ""}
        </div>
        <div class="tracker-stat">
          <span class="label">${tk("평균 가격", "Average Price")}</span>
          <strong>${data.avg_price ? data.avg_price.toFixed(2) + " €" : "-"}</strong>
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
          <h4>📅 ${tk("가격 관측 이력 (수집 시점 & 출처)", "Price Observations & Sources")}</h4>
          <table class="tracker-history-table">
            <thead>
              <tr>
                <th>${tk("수집일", "Date")}</th>
                <th>${tk("매장", "Store")}</th>
                <th>${tk("가격", "Price")}</th>
                <th>${tk("할인율", "Discount")}</th>
                <th>${tk("전단지 출처", "Flyer")}</th>
              </tr>
            </thead>
            <tbody>
              ${history.map(item => `
                <tr>
                  <td>${item.recorded_at ? item.recorded_at.substring(0, 10) : "-"}</td>
                  <td><span class="retailer-badge">${escapeHtml(item.retailer)}</span></td>
                  <td><strong>${item.price ? item.price.toFixed(2) + " €" : "-"}</strong></td>
                  <td>${item.discount_percent ? item.discount_percent + "%" : "-"}</td>
                  <td><a href="${escapeAttribute(item.source_url || '#')}" target="_blank" rel="noopener noreferrer">Prospekt ↗</a></td>
                </tr>
              `).join("")}
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

  function openCompareModal() {
    if (!compareModal) return;
    compareModal.hidden = false;
    document.body.classList.add("modal-open");
    compareModalClose?.focus();
  }

  function closeCompareModal() {
    if (!compareModal) return;
    compareModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  compareModalClose?.addEventListener("click", closeCompareModal);
  compareModal?.addEventListener("click", (e) => {
    if (e.target === compareModal) closeCompareModal();
  });

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
            <span class="csb-badge">🏆 ${tk("최저가 매장", "Cheapest Store")}</span>
            <strong>${escapeHtml(data.cheapest_store.retailer)}: ${data.cheapest_store.price.toFixed(2)} €</strong>
          </div>
          ${data.potential_savings > 0 ? `
            <div class="csb-savings">
              ${tk("최고가 매장 대비", "Savings vs Highest")}: <strong>${data.potential_savings.toFixed(2)} € (${data.savings_percent}%) ${tk("절약", "off")}</strong>
            </div>
          ` : ""}
        </div>
      `;
    }

    const cardsHtml = stores.map((s, idx) => {
      const isCheapest = idx === 0 && s.price !== null;
      return `
        <div class="compare-store-card ${isCheapest ? 'is-best' : ''}">
          <div class="csc-header">
            <span class="retailer-badge">${escapeHtml(s.retailer)}</span>
            ${isCheapest ? `<span class="badge-mini">🏆 ${tk("최저가", "Best")}</span>` : ""}
          </div>
          <h4>${escapeHtml(s.title)}</h4>
          <div class="csc-price-row">
            <strong>${s.price !== null ? s.price.toFixed(2) + " €" : "-"}</strong>
            ${s.old_price ? `<s>${s.old_price.toFixed(2)} €</s>` : ""}
            ${s.discount_percent ? `<span class="discount-badge">-${s.discount_percent}%</span>` : ""}
          </div>
          ${s.unit_price ? `<p class="csc-unit">${s.unit_price.toFixed(2)} € / ${escapeHtml(s.unit || "unit")}</p>` : ""}
          <div class="csc-footer">
            <a href="${escapeAttribute(s.source_url || '#')}" target="_blank" rel="noopener noreferrer">Zum Angebot ↗</a>
          </div>
        </div>
      `;
    }).join("");

    compareModalBody.innerHTML = `
      <div class="compare-results">
        ${summaryHtml}
        <div class="compare-grid">
          ${cardsHtml}
        </div>
      </div>
    `;
  }

  // 3. Watchlist & Price Alerts Modal
  const WATCHLIST_STORAGE_KEY = "german_market_watchlist_items";
  const watchlistModal = document.querySelector("[data-watchlist-modal]");
  const watchlistModalBody = document.querySelector("[data-watchlist-modal-body]");
  const watchlistModalClose = document.querySelector("[data-watchlist-modal-close]");
  const watchlistAddForm = document.querySelector("[data-watchlist-add-form]");
  const watchlistCountBadges = document.querySelectorAll("[data-watchlist-count]");

  function getWatchlist() {
    try {
      return JSON.parse(localStorage.getItem(WATCHLIST_STORAGE_KEY) || "[]");
    } catch {
      return [];
    }
  }

  function saveWatchlist(items) {
    localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(items));
    updateWatchlistBadge();
  }

  function updateWatchlistBadge() {
    const count = getWatchlist().length;
    watchlistCountBadges.forEach(b => {
      b.textContent = count;
      b.hidden = count === 0;
    });
  }

  function openWatchlistModal() {
    if (!watchlistModal) return;
    watchlistModal.hidden = false;
    document.body.classList.add("modal-open");
    watchlistModalClose?.focus();
    loadWatchlistDeals();
  }

  function closeWatchlistModal() {
    if (!watchlistModal) return;
    watchlistModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  watchlistModalClose?.addEventListener("click", closeWatchlistModal);
  watchlistModal?.addEventListener("click", (e) => {
    if (e.target === watchlistModal) closeWatchlistModal();
  });

  document.querySelector("[data-open-watchlist-modal]")?.addEventListener("click", openWatchlistModal);

  watchlistAddForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const keywordInput = watchlistAddForm.querySelector('input[name="keyword"]');
    const priceInput = watchlistAddForm.querySelector('input[name="target_price"]');
    const keyword = (keywordInput?.value || "").trim();
    const targetPrice = priceInput?.value ? parseFloat(priceInput.value) : null;

    if (!keyword) return;
    addToWatchlist({
      id: "wl_" + Date.now() + "_" + Math.random().toString(36).substring(2, 6),
      keyword,
      target_price: targetPrice,
      created_at: new Date().toISOString()
    });

    keywordInput.value = "";
    if (priceInput) priceInput.value = "";
    loadWatchlistDeals();
    showToast(tk(`"${keyword}" 관심 상품이 등록되었습니다.`, `"${keyword}" added to watchlist.`));
  });

  function addToWatchlist(item) {
    const list = getWatchlist();
    list.unshift(item);
    saveWatchlist(list);
  }

  function removeFromWatchlist(id) {
    const list = getWatchlist().filter(i => i.id !== id);
    saveWatchlist(list);
    loadWatchlistDeals();
  }

  async function loadWatchlistDeals() {
    const list = getWatchlist();
    if (!watchlistModalBody) return;
    if (!list.length) {
      watchlistModalBody.innerHTML = `
        <div class="empty-state">
          <p>⭐ ${tk("등록된 관심 상품이 없습니다.", "No items in your watchlist.")}</p>
          <small>${tk("위 입력창에서 키워드와 목표 가격을 입력하거나, 상품 카드의 ⭐ 버튼을 눌러 등록하세요.", "Add keywords above or click the ⭐ button on any deal card.")}</small>
        </div>
      `;
      return;
    }

    watchlistModalBody.innerHTML = `<p class="tracker-loading">${tk("관심 상품의 최신 할인 및 가격 알림 확인 중...", "Checking active deals for your watchlist...")}</p>`;

    try {
      const zipCode = document.querySelector('input[name="zip_code"]')?.value || "14195";
      const week = document.querySelector('input[name="week"]')?.value || "current";
      const res = await fetch("/api/watchlist-deals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          watchlist: list,
          zip_code: zipCode,
          week: week
        })
      });
      if (!res.ok) throw new Error("Watchlist deals check failed");
      const data = await res.json();
      renderWatchlistModalContent(list, data.results || []);
    } catch {
      renderWatchlistModalContent(list, []);
    }
  }

  function renderWatchlistModalContent(watchlist, results) {
    const resultMap = new Map();
    results.forEach(r => resultMap.set(r.id, r));

    const itemsHtml = watchlist.map(item => {
      const res = resultMap.get(item.id) || { matches: [] };
      const matches = res.matches || [];
      const targetHit = matches.some(m => m.is_target_hit);

      let dealsHtml = "";
      if (matches.length) {
        dealsHtml = `
          <div class="watchlist-item-deals">
            ${matches.slice(0, 3).map(m => `
              <div class="watchlist-deal-row ${m.is_target_hit ? 'is-target-hit' : ''}">
                <span class="retailer-badge">${escapeHtml(m.retailer)}</span>
                <span class="deal-name">${escapeHtml(m.title)}</span>
                <strong>${m.price !== null ? m.price.toFixed(2) + " €" : "-"}</strong>
                ${m.is_target_hit ? `<span class="badge-target">🎯 ${tk("목표가 달성!", "Target Met!")}</span>` : ""}
                <a href="${escapeAttribute(m.source_url || '#')}" target="_blank" rel="noopener noreferrer">↗</a>
              </div>
            `).join("")}
          </div>
        `;
      } else {
        dealsHtml = `<p class="watchlist-item-no-deals">${tk("이번 주 일치하는 할인 상품 없음", "No active deals this week")}</p>`;
      }

      return `
        <div class="watchlist-item-card ${targetHit ? 'has-target-hit' : ''}">
          <div class="wic-head">
            <div class="wic-info">
              <strong>${escapeHtml(item.keyword)}</strong>
              ${item.target_price ? `<span class="wic-target">🎯 ${tk("목표가: ", "Target: ")}${item.target_price.toFixed(2)} €</span>` : ""}
            </div>
            <button type="button" class="btn-delete" data-remove-watchlist="${item.id}" title="${tk("삭제", "Delete")}">×</button>
          </div>
          ${dealsHtml}
        </div>
      `;
    }).join("");

    watchlistModalBody.innerHTML = `
      <div class="watchlist-list">
        <div class="watchlist-list-header">
          <span>${tk(`총 ${watchlist.length}개 관심 상품`, `${watchlist.length} Watched Items`)}</span>
          <button type="button" class="btn-clear-all" data-clear-watchlist>${tk("전체 삭제", "Clear All")}</button>
        </div>
        ${itemsHtml}
      </div>
    `;

    watchlistModalBody.querySelectorAll("[data-remove-watchlist]").forEach(btn => {
      btn.addEventListener("click", () => removeFromWatchlist(btn.dataset.removeWatchlist));
    });

    watchlistModalBody.querySelector("[data-clear-watchlist]")?.addEventListener("click", () => {
      if (confirm(tk("관심 목록을 모두 삭제하시겠습니까?", "Clear all items from watchlist?"))) {
        saveWatchlist([]);
        loadWatchlistDeals();
      }
    });
  }

  // Card Event Delegation
  document.addEventListener("click", (e) => {
    // 1. Price History button
    const historyBtn = e.target.closest("[data-price-history-btn]");
    if (historyBtn) {
      const title = historyBtn.dataset.title;
      const brand = historyBtn.dataset.brand;
      const price = historyBtn.dataset.price;
      loadPriceHistory(title, brand, price);
      return;
    }

    // 2. Compare button
    const compareBtn = e.target.closest("[data-compare-btn]");
    if (compareBtn) {
      const title = compareBtn.dataset.title;
      runCompare(title);
      return;
    }

    // 3. Watchlist Add button on card
    const watchBtn = e.target.closest("[data-watchlist-add-btn]");
    if (watchBtn) {
      const title = watchBtn.dataset.title;
      const price = parseFloat(watchBtn.dataset.price) || null;
      addToWatchlist({
        id: "wl_" + Date.now() + "_" + Math.random().toString(36).substring(2, 6),
        keyword: title,
        target_price: price,
        created_at: new Date().toISOString()
      });
      showToast(tk(`"${title}" 관심 상품에 추가되었습니다!`, `"${title}" added to watchlist!`));
      return;
    }
  });

  // Global ESC Key Handler
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeHistoryModal();
      closeCompareModal();
      closeWatchlistModal();
    }
  });

  // Init badge count on page load
  updateWatchlistBadge();
})();
