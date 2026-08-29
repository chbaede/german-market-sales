(function () {

  const getLang = () => document.cookie.includes('lang=en') ? 'en' : 'ko';
  const currentLang = getLang();
  const tk = (ko, en) => currentLang === 'en' ? en : ko;

  const pageRoot = document.querySelector("[data-shopping-storage-key]");
  const storageKey = pageRoot?.dataset.shoppingStorageKey || "supermarketDealShoppingList";
  const cards = Array.from(document.querySelectorAll("[data-offer-id]"));
  const list = document.querySelector("[data-shopping-list]");
  const emptyState = document.querySelector("[data-shopping-empty]");
  const count = document.querySelector("[data-shopping-count]");
  const summary = document.querySelector("[data-shopping-summary]");
  const clearButton = document.querySelector("[data-clear-shopping-list]");
  const listScroll = document.querySelector("[data-shopping-list-scroll]");
  const shoppingWhatsapp = document.querySelector("[data-shopping-whatsapp]");
  const filterForm = document.querySelector("[data-filter-form]");
  const basketForm = document.querySelector("[data-basket-form]");
  const basketInput = basketForm?.querySelector('[name="basket"]');
  const basketModal = document.querySelector("[data-basket-modal]");
  const basketModalBody = document.querySelector("[data-basket-modal-body]");
  const basketModalStatus = document.querySelector("[data-basket-modal-status]");
  const basketModalClose = document.querySelector("[data-basket-modal-close]");
  const basketWhatsapp = document.querySelector("[data-basket-whatsapp]");

  if (!list || !emptyState || !count || !summary || !clearButton) {
    return;
  }

  let selected = readSelected();

  function readSelected() {
    try {
      const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function writeSelected() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(selected));
    } catch (error) {
      return;
    }
  }

  function offerFromCard(card) {
    return {
      id: card.dataset.offerId,
      title: card.dataset.title || "",
      titleKo: card.dataset.titleKo || "",
      retailer: card.dataset.retailer || "",
      retailerSlug: card.dataset.retailerSlug || "",
      price: card.dataset.price || "",
      unitPrice: card.dataset.unitPrice || "",
      discount: card.dataset.discount || "",
      valid: card.dataset.valid || "",
      sourceUrl: card.dataset.sourceUrl || "",
      imageUrl: card.dataset.imageUrl || "",
    };
  }

  function setButtonState(card, isSelected) {
    const button = card.querySelector("[data-select-offer]");
    if (!button) {
      return;
    }
    button.setAttribute("aria-pressed", String(isSelected));
    button.classList.toggle("is-selected", isSelected);
    button.innerHTML = isSelected ? `Gemerkt <span>${tk("담김", "Saved")}</span>` : `Zur Liste <span>${tk("담기", "Add")}</span>`;
  }

  function syncButtons() {
    const selectedIds = new Set(selected.map((offer) => offer.id));
    cards.forEach((card) => {
      setButtonState(card, selectedIds.has(card.dataset.offerId));
    });
  }

  function renderSummary() {
    const counts = selected.reduce((acc, offer) => {
      const key = retailerKey(offer.retailerSlug, offer.retailer);
      acc[key] = acc[key] || { amount: 0, retailer: offer.retailer, retailerSlug: offer.retailerSlug };
      acc[key].amount += 1;
      return acc;
    }, {});
    summary.innerHTML = "";
    Object.values(counts).forEach(({ retailer, retailerSlug, amount }) => {
      const item = document.createElement("span");
      item.className = `retailer-theme ${themeClass(retailerSlug)}`;
      item.textContent = `${retailer} ${amount}`;
      summary.appendChild(item);
    });
  }

  function renderList(options = {}) {
    list.innerHTML = "";
    count.textContent = selected.length;
    emptyState.hidden = selected.length > 0;
    clearButton.disabled = selected.length === 0;
    renderSummary();
    updateShoppingWhatsappShare();

    selected.forEach((offer) => {
      const item = document.createElement("li");
      item.className = `shopping-item retailer-theme ${themeClass(offer.retailerSlug)}`;
      item.innerHTML = `
        <div class="shopping-item-main">
          ${
            offer.imageUrl
              ? `<img src="${escapeAttribute(offer.imageUrl)}" alt="">`
              : `<span class="shopping-image-placeholder">${escapeHtml(offer.retailer.slice(0, 1))}</span>`
          }
          <div>
            <span class="retailer-badge">${escapeHtml(offer.retailer)}</span>
            <strong>${escapeHtml(offer.title)}</strong>
            ${offer.titleKo ? `<p class="shopping-translation">${escapeHtml(offer.titleKo)}</p>` : ""}
            <p>${escapeHtml(offer.price)}${offer.discount ? ` · ${escapeHtml(offer.discount)}` : ""}</p>
            ${offer.unitPrice ? `<p class="shopping-unit">${escapeHtml(offer.unitPrice)}</p>` : ""}
            <p class="shopping-valid">${escapeHtml(offer.valid)}</p>
          </div>
        </div>
        <div class="shopping-item-actions">
          <a href="${escapeAttribute(offer.sourceUrl)}" target="_blank" rel="noopener noreferrer">Quelle <span>${tk("출처", "Source")}</span></a>
          <button type="button" data-remove-offer="${escapeAttribute(offer.id)}">Entfernen <span>${tk("삭제", "Remove")}</span></button>
        </div>
      `;
      list.appendChild(item);
    });

    if (options.scrollToTop && listScroll) {
      listScroll.scrollTop = 0;
    }
  }

  function toggleOffer(card) {
    const offer = offerFromCard(card);
    const index = selected.findIndex((item) => item.id === offer.id);
    const isAdding = index < 0;
    if (index >= 0) {
      selected.splice(index, 1);
    } else {
      selected.unshift(offer);
    }
    writeSelected();
    syncButtons();
    renderList({ scrollToTop: isAdding });
  }

  cards.forEach((card) => {
    const button = card.querySelector("[data-select-offer]");
    if (!button) {
      return;
    }
    button.addEventListener("click", () => toggleOffer(card));
  });

  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-offer]");
    if (!button) {
      return;
    }
    selected = selected.filter((offer) => offer.id !== button.dataset.removeOffer);
    writeSelected();
    syncButtons();
    renderList();
  });

  clearButton.addEventListener("click", () => {
    selected = [];
    writeSelected();
    syncButtons();
    renderList({ scrollToTop: true });
  });

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

  if (basketForm && basketModal && basketModalBody && basketModalStatus && basketModalClose && basketWhatsapp) {
    basketForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = buildBasketRequestData();
      const query = String(formData.get("basket") || "").trim();
      openBasketModal();
      updateWhatsappShare("");
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
        basketModalStatus.textContent = tk("추천을 가져오지 못했습니다. 잠시 후 다시 시도해주세요.", "Failed to fetch recommendations. Please try again.");
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
  }

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
    updateWhatsappShare(buildWhatsappText(payload));
    const recommendations = payload.recommendations || [];
    if (!recommendations.length) {
      basketModalBody.innerHTML = `<p class="basket-miss">${tk("입력한 장보기 항목이 없습니다.", "No shopping items entered.")}</p>`;
      return;
    }
    basketModalBody.innerHTML = `
      <div class="basket-results basket-results-modal">
        ${recommendations.map(renderBasketRecommendation).join("")}
      </div>
    `;
    window.requestAnimationFrame(() => {
      basketModal.scrollTop = 0;
      basketModalBody.scrollTop = 0;
    });
  }

  function updateWhatsappShare(text) {
    updateShareLink(basketWhatsapp, text);
  }

  function updateShoppingWhatsappShare() {
    updateShareLink(shoppingWhatsapp, buildSelectedWhatsappText());
  }

  function updateShareLink(link, text) {
    if (!link) {
      return;
    }
    if (!text) {
      link.href = "#";
      link.classList.add("is-disabled");
      link.setAttribute("aria-disabled", "true");
      return;
    }
    link.href = `https://wa.me/?text=${encodeURIComponent(text)}`;
    link.classList.remove("is-disabled");
    link.setAttribute("aria-disabled", "false");
  }

  function buildWhatsappText(payload) {
    const weekLabel = payload.week_label ? `${payload.week_label.de} / ${payload.week_label.ko}` : "";
    const lines = [
      tk("할인 장보기 추천", "Deal Recommendations"),
      weekLabel ? `${tk("기간: ", "Period: ")}${weekLabel}` : "",
      payload.query ? `${tk("목록: ", "List: ")}${payload.query}` : "",
      "",
    ].filter(Boolean);

    const grouped = groupBasketCandidatesByRetailer(payload.recommendations || []);
    grouped.forEach((group) => {
      lines.push(`[${group.retailer}]`);
      group.items.forEach(({ item, candidate }) => {
        lines.push(`- ${item.raw || item.name || tk("항목", "Item")}: ${candidate.title || ""}`);
        appendDetailLine(lines, candidateDetails(candidate));
      });
      lines.push("");
    });

    const missed = (payload.recommendations || []).filter((recommendation) => {
      return !(recommendation.candidates || []).length;
    });
    if (missed.length) {
      lines.push(tk("[못 찾음]", "[Not found]"));
      missed.forEach((recommendation) => {
        const item = recommendation.item || {};
        lines.push(`- ${item.raw || item.name || tk("항목", "Item")}`);
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

  function buildSelectedWhatsappText() {
    if (!selected.length) {
      return "";
    }
    const lines = [tk("장보기 목록", "Shopping List"), ""];
    groupSelectedByRetailer(selected).forEach((group) => {
      lines.push(`[${group.retailer}]`);
      group.offers.forEach((offer) => {
        lines.push(`- ${offer.title}`);
        appendDetailLine(lines, [offer.price, offer.discount, offer.unitPrice, offer.valid]);
      });
      lines.push("");
    });
    return lines.join("\n").trim();
  }

  function groupSelectedByRetailer(offers) {
    const groups = new Map();
    offers.forEach((offer) => {
      const key = retailerKey(offer.retailerSlug, offer.retailer);
      ensureRetailerGroup(groups, key, offer.retailer, "offers").offers.push(offer);
    });
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
      lines.push(`  ${visibleDetails.join(" · ")}`);
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
    const oldPriceLabel = candidate.old_price_estimated ? `Normalpreis ${tk("추정 원가", "Estimated Reg.")}` : `Normalpreis ${tk("원가", "Regular Price")}`;
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
            ${candidate.title_ko ? `<p class="shopping-translation">${escapeHtml(candidate.title_ko)}</p>` : ""}
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

  syncButtons();
  renderList();
})();
