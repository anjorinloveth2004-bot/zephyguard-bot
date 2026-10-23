"""Small dict-based i18n for the most common group strings (help, welcome, moderation notices).

Languages are stored per group (groups.lang). Anything missing falls back to English, so adding a
language never breaks a reply. Keep keys short and copy lowercase-friendly.
"""
from __future__ import annotations

LANGS: dict[str, str] = {
    "en": "english",
    "es": "español",
    "fr": "français",
    "pt": "português",
    "hi": "हिन्दी",
    "ar": "العربية",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "help": (
            "commands: /ask, /summary, /points, /lb, /rules, /settings (admins, opens dm)\n"
            "notes: /save name, /notes, /get name or #name\n"
            "filters: /filter word reply, /filters, /stop word\n"
            "admins: /warn, /ban, /kick, /mute, /purge (reply), /award N reason (reply)\n"
            "more: /pin, /unpin, /pinned, /report (reply), /lang, /export, /import, /disable"
        ),
        "welcome": "welcome {name} to {group}",
        "warn": "⚠️ {name} warned ({count}/{limit}): {reason}",
        "warn_max": "⛔ {name} hit {limit}/{limit} warnings ({reason}). action: {action}.",
        "done": "done: {action} {name}.",
        "flood": "🌊 {name} is flooding. action: {action}.",
        "disabled": "{cmd} is disabled in this group.",
        "admins_only": "admins only.",
        "lang_set": "language set to {lang}.",
        "lang_bad": "unknown language. pick one of: {langs}",
        "approve_prompt": "welcome {name}! tap the button within 10 minutes to be approved (only you can tap it).",
        "raid": "⚠️ raid mode on: {n}+ joins in a short time. new members need approval for 10 minutes.",
        "approved": "approved ✅ welcome {name}.",
        "report_sent": "report sent to the admins.",
    },
    "es": {
        "help": (
            "comandos: /ask, /summary, /points, /lb, /rules, /settings (admins, abre dm)\n"
            "notas: /save nombre, /notes, /get nombre o #nombre\n"
            "filtros: /filter palabra respuesta, /filters, /stop palabra\n"
            "admins: /warn, /ban, /kick, /mute, /purge (responde), /award N motivo (responde)\n"
            "mas: /pin, /unpin, /pinned, /report (responde), /lang, /export, /import, /disable"
        ),
        "welcome": "bienvenido {name} a {group}",
        "warn": "⚠️ {name} advertido ({count}/{limit}): {reason}",
        "warn_max": "⛔ {name} llegó a {limit}/{limit} advertencias ({reason}). acción: {action}.",
        "done": "hecho: {action} {name}.",
        "flood": "🌊 {name} está enviando demasiados mensajes. acción: {action}.",
        "disabled": "{cmd} está desactivado en este grupo.",
        "admins_only": "solo admins.",
        "lang_set": "idioma cambiado a {lang}.",
        "lang_bad": "idioma desconocido. elige uno de: {langs}",
        "approve_prompt": "¡bienvenido {name}! toca el botón en 10 minutos para ser aprobado (solo tú puedes tocarlo).",
        "approved": "aprobado ✅ bienvenido {name}.",
        "report_sent": "reporte enviado a los admins.",
    },
    "fr": {
        "help": (
            "commandes : /ask, /summary, /points, /lb, /rules, /settings (admins, ouvre le dm)\n"
            "notes : /save nom, /notes, /get nom ou #nom\n"
            "filtres : /filter mot réponse, /filters, /stop mot\n"
            "admins : /warn, /ban, /kick, /mute, /purge (répondre), /award N raison (répondre)\n"
            "plus : /pin, /unpin, /pinned, /report (répondre), /lang, /export, /import, /disable"
        ),
        "welcome": "bienvenue {name} dans {group}",
        "warn": "⚠️ {name} averti ({count}/{limit}) : {reason}",
        "warn_max": "⛔ {name} a atteint {limit}/{limit} avertissements ({reason}). action : {action}.",
        "done": "fait : {action} {name}.",
        "flood": "🌊 {name} spamme. action : {action}.",
        "disabled": "{cmd} est désactivé dans ce groupe.",
        "admins_only": "réservé aux admins.",
        "lang_set": "langue définie sur {lang}.",
        "lang_bad": "langue inconnue. choisis parmi : {langs}",
        "approve_prompt": "bienvenue {name} ! appuie sur le bouton dans les 10 minutes pour être approuvé (toi seul peux appuyer).",
        "approved": "approuvé ✅ bienvenue {name}.",
        "report_sent": "signalement envoyé aux admins.",
    },
    "pt": {
        "help": (
            "comandos: /ask, /summary, /points, /lb, /rules, /settings (admins, abre o dm)\n"
            "notas: /save nome, /notes, /get nome ou #nome\n"
            "filtros: /filter palavra resposta, /filters, /stop palavra\n"
            "admins: /warn, /ban, /kick, /mute, /purge (responda), /award N motivo (responda)\n"
            "mais: /pin, /unpin, /pinned, /report (responda), /lang, /export, /import, /disable"
        ),
        "welcome": "bem-vindo {name} a {group}",
        "warn": "⚠️ {name} advertido ({count}/{limit}): {reason}",
        "warn_max": "⛔ {name} chegou a {limit}/{limit} advertências ({reason}). ação: {action}.",
        "done": "feito: {action} {name}.",
        "flood": "🌊 {name} está flodando. ação: {action}.",
        "disabled": "{cmd} está desativado neste grupo.",
        "admins_only": "só admins.",
        "lang_set": "idioma definido para {lang}.",
        "lang_bad": "idioma desconhecido. escolha um de: {langs}",
        "approve_prompt": "bem-vindo {name}! toque no botão em 10 minutos para ser aprovado (só você pode tocar).",
        "approved": "aprovado ✅ bem-vindo {name}.",
        "report_sent": "denúncia enviada aos admins.",
    },
    "hi": {
        "help": (
            "कमांड: /ask, /summary, /points, /lb, /rules, /settings (एडमिन, dm खोलता है)\n"
            "नोट्स: /save नाम, /notes, /get नाम या #नाम\n"
            "फ़िल्टर: /filter शब्द जवाब, /filters, /stop शब्द\n"
            "एडमिन: /warn, /ban, /kick, /mute, /purge (जवाब दें), /award N कारण (जवाब दें)\n"
            "और: /pin, /unpin, /pinned, /report (जवाब दें), /lang, /export, /import, /disable"
        ),
        "welcome": "{group} में स्वागत है {name}",
        "warn": "⚠️ {name} को चेतावनी दी गई ({count}/{limit}): {reason}",
        "warn_max": "⛔ {name} की {limit}/{limit} चेतावनियाँ पूरी हुईं ({reason})। कार्रवाई: {action}।",
        "done": "हो गया: {action} {name}।",
        "flood": "🌊 {name} बहुत ज़्यादा संदेश भेज रहे हैं। कार्रवाई: {action}।",
        "disabled": "इस ग्रुप में {cmd} बंद है।",
        "admins_only": "सिर्फ़ एडमिन।",
        "lang_set": "भाषा {lang} कर दी गई।",
        "lang_bad": "अज्ञात भाषा। इनमें से चुनें: {langs}",
        "approve_prompt": "स्वागत है {name}! 10 मिनट में बटन दबाकर स्वीकृति लें (सिर्फ़ आप दबा सकते हैं)।",
        "approved": "स्वीकृत ✅ स्वागत है {name}।",
        "report_sent": "रिपोर्ट एडमिन को भेज दी गई।",
    },
    "ar": {
        "help": (
            "الأوامر: /ask، /summary، /points، /lb، /rules، /settings (للمشرفين، يفتح الخاص)\n"
            "الملاحظات: /save اسم، /notes، /get اسم أو #اسم\n"
            "الفلاتر: /filter كلمة رد، /filters، /stop كلمة\n"
            "المشرفون: /warn، /ban، /kick، /mute، /purge (بالرد)، /award N سبب (بالرد)\n"
            "المزيد: /pin، /unpin، /pinned، /report (بالرد)، /lang، /export، /import، /disable"
        ),
        "welcome": "مرحباً {name} في {group}",
        "warn": "⚠️ تم تحذير {name} ({count}/{limit}): {reason}",
        "warn_max": "⛔ {name} وصل إلى {limit}/{limit} تحذيرات ({reason}). الإجراء: {action}.",
        "done": "تم: {action} {name}.",
        "flood": "🌊 {name} يرسل رسائل كثيرة جداً. الإجراء: {action}.",
        "disabled": "الأمر {cmd} معطل في هذه المجموعة.",
        "admins_only": "للمشرفين فقط.",
        "lang_set": "تم تعيين اللغة إلى {lang}.",
        "lang_bad": "لغة غير معروفة. اختر واحدة من: {langs}",
        "approve_prompt": "مرحباً {name}! اضغط الزر خلال 10 دقائق للموافقة (أنت فقط تستطيع الضغط).",
        "approved": "تمت الموافقة ✅ مرحباً {name}.",
        "report_sent": "تم إرسال البلاغ إلى المشرفين.",
    },
}


def t(lang: str | None, key: str, **kw) -> str:
    """Translate a key for a language code, falling back to English. Unknown keys raise KeyError."""
    table = STRINGS.get(lang or "en", {})
    raw = table.get(key) or STRINGS["en"][key]
    return raw.format(**kw) if kw else raw


def normalize_lang(code: str | None) -> str:
    code = (code or "").strip().lower()
    return code if code in LANGS else "en"
