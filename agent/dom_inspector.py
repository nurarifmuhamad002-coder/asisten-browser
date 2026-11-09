from typing import Any, Dict, List
from playwright.async_api import Page

INSPECT_DOM_JS = """
(options) => {
    const showBadges = !!(options && options.showBadges);
    const oldOverlay = document.getElementById('__nusa_som_overlay__');
    if (oldOverlay) oldOverlay.remove();
    document.querySelectorAll('[data-nusa-idx]').forEach(el => el.removeAttribute('data-nusa-idx'));

    const isBoxRendered = (el, allowBelowFold = false) => {
        if (!el || !el.getBoundingClientRect) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
            return false;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) return false;
        if (!allowBelowFold) {
            if (rect.bottom < 0 || rect.top > window.innerHeight + 240) return false;
            if (rect.right < 0 || rect.left > window.innerWidth) return false;
        } else {
            if (rect.top > 3600 || rect.bottom < -1200) return false;
        }
        return true;
    };

    // Untuk <input type="radio|checkbox"> custom (Bootstrap/React dengan opacity:0), cek keterlihatan <label for="...">
    const getVisibleProxy = (el) => {
        const tag = (el.tagName || '').toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        const inForm = !!el.closest('form');
        if (isBoxRendered(el, inForm)) return el;
        if (tag === 'input' && (type === 'radio' || type === 'checkbox' || type === 'file')) {
            const idAttr = el.getAttribute('id') || '';
            if (idAttr) {
                const lbl = document.querySelector(`label[for="${CSS.escape(idAttr)}"]`);
                if (lbl && isBoxRendered(lbl, true)) return lbl;
            }
            const wrapLbl = el.closest('label');
            if (wrapLbl && isBoxRendered(wrapLbl, true)) return wrapLbl;
        }
        return null;
    };

    // Prioritaskan seluruh kontrol form agar tidak ada field di bawah fold yang terlewat
    const formSelector = 'form input:not([type="hidden"]), form textarea, form select, form button, button[type="submit"], input[type="submit"]';
    const generalSelector = [
        'input:not([type="hidden"])',
        'textarea',
        'select',
        'button',
        'a[href]',
        'a[class]',
        '[data-test]',
        '[data-testid]',
        '[role="button"]',
        '[role="combobox"]',
        '[role="listbox"]',
        '[role="option"]',
        '[role="checkbox"]',
        '[role="radio"]',
        '[role="tab"]',
        '[role="menuitem"]',
        'summary',
        '[onclick]',
        '[data-action]'
    ].join(',');

    const rawNodes = [
        ...Array.from(document.querySelectorAll(formSelector)),
        ...Array.from(document.querySelectorAll(generalSelector))
    ];

    // Tambahkan dropdown custom React-Select / SPA container (mis. #state, #city) & opsi yang sedang terbuka
    for (const cand of document.querySelectorAll('[id^="react-select-"], [class*="-option"], [class*="auto-complete__option"], div, li, span')) {
        if (rawNodes.length >= 160) break;
        const cls = String(cand.className || '');
        const cid = cand.getAttribute('id') || '';
        if (cid.includes('react-select-') || cls.includes('option') || cls.includes('auto-complete__option')) {
            rawNodes.push(cand);
            continue;
        }
        if (cand.children.length <= 6 && window.getComputedStyle(cand).cursor === 'pointer') {
            const txt = (cand.innerText || '').trim();
            if (txt.length >= 2 && txt.length <= 100) {
                rawNodes.push(cand);
            }
        }
    }

    const uniqueNodes = [];
    const seen = new Set();

    for (const el of rawNodes) {
        if (seen.has(el)) continue;
        seen.add(el);
        const proxy = getVisibleProxy(el);
        if (!proxy) continue;
        uniqueNodes.push({ el, proxy });
        if (uniqueNodes.length >= 95) break;
    }

    let overlay = null;
    if (showBadges) {
        overlay = document.createElement('div');
        overlay.id = '__nusa_som_overlay__';
        overlay.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;overflow:hidden;';
        document.body.appendChild(overlay);
    }

    const buildDomSelector = (el, tag, inputType, idAttr, nameAttr, placeholder, ariaLabel) => {
        if (idAttr) {
            const byId = `#${CSS.escape(idAttr)}`;
            if (document.querySelectorAll(byId).length === 1) return byId;
        }
        if (nameAttr && (inputType === 'radio' || inputType === 'checkbox') && el.value) {
            const byNV = `${tag}[name="${CSS.escape(nameAttr)}"][value="${CSS.escape(el.value)}"]`;
            if (document.querySelectorAll(byNV).length === 1) return byNV;
        }
        if (nameAttr) {
            const byName = `${tag}[name="${CSS.escape(nameAttr)}"]`;
            if (document.querySelectorAll(byName).length === 1) return byName;
        }
        if (placeholder) {
            const byPh = `${tag}[placeholder="${CSS.escape(placeholder)}"]`;
            if (document.querySelectorAll(byPh).length === 1) return byPh;
        }
        if (ariaLabel) {
            const byAria = `${tag}[aria-label="${CSS.escape(ariaLabel)}"]`;
            if (document.querySelectorAll(byAria).length === 1) return byAria;
        }
        return '';
    };

    const resolveFieldLabel = (el, idAttr, placeholder, ariaLabel, nameAttr) => {
        if (idAttr) {
            const lbl = document.querySelector(`label[for="${CSS.escape(idAttr)}"]`);
            if (lbl && (lbl.innerText || '').trim()) return (lbl.innerText || '').trim();
        }
        if (el.closest('label')) {
            const t = (el.closest('label').innerText || '').trim();
            if (t) return t;
        }
        // Cek kolom label di baris form Bootstrap (.row / .form-group / tr)
        const row = el.closest('.row, .mt-2, .form-group, .field, tr');
        if (row) {
            const rowLbl = row.querySelector('label.form-label, td:first-child, .col-md-3 label, label');
            if (rowLbl && !rowLbl.htmlFor) {
                const rt = (rowLbl.innerText || '').trim();
                if (rt && rt.length <= 60) return rt;
            }
        }
        return ariaLabel || placeholder || nameAttr || idAttr || '';
    };

    const resolveCompositeValue = (el, tag, inputType) => {
        if (tag === 'select') {
            const selOpt = el.selectedOptions && el.selectedOptions[0];
            return selOpt ? (selOpt.text || selOpt.value || '').trim() : (el.value || '');
        }
        // Cek chip multi-value / single-value React-Select (contoh: #subjectsContainer, #state, #city)
        const rsContainer = el.closest('#subjectsContainer, #state, #city') || el.closest('[class$="-container"]');
        if (rsContainer && (el.id.includes('subjects') || el.id.startsWith('react-select-') || (el.getAttribute('aria-autocomplete') === 'list'))) {
            let chips = Array.from(rsContainer.querySelectorAll('[class*="multi-value__label"]')).map(n => (n.innerText || '').trim()).filter(Boolean);
            if (!chips.length) {
                chips = Array.from(rsContainer.querySelectorAll('[class*="multiValue"] > div:first-child')).map(n => (n.innerText || '').trim()).filter(Boolean);
            }
            if (chips.length > 0) {
                const rawV = (el.value || '').trim();
                return rawV ? `${chips.join(', ')} (mengetik: ${rawV})` : chips.join(', ');
            }
            const single = rsContainer.querySelector('[class*="singleValue"], [class*="single-value"]');
            if (single && (single.innerText || '').trim()) {
                return (single.innerText || '').trim();
            }
        }
        if (inputType === 'checkbox' || inputType === 'radio') {
            return el.checked ? (el.value || 'checked') : '';
        }
        return (el.value !== undefined ? String(el.value) : (el.innerText || '')).trim();
    };

    const elements = [];
    let hasPasswordField = false;
    let hasOtpField = false;

    uniqueNodes.forEach(({ el, proxy }, idx) => {
        const idNum = idx + 1;
        el.setAttribute('data-nusa-idx', String(idNum));
        if (proxy !== el) proxy.setAttribute('data-nusa-proxy-idx', String(idNum));

        const rect = proxy.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const inputType = (el.getAttribute('type') || '').toLowerCase();
        const isPassword = (tag === 'input' && inputType === 'password');
        if (isPassword) hasPasswordField = true;

        const nameAttr = el.getAttribute('name') || '';
        const idAttr = el.getAttribute('id') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const ariaAuto = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
        const roleAttr = (el.getAttribute('role') || '').toLowerCase();

        if (/otp|2fa|verification|kode.?verifikasi|authenticator/i.test(nameAttr + ' ' + idAttr + ' ' + placeholder + ' ' + ariaLabel)) {
            hasOtpField = true;
        }

        const labelText = resolveFieldLabel(el, idAttr, placeholder, ariaLabel, nameAttr);
        const domSelector = buildDomSelector(el, tag, inputType, idAttr, nameAttr, placeholder, ariaLabel);
        let labelSelector = '';
        if (idAttr && document.querySelector(`label[for="${CSS.escape(idAttr)}"]`)) {
            labelSelector = `label[for="${CSS.escape(idAttr)}"]`;
        }

        const currentVal = isPassword ? (el.value ? '[PASSWORD TERISI]' : '') : resolveCompositeValue(el, tag, inputType);
        let innerText = ((proxy.innerText || el.innerText || '')).trim().replace(/\\s+/g, ' ');
        if (innerText.length > 90) innerText = innerText.slice(0, 90) + '...';

        const isAutocomplete = !!(
            ariaAuto === 'list' ||
            ariaAuto === 'both' ||
            roleAttr === 'combobox' ||
            /subject|autocomplete|typeahead|react-select/i.test(idAttr + ' ' + el.className)
        );

        let optionsList = [];
        if (tag === 'select') {
            optionsList = Array.from(el.options || []).slice(0, 20).map(o => ({
                value: o.value,
                text: (o.text || '').trim(),
                selected: !!o.selected
            }));
        }

        const maxLen = (typeof el.maxLength === 'number' && el.maxLength > 0 && el.maxLength < 524288) ? el.maxLength : null;
        const minLen = (typeof el.minLength === 'number' && el.minLength > 0) ? el.minLength : null;
        const patternAttr = el.getAttribute('pattern') || '';
        const isRequired = !!el.required || el.getAttribute('aria-required') === 'true';
        const isValid = (typeof el.checkValidity === 'function') ? el.checkValidity() : true;
        const validationMsg = el.validationMessage || '';
        const sensitiveAttr = el.getAttribute('data-sensitive') === 'true';

        elements.push({
            index: idNum,
            tag,
            type: inputType || tag,
            text: innerText,
            value: currentVal.slice(0, 160),
            label: labelText.slice(0, 80),
            placeholder: placeholder.slice(0, 80),
            aria_label: ariaLabel.slice(0, 80),
            name: nameAttr,
            el_id: idAttr,
            dom_selector: domSelector,
            label_selector: labelSelector,
            href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 140) : '',
            disabled: !!el.disabled,
            checked: !!el.checked,
            required: isRequired,
            max_length: maxLen,
            min_length: minLen,
            pattern: patternAttr,
            is_autocomplete: isAutocomplete,
            valid: isValid,
            validation_msg: validationMsg.slice(0, 120),
            is_password: isPassword,
            data_sensitive: sensitiveAttr,
            options: optionsList,
            rect: {
                x: Math.round(rect.left),
                y: Math.round(rect.top),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                centerX: Math.round(rect.left + rect.width / 2),
                centerY: Math.round(rect.top + rect.height / 2)
            }
        });

        if (showBadges && overlay && rect.top >= 0 && rect.top <= window.innerHeight) {
            const box = document.createElement('div');
            const color = isPassword ? '#f59e0b' : (sensitiveAttr ? '#ef4444' : '#06b6d4');
            box.style.cssText = `
                position:fixed;
                left:${Math.max(0, rect.left)}px;
                top:${Math.max(0, rect.top)}px;
                width:${rect.width}px;
                height:${rect.height}px;
                border:1.5px dashed ${color};
                border-radius:4px;
                box-sizing:border-box;
                pointer-events:none;
            `;
            const badge = document.createElement('span');
            badge.textContent = String(idNum);
            badge.style.cssText = `
                position:absolute;
                top:-16px;
                left:-2px;
                background:${color};
                color:#081018;
                font:700 10px/14px ui-monospace, SFMono-Regular, Menlo, monospace;
                padding:0 4px;
                border-radius:3px;
                box-shadow:0 1px 3px rgba(0,0,0,0.45);
            `;
            box.appendChild(badge);
            overlay.appendChild(box);
        }
    });

    // Ekstrak Tabel Hasil Submit (Modal / Summary Table) secara terstruktur untuk verifikasi pasca-submit!
    const resultTable = [];
    const modalRoot = document.querySelector('.modal.show, .modal-content, [role="dialog"]');
    const tablesToScan = modalRoot
        ? Array.from(modalRoot.querySelectorAll('table'))
        : Array.from(document.querySelectorAll('table'));

    for (const tbl of tablesToScan) {
        if (!isBoxRendered(tbl, true)) continue;
        for (const tr of Array.from(tbl.querySelectorAll('tbody tr, tr'))) {
            const cells = Array.from(tr.querySelectorAll('td, th')).map(c => (c.innerText || '').trim().replace(/\\s+/g, ' '));
            if (cells.length >= 2 && cells[0] && cells[0].toLowerCase() !== 'label') {
                resultTable.push({ label: cells[0], value: cells.slice(1).join(' | ') });
            }
        }
    }
    const modalTitle = modalRoot
        ? ((modalRoot.querySelector('.modal-title, h1, h2, h3, h4') || {}).innerText || '').trim()
        : '';

    // Extract clean visible page content
    const contentChunks = [];
    const textTargets = document.querySelectorAll('h1, h2, h3, h4, .modal-title, p, li, tr, article, [role="alert"], .card');
    const seenTexts = new Set();
    for (const node of textTargets) {
        if (!isBoxRendered(node, false)) continue;
        const tag = node.tagName.toLowerCase();
        if (tag === 'li' && node.querySelectorAll('li').length > 0) continue;
        let t = (node.innerText || '').trim().replace(/\\s+/g, ' ');
        if (t.length < 3) continue;
        if (t.length > 260) t = t.slice(0, 260) + '...';
        if (seenTexts.has(t)) continue;
        seenTexts.add(t);
        const prefix = /^h[1-4]$/.test(tag) ? `[${tag.toUpperCase()}] ` : '';
        contentChunks.push(prefix + t);
        if (contentChunks.length >= 50) break;
    }

    const bodyTextSample = (document.body ? document.body.innerText : '').slice(0, 3500);
    const hasCaptcha = /recaptcha|hcaptcha|cloudflare.*verify.*human|verifikasi.*bukan.*robot/i.test(
        document.documentElement.innerHTML.slice(0, 15000)
    );
    const wasValidated = !!document.querySelector('form.was-validated, .is-invalid, input:invalid:focus');

    return {
        url: window.location.href,
        title: document.title || '',
        modal_title: modalTitle,
        was_validated: wasValidated,
        result_table: resultTable,
        scroll: {
            y: Math.round(window.scrollY),
            maxY: Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight)),
            viewportHeight: window.innerHeight,
            viewportWidth: window.innerWidth
        },
        signals: {
            has_password_field: hasPasswordField,
            has_otp_field: hasOtpField,
            has_captcha: hasCaptcha
        },
        elements,
        readable_text: contentChunks.join('\\n'),
        raw_excerpt: bodyTextSample.replace(/\\n{3,}/g, '\\n\\n').trim()
    };
}
"""


