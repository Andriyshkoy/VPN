from __future__ import annotations

GUIDE_MENU_TEXT = (
    "📚 <b>Как подключить VPN</b>\n\n"
    "1. Откройте <b>«Мои конфиги»</b> и скачайте файл "
    "<code>.ovpn</code>.\n"
    "2. Выберите своё устройство ниже.\n"
    "3. Установите клиент, импортируйте файл и включите подключение.\n\n"
    "🔐 <b>Важно:</b> файл <code>.ovpn</code> содержит персональные ключи "
    "доступа. Не пересылайте его другим людям и не загружайте на сторонние "
    "сайты."
)

GUIDES = {
    "windows": (
        "🪟 <b>Windows 10 / 11</b>\n\n"
        "1. Установите официальный "
        '<a href="https://openvpn.net/connect-docs/connect-for-windows.html">'
        "OpenVPN Connect</a>.\n"
        "2. Откройте приложение и примите условия использования.\n"
        "3. Дважды нажмите на скачанный <code>.ovpn</code>-файл. Если он не "
        "открылся автоматически: <b>Menu → My Profiles → + → Upload File</b>.\n"
        "4. Подтвердите импорт кнопкой <b>OK</b>.\n"
        "5. Включите переключатель рядом с профилем и подтвердите изменение "
        "сетевых настроек Windows.\n\n"
        "Статус <b>Connected</b> означает, что VPN работает ✅"
    ),
    "macos": (
        "🍎 <b>macOS</b>\n\n"
        "1. Установите официальный "
        '<a href="https://openvpn.net/connect-docs/macos-installation-guide.html">'
        "OpenVPN Connect</a>. На странице выберите версию для Apple Silicon "
        "или Intel.\n"
        "2. Откройте приложение из папки <b>Applications</b>.\n"
        "3. Перетащите <code>.ovpn</code>-файл в окно приложения. Также можно "
        "дважды нажать на файл или выбрать "
        "<b>Menu → My Profiles → + → Upload File</b>.\n"
        "4. Нажмите <b>OK</b> и включите профиль.\n"
        "5. Разрешите изменение настроек VPN в системном окне macOS.\n\n"
        "Статус <b>Connected</b> означает, что VPN работает ✅"
    ),
    "ios": (
        "📱 <b>iPhone / iPad</b>\n\n"
        "Нужна iOS или iPadOS 15 и новее.\n\n"
        "1. Установите "
        '<a href="https://apps.apple.com/app/openvpn-connect/id590379981">'
        "OpenVPN Connect</a> из App Store.\n"
        "2. Скачайте <code>.ovpn</code> в боте. Если файл не открылся, "
        "выберите <b>Поделиться → Сохранить в Файлы</b>.\n"
        "3. В OpenVPN Connect откройте "
        "<b>Menu → My Profiles → + → Upload File</b>.\n"
        "4. Выберите файл, нажмите <b>Add</b>, затем <b>Connect</b>.\n"
        "5. Разрешите iOS добавить конфигурацию VPN.\n\n"
        "Значок VPN в строке состояния означает, что соединение установлено ✅"
    ),
    "android": (
        "🤖 <b>Android</b>\n\n"
        "1. Установите официальный "
        '<a href="https://play.google.com/store/apps/details?id=net.openvpn.openvpn">'
        "OpenVPN Connect</a>. Проверьте, что разработчик — OpenVPN.\n"
        "2. Скачайте <code>.ovpn</code>-файл из бота.\n"
        "3. В приложении выберите <b>Upload File</b> или <b>File</b>.\n"
        "4. Найдите скачанный файл и нажмите <b>OK</b> или <b>Add</b>.\n"
        "5. Нажмите <b>Connect</b> и разрешите создать VPN-подключение.\n\n"
        "Значок ключа или VPN в строке состояния означает, что всё работает ✅\n\n"
        "OpenVPN Connect больше не поддерживает Android 8/8.1; используйте "
        "Android 9 или новее."
    ),
    "linux": (
        "🐧 <b>Linux</b>\n\n"
        "Официальный OpenVPN 3 работает через терминал. "
        '<a href="https://openvpn.net/community-docs/openvpn-client-for-linux.html">'
        "Инструкция по установке для дистрибутивов</a>.\n\n"
        "После установки импортируйте файл:\n"
        '<code>openvpn3 config-import --config "$HOME/Downloads/ИМЯ_ФАЙЛА.ovpn" '
        "--name MyVPN --persistent</code>\n\n"
        "Подключиться:\n"
        "<code>openvpn3 session-start --config MyVPN</code>\n\n"
        "Отключиться:\n"
        "<code>openvpn3 session-manage --config MyVPN --disconnect</code>\n\n"
        "Если вы предпочитаете графический интерфейс, многие дистрибутивы "
        "умеют импортировать <code>.ovpn</code> через настройки сети / "
        "NetworkManager. Названия пунктов зависят от окружения рабочего стола."
    ),
    "tv": (
        "📺 <b>Телевизор, ТВ-приставка или роутер</b>\n\n"
        "Универсальной инструкции для всех моделей нет: устройство должно "
        "поддерживать импорт OpenVPN-профиля <code>.ovpn</code>.\n\n"
        "• <b>Android TV / Google TV:</b> найдите OpenVPN-клиент в Play Store "
        "и импортируйте файл так же, как на Android.\n"
        "• <b>Samsung, LG и другие Smart TV:</b> если OpenVPN-клиента нет, "
        "VPN обычно настраивается на совместимом роутере.\n"
        "• <b>Роутер:</b> найдите в панели управления раздел VPN Client / "
        "OpenVPN и импорт профиля. Перед изменениями сохраните резервную копию "
        "настроек роутера.\n\n"
        "Пришлите модель устройства в поддержку "
        '<a href="https://t.me/andriyshkoy">@andriyshkoy</a> — подскажем '
        "подходящий вариант."
    ),
    "troubleshooting": (
        "🛠 <b>VPN не подключается</b>\n\n"
        "1. Проверьте в <b>«Моих конфигах»</b>, что профиль активен, а баланс "
        "положительный.\n"
        "2. Отключите другие VPN-приложения и повторите подключение.\n"
        "3. Скачайте конфиг заново и повторно импортируйте его.\n"
        "4. Убедитесь, что дата и время на устройстве выставляются "
        "автоматически.\n"
        "5. Переключитесь между Wi-Fi и мобильным интернетом.\n\n"
        "Не помогло? Напишите "
        '<a href="https://t.me/andriyshkoy">@andriyshkoy</a> и укажите '
        "устройство, версию системы и текст ошибки. Сам <code>.ovpn</code>-файл "
        "присылать не нужно."
    ),
}
