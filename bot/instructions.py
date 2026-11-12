"""Telegram-sized client instructions, using the verified download catalog."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .subscription_copy import plan_overview

DOWNLOADS_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "client-guides"
    / "client-downloads.json"
)
DOWNLOADS = json.loads(DOWNLOADS_PATH.read_text(encoding="utf-8"))
KARING_IOS = DOWNLOADS["alternatives"]["ios_karing"]


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _downloads(platform: str) -> str:
    item = DOWNLOADS["platforms"][platform]
    links = []
    if item["store_url"]:
        store = {
            "android": "Google Play",
            "ios": "App Store",
            "windows": "Microsoft Store",
            "tv": "Google Play",
        }[platform]
        links.append(_link(item["store_url"], store))
    # Bounded, useful alternatives; full architecture list is in the guide.
    links.extend(_link(entry["url"], entry["label"]) for entry in item["downloads"][:2])
    return " · ".join(links)


_COMMON = (
    "2. В боте откройте <b>«Подписка»</b>, выберите тариф. Затем "
    "откройте <b>«Мои ключи»</b>. Если ключей ещё нет, нажмите "
    "<b>«Выпустить / проверить ключи бесплатно»</b>. Если они готовы, "
    "выберите нужный сервис.\n"
    "3. Выберите сервис. <b>Hysteria</b>: сохраните файл "
    "<code>vpn-by-andriyshkoy-hysteria.json</code> или скопируйте весь текст "
    "из сообщения: первую строку <code>// profile-title</code> и JSON целиком. "
    "<b>VLESS + REALITY</b>: скопируйте "
    "всю ссылку <code>vless://…</code>.\n"
    "4. В Hiddify для JSON-файла нажмите <b>«+» → «Файл / File»</b>. "
    "Для профиля из буфера или ссылки VLESS выберите "
    "<b>«+» → «Из буфера обмена / Clipboard»</b>.\n"
    "5. Выберите добавленный профиль, нажмите большую кнопку подключения "
    "и разрешите создание VPN-подключения.\n"
    "6. Откройте сайт или видео. Перед проверкой отключите другие VPN.\n\n"
    + plan_overview()
    + "\n\n"
    "Автоматическое переключение между этими профилями не включается ботом. "
    "В JSON Hysteria уже настроена проверка сертификата: не удаляйте "
    "параметры TLS и не включайте insecure.\n\n"
)

_FOOTER = (
    _link(DOWNLOADS["client"]["website"], "Официальный сайт")
    + " · "
    + _link(DOWNLOADS["client"]["github"], "GitHub")
    + " · "
    + _link(DOWNLOADS["client"]["official_use_guide"], "Инструкция Hiddify")
    + "\n\n🔐 JSON и ссылка содержат доступ к VPN. Не публикуйте их и не вставляйте на посторонние сайты."
)

GUIDE_MENU_TEXT = (
    "📚 <b>Подключение VPN</b>\n\n"
    "1. Выберите устройство ниже. Для iPhone есть отдельные инструкции "
    "Hiddify и Karing. Если Hiddify нет в App Store, Karing подходит для "
    "<b>VLESS + REALITY в PLUS</b>. Для нашей Hysteria используйте Hiddify.\n"
    "2. В <b>«Подписка»</b> выберите тариф. Выпуск и замена ключей бесплатны.\n"
    "3. В <b>«Мои ключи»</b> получите профиль сервиса. Для импорта в Hiddify: "
    "<b>Hysteria</b> — "
    "сохраните JSON и импортируйте через <b>«+» → «Файл / File»</b>, "
    "либо скопируйте весь текст, включая первую строку <code>// profile-title</code> "
    "и JSON, через <b>Clipboard</b>. "
    "<b>VLESS + REALITY</b>: целая ссылка <code>vless://…</code> через "
    "<b>«+» → «Из буфера обмена / Clipboard»</b>.\n\n"
    + plan_overview()
    + "\n\n"
    + _FOOTER
)


def _guide(platform: str) -> str:
    item = DOWNLOADS["platforms"][platform]
    text = f"📱 <b>{html.escape(item['label'])} — Hiddify</b>\n\n"
    text += f"Требования: {html.escape(item['minimum_os'])}.\n\n"
    text += "1. Установите Hiddify: " + _downloads(platform) + ".\n"
    if platform == "android":
        text += "Если Play недоступен, скачайте официальный APK. Разрешите установку для браузера; после установки разрешение можно отключить.\n"
    elif platform == "ios":
        text += (
            "Если Hiddify недоступен в вашем App Store, для <b>VLESS + REALITY "
            "в PLUS</b> откройте отдельную инструкцию <b>«iPhone / iPad — Karing»</b> "
            "в списке устройств. Для <b>Hysteria / BASIC</b> используйте Hiddify "
            "или напишите в поддержку.\n"
        )
    elif platform == "windows":
        text += "Для режима всего устройства запустите Hiddify с правами администратора и включите VPN/TUN. Для отдельных приложений можно использовать системный прокси, но он покрывает не весь трафик.\n"
    elif platform == "macos":
        text += "Откройте DMG, перенесите Hiddify в «Программы». При запросе разрешите VPN. Используйте официальный DMG для Mac.\n"
    elif platform == "linux":
        text += "DEB подходит Debian/Ubuntu; AppImage требует разрешения на запуск и совместимых библиотек. Для TUN нужны системные права; не запускайте команды из случайных инструкций.\n"
    return text + _COMMON + _FOOTER


GUIDES = {
    platform: _guide(platform)
    for platform in ("windows", "macos", "android", "ios", "linux")
}
GUIDES["ios_karing"] = (
    "📱 <b>iPhone / iPad — Karing</b>\n\n"
    "Если Hiddify недоступен в App Store, этот вариант подходит для "
    "<b>VLESS + REALITY в PLUS — 100 ₽/месяц</b>.\n"
    "Для <b>Hysteria / BASIC</b> используйте Hiddify или напишите в поддержку: "
    "безопасный импорт нашего JSON Hysteria в Karing пока не подтверждён. "
    "Не отключайте проверку сертификата.\n\n"
    "1. Установите бесплатный <b>Karing</b>: "
    + _link(KARING_IOS["store_url"], "App Store")
    + ". Разработчик — <b>Supernova Nebula LLC</b>. Требуется "
    + html.escape(KARING_IOS["minimum_os"])
    + ". Наличие приложения зависит от региона магазина.\n"
    "2. В боте откройте <b>«Мои ключи» → «Ключ VLESS + REALITY»</b>. "
    "Скопируйте всю ссылку <code>vless://…</code> без подписи сообщения. "
    "Выпуск и замена ключей бесплатны.\n"
    "3. В Karing откройте <b>«Добавить профиль» → "
    "«Импорт из буфера обмена»</b>. Если iOS спросит разрешение вставить "
    "текст, разрешите вставку.\n"
    "4. Если появится поле <b>«Примечание»</b>, задайте название, например "
    "<b>VPN by @andriyshkoy</b>. Сохраните профиль кнопкой <b>✓</b> вверху справа.\n"
    "5. На главном экране выберите добавленный сервер и включите подключение. "
    "В системном запросе на добавление VPN нажмите <b>«Разрешить / Allow»</b> "
    "и подтвердите действие кодом устройства, если потребуется.\n"
    "6. Отключите другие VPN, откройте YouTube или Telegram и запустите видео. "
    "Для отключения используйте переключатель в Karing.\n\n"
    "В PLUS можно вручную переключаться между VLESS в Karing и Hysteria "
    "в Hiddify. Перед переключением отключите текущее VPN-подключение. "
    "Отключение приложения не приостанавливает подписку — это делается "
    "в разделе <b>«Подписка»</b> бота.\n\n"
    + _link(KARING_IOS["website"], "Сайт Karing")
    + " · "
    + _link(KARING_IOS["github"], "GitHub")
    + " · "
    + _link(KARING_IOS["official_use_guide"], "Официальная инструкция")
    + '\n\nНужна помощь? <a href="https://t.me/andriyshkoy">Напишите в поддержку</a>. '
    "Ссылка содержит доступ к VPN: не публикуйте её и скрывайте на снимках экрана."
)
GUIDES["tv"] = (
    "📺 <b>Android TV / Google TV</b>\n\n"
    "Hiddify поддерживает Android TV с версии 5.5.5. Другие системы ТВ "
    "(Apple TV, Tizen, webOS) этой инструкцией не поддерживаются.\n\n"
    "1. Установите Hiddify: "
    + _downloads("tv")
    + ". Если приложения нет в ТВ-магазине, используйте официальный universal APK.\n"
    "2. В разделе «Мои ключи» получите профиль сервиса. Для Hysteria "
    "передайте <code>vpn-by-andriyshkoy-hysteria.json</code> на TV и выберите "
    "<b>«+» → «Файл / File»</b>. Либо вставьте весь текст, включая первую "
    "строку <code>// profile-title</code> и JSON, "
    "через Clipboard. Для VLESS вставьте всю ссылку <code>vless://…</code> "
    "через <b>«+» → «Из буфера обмена»</b>. Возможности передачи зависят от TV.\n"
    "3. Выберите профиль, подключитесь и разрешите VPN. Проверьте видео.\n\n"
    "На роутере Hiddify не устанавливается как обычное приложение. "
    "Настройка зависит от модели и прошивки — напишите в поддержку.\n\n" + _FOOTER
)
GUIDES["troubleshooting"] = (
    "🛠 <b>Если VPN не подключается</b>\n\n"
    "1. Без VPN проверьте обычный интернет. Включите автоматические дату и время.\n"
    "2. В боте проверьте «Подписка» и состояние ключа. Если доступ на паузе, "
    "возобновите его. Если ключ создаётся, подождите и снова откройте «Мои ключи».\n"
    "3. Обновите Hiddify из официального источника. Hysteria импортируйте "
    "файлом JSON или скопируйте весь текст, включая первую строку "
    "<code>// profile-title</code> и JSON. Для VLESS нужна "
    "вся ссылка <code>vless://…</code>. JSON не вставляйте в поле URL подписки.\n"
    "4. Отключите другой VPN, проверьте разрешение VPN/TUN. На Android "
    "разрешите Hiddify работать в фоне.\n"
    "5. BASIC допускает одно подключение одновременно: отключите этот "
    "ключ на другом устройстве. PLUS: попробуйте другой профиль сервиса.\n"
    "6. Сравните Wi-Fi и мобильную сеть. Hysteria использует UDP: некоторые "
    "сети его ограничивают; в PLUS попробуйте VLESS + REALITY. Не меняйте "
    "проверку TLS-сертификатов и не включайте insecure.\n"
    "7. После замены ключа импортируйте новый JSON Hysteria или новую ссылку "
    "VLESS — прежний доступ отключается на всех устройствах.\n\n"
    'Не помогло? <a href="https://t.me/andriyshkoy">Напишите в поддержку</a>: '
    "устройство, ОС, версия Hiddify, оператор, тип сети и время ошибки. "
    "Приложите текст ошибки без ключей, токенов и личных данных.\n\n" + _FOOTER
)