async def inspect_page(page: Page, show_badges: bool = False) -> Dict[str, Any]:
    """
    Membaca tampilan halaman website: mengambil daftar elemen interaktif beserta selector DOM deterministik,
    atribut validasi HTML, tabel hasil submit, sinyal keamanan, serta isi teks halaman.
    """
    try:
        data = await page.evaluate(INSPECT_DOM_JS, {"showBadges": show_badges})
        return data
    except Exception as exc:
        return {
            "url": page.url,
            "title": "",
            "modal_title": "",
            "was_validated": False,
            "result_table": [],
            "scroll": {"y": 0, "maxY": 0, "viewportHeight": 600, "viewportWidth": 800},
            "signals": {"has_password_field": False, "has_otp_field": False, "has_captcha": False},
            "elements": [],
            "readable_text": f"[Gagal membaca DOM sementara: {exc}]",
            "raw_excerpt": "",
        }


async def clear_som_badges(page: Page) -> None:
    try:
        await page.evaluate(
            "() => { const el = document.getElementById('__nusa_som_overlay__'); if (el) el.remove(); }"
        )
    except Exception:
        pass


def format_dom_for_llm(dom_state: Dict[str, Any]) -> str:
    """Merangkum tampilan halaman, atribut form lengkap, dan tabel hasil submit untuk prompt AI."""
    lines = [
        f"URL Saat Ini: {dom_state.get('url', '')}",
        f"Judul Halaman: {dom_state.get('title', '')}",
    ]
    scroll = dom_state.get("scroll", {})
    lines.append(f"Posisi Scroll: Y={scroll.get('y', 0)} / Max={scroll.get('maxY', 0)}")

    signals = dom_state.get("signals", {})
    active_signals = [k for k, v in signals.items() if v]
    if active_signals:
        lines.append(f"SINYAL KEAMANAN HALAMAN: {', '.join(active_signals)} (JANGAN ketik password sendiri, gunakan request_human_login!)")

    # Tampilkan Tabel Hasil Submit paling atas jika muncul agar AI wajib memverifikasi tiap baris!
    result_table: List[Dict[str, str]] = dom_state.get("result_table") or []
    if result_table:
        m_title = dom_state.get("modal_title") or "Tabel Hasil"
        lines.append(f"\n=== TABEL HASIL SUBMIT TERDETEKSI ({m_title}) — WAJIB VERIFIKASI SATU PER SATU ===")
        for row in result_table:
            lines.append(f"- {row.get('label', '')}: '{row.get('value', '')}'")

    lines.append("\n=== ELEMEN INTERAKTIF & FORMULIR [index] (berdasarkan DOM Selector & Label) ===")
    elements = dom_state.get("elements", [])
    if not elements:
        lines.append("(Tidak ada elemen interaktif saat ini)")
    else:
        for el in elements[:85]:
            idx = el["index"]
            tag = el["tag"]
            etype = el["type"]
            desc_parts = []
            if el.get("dom_selector"):
                desc_parts.append(f"sel='{el['dom_selector']}'")
            if el.get("label"):
                desc_parts.append(f"label='{el['label']}'")
            if el.get("placeholder"):
                desc_parts.append(f"placeholder='{el['placeholder']}'")
            if el.get("name"):
                desc_parts.append(f"name='{el['name']}'")
            if el.get("value"):
                desc_parts.append(f"CURRENT_VALUE='{el['value']}'")
            elif tag in ("input", "textarea", "select") and etype not in ("radio", "checkbox", "submit", "button"):
                desc_parts.append("CURRENT_VALUE=(KOSONG)")
            if etype in ("radio", "checkbox"):
                desc_parts.append("CHECKED=True" if el.get("checked") else "CHECKED=False")
            if el.get("text") and el.get("text") != el.get("label"):
                desc_parts.append(f"text='{el['text']}'")
            if el.get("required"):
                desc_parts.append("[REQUIRED]")
            if el.get("max_length"):
                desc_parts.append(f"maxlength={el['max_length']}")
            if el.get("pattern"):
                desc_parts.append(f"pattern='{el['pattern']}'")
            if el.get("is_autocomplete"):
                desc_parts.append("[AUTOCOMPLETE_INPUT]")
            if not el.get("valid", True) and dom_state.get("was_validated"):
                desc_parts.append(f"[INVALID: {el.get('validation_msg', 'format/wajib diisi')}]")
            if el.get("options"):
                opts = ", ".join(o["text"] for o in el["options"][:8])
                desc_parts.append(f"options=[{opts}]")
            if el.get("href"):
                desc_parts.append(f"href='{el['href']}'")
            if el.get("data_sensitive"):
                desc_parts.append("[SENSITIVE_ACTION]")
            if el.get("is_password"):
                desc_parts.append("[PASSWORD_FIELD]")
            desc = " | ".join(desc_parts) if desc_parts else "(tanpa label)"
            lines.append(f"[{idx}] <{tag} type='{etype}'> {desc}")

    lines.append("\n=== RINGKASAN TEKS TAMPILAN WEBSITE ===")
    readable = dom_state.get("readable_text") or dom_state.get("raw_excerpt", "")
    lines.append(readable[:2800] if readable else "(Halaman kosong)")
    return "\n".join(lines)
