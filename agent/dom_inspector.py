from typing import Any, Dict
from playwright.async_api import Page

INSPECT_DOM_JS = """
(options) => {
    const showBadges = !!(options && options.showBadges);
    const oldOverlay = document.getElementById('__nusa_som_overlay__');
    if (oldOverlay) oldOverlay.remove();
    document.querySelectorAll('[data-nusa-idx]').forEach(el => el.removeAttribute('data-nusa-idx'));

    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
            return false;
        }
        const rect = el.getBoundingClientRect();
        if (rect.width < 6 || rect.height < 6) return false;
        // Element within viewport or slightly below
        if (rect.bottom < 0 || rect.top > window.innerHeight + 180) return false;
        if (rect.right < 0 || rect.left > window.innerWidth) return false;
        return true;
    };

    const selector = [
        'a[href]',
        'button',
        'input:not([type="hidden"])',
        'textarea',
        'select',
        '[role="button"]',
        '[role="link"]',
        '[role="tab"]',
        '[role="menuitem"]',
        '[role="checkbox"]',
        'summary',
        '[onclick]',
        '[data-action]'
    ].join(',');

    const rawNodes = Array.from(document.querySelectorAll(selector));
    const uniqueNodes = [];
    const seen = new Set();

    for (const el of rawNodes) {
        if (seen.has(el)) continue;
        seen.add(el);
        if (!isVisible(el)) continue;
        uniqueNodes.push(el);
        if (uniqueNodes.length >= 75) break;
    }

    let overlay = null;
    if (showBadges) {
        overlay = document.createElement('div');
        overlay.id = '__nusa_som_overlay__';
        overlay.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;overflow:hidden;';
        document.body.appendChild(overlay);
    }

    const elements = [];
    let hasPasswordField = false;
    let hasOtpField = false;

    uniqueNodes.forEach((el, idx) => {
        const idNum = idx + 1;
        el.setAttribute('data-nusa-idx', String(idNum));
        const rect = el.getBoundingClientRect();
        const tag = el.tagName.toLowerCase();
        const inputType = (el.getAttribute('type') || '').toLowerCase();
        const isPassword = (tag === 'input' && inputType === 'password');
        if (isPassword) hasPasswordField = true;

        const nameAttr = el.getAttribute('name') || '';
        const idAttr = el.getAttribute('id') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';

        if (/otp|2fa|verification|kode.?verifikasi|authenticator/i.test(nameAttr + ' ' + idAttr + ' ' + placeholder + ' ' + ariaLabel)) {
            hasOtpField = true;
        }

        // Find associated <label> text if available
        let labelText = '';
        if (idAttr) {
            const lbl = document.querySelector(`label[for="${CSS.escape(idAttr)}"]`);
            if (lbl) labelText = (lbl.innerText || '').trim();
        }
        if (!labelText && el.closest('label')) {
            labelText = (el.closest('label').innerText || '').trim();
        }

        let innerText = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
        if (isPassword) {
            innerText = el.value ? '[PASSWORD TERISI]' : '';
        }
        if (innerText.length > 90) innerText = innerText.slice(0, 90) + '...';

        let optionsList = [];
        if (tag === 'select') {
            optionsList = Array.from(el.options || []).slice(0, 12).map(o => ({
                value: o.value,
                text: (o.text || '').trim(),
                selected: !!o.selected
            }));
        }

        const sensitiveAttr = el.getAttribute('data-sensitive') === 'true';

        elements.push({
            index: idNum,
            tag,
            type: inputType || tag,
            text: innerText,
            label: labelText.slice(0, 80),
            placeholder: placeholder.slice(0, 80),
            aria_label: ariaLabel.slice(0, 80),
            name: nameAttr,
            el_id: idAttr,
            href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 140) : '',
            disabled: !!el.disabled,
            checked: !!el.checked,
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

    // Extract clean visible page content (headings, articles, tables, key paragraphs)
    const contentChunks = [];
    const textTargets = document.querySelectorAll('h1, h2, h3, p, li, tr, article, [role="alert"], .card, .item, .athing');
    const seenTexts = new Set();
    for (const node of textTargets) {
        if (!isVisible(node)) continue;
        const tag = node.tagName.toLowerCase();
        if (tag === 'li' && node.querySelectorAll('li').length > 0) continue;
        let t = (node.innerText || '').trim().replace(/\\s+/g, ' ');
        if (t.length < 4) continue;
        if (t.length > 260) t = t.slice(0, 260) + '...';
        if (seenTexts.has(t)) continue;
        seenTexts.add(t);
        const prefix = /^h[1-3]$/.test(tag) ? `[${tag.toUpperCase()}] ` : '';
        contentChunks.push(prefix + t);
        if (contentChunks.length >= 45) break;
    }

    const bodyTextSample = (document.body ? document.body.innerText : '').slice(0, 3500);
    const hasCaptcha = /recaptcha|hcaptcha|cloudflare.*verify.*human|verifikasi.*bukan.*robot/i.test(
        document.documentElement.innerHTML.slice(0, 15000)
    );

    return {
        url: window.location.href,
        title: document.title || '',
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
    Membaca tampilan halaman website: mengambil daftar elemen interaktif beserta koordinat,
    sinyal keamanan (form login/password/OTP/CAPTCHA), serta isi teks utama halaman.
    """
    try:
        data = await page.evaluate(INSPECT_DOM_JS, {"showBadges": show_badges})
        return data
    except Exception as exc:
        return {
            "url": page.url,
            "title": "",
            "scroll": {"y": 0, "maxY": 0, "viewportHeight": 800, "viewportWidth": 1280},
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
    """Merangkum tampilan halaman ke format ringkas dan presisi untuk prompt AI."""
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

    lines.append("\n=== ELEMEN INTERAKTIF YANG TERLIHAT [index] ===")
    elements = dom_state.get("elements", [])
    if not elements:
        lines.append("(Tidak ada elemen interaktif di viewport saat ini)")
    else:
        for el in elements[:60]:
            idx = el["index"]
            tag = el["tag"]
            etype = el["type"]
            desc_parts = []
            if el.get("label"):
                desc_parts.append(f"label='{el['label']}'")
            if el.get("text"):
                desc_parts.append(f"text='{el['text']}'")
            if el.get("placeholder"):
                desc_parts.append(f"placeholder='{el['placeholder']}'")
            if el.get("aria_label"):
                desc_parts.append(f"aria='{el['aria_label']}'")
            if el.get("name"):
                desc_parts.append(f"name='{el['name']}'")
            if el.get("href"):
                desc_parts.append(f"href='{el['href']}'")
            if el.get("options"):
                opts = ", ".join(o["text"] for o in el["options"][:6])
                desc_parts.append(f"options=[{opts}]")
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
