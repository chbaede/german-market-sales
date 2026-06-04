(function () {
  const pageRoot = document.querySelector("[data-shopping-storage-key]");
  const storageKey = pageRoot?.dataset.shoppingStorageKey || "supermarketDealShoppingList";
  const cards = Array.from(document.querySelectorAll("[data-offer-id]"));
  const list = document.querySelector("[data-shopping-list]");
  const emptyState = document.querySelector("[data-shopping-empty]");
  const count = document.querySelector("[data-shopping-count]");
  const summary = document.querySelector("[data-shopping-summary]");
  const clearButton = document.querySelector("[data-clear-shopping-list]");
  const filterForm = document.querySelector("[data-filter-form]");
  const basketForm = document.querySelector("[data-basket-form]");
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
    button.innerHTML = isSelected ? "Gemerkt <span>담김</span>" : "Zur Liste <span>담기</span>";
  }

  function syncButtons() {
    const selectedIds = new Set(selected.map((offer) => offer.id));
    cards.forEach((card) => {
      setButtonState(card, selectedIds.has(card.dataset.offerId));
    });
  }

  function renderSummary() {
    const counts = selected.reduce((acc, offer) => {
      acc[offer.retailer] = (acc[offer.retailer] || 0) + 1;
      return acc;
    }, {});
    summary.innerHTML = "";
    Object.entries(counts).forEach(([retailer, amount]) => {
      const item = document.createElement("span");
      item.textContent = `${retailer} ${amount}`;
      summary.appendChild(item);
    });
  }

  function renderList() {
    list.innerHTML = "";
    count.textContent = selected.length;
    emptyState.hidden = selected.length > 0;
    clearButton.disabled = selected.length === 0;
    renderSummary();

    selected.forEach((offer) => {
      const item = document.createElement("li");
      item.className = "shopping-item";
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
          <a href="${escapeAttribute(offer.sourceUrl)}" target="_blank" rel="noopener noreferrer">Quelle <span>출처</span></a>
          <button type="button" data-remove-offer="${escapeAttribute(offer.id)}">Entfernen <span>삭제</span></button>
        </div>
      `;
      list.appendChild(item);
    });
  }

  function toggleOffer(card) {
    const offer = offerFromCard(card);
    const index = selected.findIndex((item) => item.id === offer.id);
    if (index >= 0) {
      selected.splice(index, 1);
    } else {
      selected.unshift(offer);
    }
    writeSelected();
    syncButtons();
    renderList();
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
    renderList();
  });

  if (basketForm && basketModal && basketModalBody && basketModalStatus && basketModalClose && basketWhatsapp) {
    basketForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const formData = buildBasketRequestData();
      const query = String(formData.get("basket") || "").trim();
      openBasketModal();
      updateWhatsappShare("");
      basketModalBody.innerHTML = "";
      if (!query) {
        basketModalStatus.textContent = "장보기 목록을 먼저 입력해주세요.";
        return;
      }

      basketModalStatus.textContent = "할인 품목을 찾는 중입니다...";
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
        basketModalStatus.textContent = "추천을 가져오지 못했습니다. 잠시 후 다시 시도해주세요.";
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

  function buildBasketRequestData() {
    const formData = new FormData(basketForm);
    if (!filterForm) {
      return formData;
    }

    copyCurrentFilterValue(formData, "zip_code");
    copyCurrentFilterValue(formData, "week");
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
      basketModalBody.innerHTML = `<p class="basket-miss">입력한 장보기 항목이 없습니다.</p>`;
      return;
    }
    basketModalBody.innerHTML = `
      <div class="basket-results basket-results-modal">
        ${recommendations.map(renderBasketRecommendation).join("")}
      </div>
    `;
  }

  function updateWhatsappShare(text) {
    if (!text) {
      basketWhatsapp.href = "#";
      basketWhatsapp.classList.add("is-disabled");
      basketWhatsapp.setAttribute("aria-disabled", "true");
      return;
    }
    basketWhatsapp.href = `https://wa.me/?text=${encodeURIComponent(text)}`;
    basketWhatsapp.classList.remove("is-disabled");
    basketWhatsapp.setAttribute("aria-disabled", "false");
  }

  function buildWhatsappText(payload) {
    const weekLabel = payload.week_label ? `${payload.week_label.de} / ${payload.week_label.ko}` : "";
    const lines = [
      "할인 장보기 추천",
      weekLabel ? `기간: ${weekLabel}` : "",
      payload.query ? `목록: ${payload.query}` : "",
      "",
    ].filter(Boolean);

    for (const recommendation of payload.recommendations || []) {
      const item = recommendation.item || {};
      lines.push(`[${item.raw || item.name || "항목"}]`);
      const candidates = recommendation.candidates || [];
      if (!candidates.length) {
        lines.push("현재 선택한 마트/주간 할인 목록에서 찾지 못했습니다.", "");
        continue;
      }
      candidates.forEach((candidate, index) => {
        const details = [];
        if (candidate.old_price_text) {
          details.push(`원가 ${candidate.old_price_text}${candidate.old_price_estimated ? " (추정)" : ""}`);
        }
        if (candidate.price_text) {
          details.push(`할인가 ${candidate.price_text}`);
        }
        if (candidate.discount_text) {
          details.push(`할인 ${candidate.discount_text}`);
        }
        if (candidate.estimated_total_text) {
          details.push(`예상 ${candidate.estimated_total_text}`);
        }
        if (candidate.unit_price_text) {
          details.push(`단위 ${candidate.unit_price_text}`);
        }
        if (candidate.valid_text) {
          details.push(`유효 ${candidate.valid_text}`);
        }
        lines.push(`${index + 1}. ${candidate.retailer} - ${candidate.title}`);
        if (details.length) {
          lines.push(`   ${details.join(" · ")}`);
        }
      });
      lines.push("");
    }
    return lines.join("\n").trim();
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
            : `<p class="basket-miss">Aktuell kein passendes Angebot. <span class="ko">현재 선택한 주 할인 목록에서 찾지 못했습니다.</span></p>`
        }
      </article>
    `;
  }

  function renderBasketCandidate(candidate) {
    const oldPriceLabel = candidate.old_price_estimated ? "Normalpreis 추정 원가" : "Normalpreis 원가";
    const hasImage = Boolean(candidate.image_url);
    return `
      <li>
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
              ${candidate.price_text ? `<span>Angebot 할인가: <strong>${escapeHtml(candidate.price_text)}</strong></span>` : ""}
              ${candidate.discount_text ? `<span class="basket-discount">${escapeHtml(candidate.discount_text)}</span>` : ""}
            </div>
            ${candidate.unit_price_text ? `<p>${escapeHtml(candidate.unit_price_text)}</p>` : ""}
            ${candidate.valid_text ? `<p>${escapeHtml(candidate.valid_text)}</p>` : ""}
          </div>
        </div>
        <div class="basket-estimate">
          <strong>${escapeHtml(candidate.estimated_total_text || "-")}</strong>
          <span>${escapeHtml(candidate.estimate_note || "")}</span>
          <a href="${escapeAttribute(candidate.source_url || "#")}" target="_blank" rel="noopener noreferrer">Quelle <span>출처</span></a>
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

  syncButtons();
  renderList();
})();
