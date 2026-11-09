# Подготовка и проверка руководств

Статус: инструкции подготовлены для репозитория. Ни серверы, ни публичная страница помощи, ни рассылки этими материалами не изменялись.

Проверено 09.11.2026 по официальным первичным источникам:

| Что проверено | Источник |
| --- | --- |
| Версия 5.5.5, дата, точные имена файлов; Windows/Linux x64, Android APK и Android TV | [Релиз](https://github.com/hiddify/hiddify-app/releases/tag/v5.5.5), [API релиза](https://api.github.com/repos/hiddify/hiddify-app/releases/latest) |
| Google Play и Android package | [Google Play](https://play.google.com/store/apps/details?id=app.hiddify.com) |
| App Store ID, название, разработчик, iOS/iPadOS 15.5+ | [App Store](https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532) |
| Минимум macOS 12.0 текущей сборки | [Deployment target 5.5.5](https://github.com/hiddify/hiddify-app/blob/v5.5.5/macos/Runner.xcodeproj/project.pbxproj) |
| Установка, импорт, разрешение VPN, отличие VPN от System proxy | [Установка](https://hiddify.com/app/How-to-install-Hiddify-app/), [использование](https://hiddify.com/app/How-to-use-Hiddify-app/) |
| Точный deeplink с `url` и `name` | [Парсер 5.5.5](https://github.com/hiddify/hiddify-app/blob/v5.5.5/lib/utils/link_parsers.dart), [его тесты](https://github.com/hiddify/hiddify-app/blob/v5.5.5/test/utils/link_parsers_test.dart) |
| Актуальный выбор Service mode | [Inbound UI 5.5.5](https://github.com/hiddify/hiddify-app/blob/v5.5.5/lib/features/settings/overview/sections/inbound_options_page.dart), [quick settings](https://github.com/hiddify/hiddify-app/blob/v5.5.5/lib/core/router/bottom_sheets/widgets/quick_settings_modal.dart) |

## Учтённые расхождения upstream

- Официальный how-to обновлён 06.11.2026, но продолжает ссылаться на интерфейс 4.1.1 и более старые изображения. Использованные исторические снимки 0.6.0 / 2.7.0 dev / 5.0.0 dev подписаны честно. Актуальная версия каталога — 5.5.5; для Linux сделаны собственные снимки этой версии.
- App Store требует iOS 15.5+, хотя общий install guide ещё пишет 15+. Для macOS в текущем исходнике target 12.0, хотя старые таблицы продолжают указывать 10.15.
- Mac-версия из App Store может быть iOS-приложением для M1+, которое Apple помечает «Not verified for macOS». Для desktop-инструкции выбран DMG. В новой версии есть VPN system extension, поэтому старая команда GUI через sudo не объявляется обязательной для каждого Mac.
- RPM встречается в старом README, но отсутствует в assets проверенного релиза. Для обычного iPhone не предлагается jailbreak IPA.
- Наличие приложения в магазине Android TV зависит от конкретного устройства. Физический TV и его QR-сценарий в этой подготовке не проверялись.
- How-to заявляет HTTPS-only для подписок, но в [валидаторе 5.5.5](https://github.com/hiddify/hiddify-app/blob/v5.5.5/lib/utils/validators.dart) принимается HTTP, а в [скачивании профиля](https://github.com/hiddify/hiddify-app/blob/v5.5.5/lib/features/profile/data/profile_parser.dart) проверка запрета HTTP закомментирована. Поэтому HTTPS указан как рекомендация для нашего сервиса; универсальная невозможность HTTP не утверждается. HTTP на каждой ОС отдельно не проверялся.

## Изображения

[assets/manifest.json](assets/manifest.json) хранит исходный URL, дату, платформу, известную версию, SHA256, размер, alt-текст и атрибуцию 17 подготовленных изображений. Файлы изображений и capture receipts сохранены в отдельном локальном preview и не включаются в публичный репозиторий.

Семь официальных исторических иллюстраций в публичных Markdown-файлах подключаются по исходным URL Hiddify или App Store. Десять собственных кадров — восемь Linux 5.5.5 и два браузерных снимка магазинов — доступны только в локальном preview. Подготовленные пиксели не изменялись и не генерировались; приложение Hiddify в проект не включается.

Наличие реального скриншота не означает, что на соответствующей платформе проверено рабочее соединение с NL. Тесты транспорта и диагностика VPN ведутся отдельно. Системные диалоги iOS, физический Mac и TV здесь не запускались; исторические снимки не выдаются за интерфейс 5.5.5.

Попытка получить новый Android-снимок отражена в приватном capture receipt: официальный APK 5.5.5 проверен через `aapt` (package, version code, minSdk, SHA256), но software-эмулятор без KVM за ограниченное время не завершил загрузку Android. Поэтому новых Android-снимков и проверки разрешения VPN не заявляется. Временные эмуляторы, ADB и AVD очищены; существующий SDK и production не изменялись.

## Контракт интеграции

`client-downloads.json` — единый каталог ссылок: `schema_version`, `checked_at`, `client`, `platforms`, `import`, `guides`, `sources`. Ключи платформ: `android`, `ios`, `windows`, `macos`, `linux`, `tv`. У каждой есть `label`, `guide`, `store_url` (может быть `null`), `minimum_os`, `downloads` (массив `label`, `url`, `architecture`) и необязательный `notes`.

Ссылки `releases/latest/download/...` обновляются у upstream и не являются фиксацией бинарника по SHA. Для происхождения сделанных снимков metadata содержит конкретный тег и хеш скачанного бинарника. Каталог нужно повторно сверять при обновлении релиза.

Бот может показывать официальный магазин и краткие шаги, а подробную помощь открывать по настраиваемому базовому URL. Несуществующий публичный домен помощи не задаётся. При включении материалов в web-сборку официальные иллюстрации остаются ссылками на первоисточник. Локальные preview-файлы не добавляются автоматически; секреты пользователя в контент или URL аналитики не попадают.

Для нашего **Hysteria** бот выдаёт JSON (`format: singbox-json`, файл `hiddify-hysteria.json`). Через **«+» → Clipboard** импортируется весь JSON, включая внешние фигурные скобки; **«+» → File** читает тот же JSON из файла. **VLESS + REALITY** остаётся ссылкой `vless://…`. Текстовый профиль не нужно превращать в HTTPS-адрес. Автоматическая ротация между двумя отдельно добавленными профилями не обещается.

Проверка Hiddify 5.5.5 обнаружила, что конвертер короткой ссылки Hysteria не сохраняет pin сертификата. Поэтому для этого клиента выбран JSON с `tls.insecure: false` и `tls.certificate_sha256`, который хранит base64 SHA256 DER-сертификата. Параметры уже формирует сервер; пользователю не нужно вычислять или редактировать их. Проверенный Linux-клиент подключился с правильным pin и отклонил неправильный. Выполнение на физических Android/iOS/TV здесь не заявляется.

Первичные источники закреплённой версии:

- [Clipboard и импорт файла](https://github.com/hiddify/hiddify-app/blob/7804cadac01419f31b1ec7802d9e1be599e66de2/lib/features/profile/add/widgets/fix_btns.dart): JSON разрешён в File picker, прочитанное содержимое передаётся в общий обработчик.
- [Обработчик Clipboard](https://github.com/hiddify/hiddify-app/blob/7804cadac01419f31b1ec7802d9e1be599e66de2/lib/features/profile/notifier/profile_notifier.dart) и [валидация локального профиля](https://github.com/hiddify/hiddify-app/blob/7804cadac01419f31b1ec7802d9e1be599e66de2/lib/features/profile/data/profile_repository.dart).
- [Конвертер ссылок Hysteria](https://github.com/hiddify/ray2sing/blob/ed1a4b12297992de89adf2a7a91ce5ec397d4819/ray2sing/hysteria2.go), [парсер JSON](https://github.com/hiddify/hiddify-core/blob/ad83d7570c06c012ad0589896138e5c1bf299c50/v2/config/parser.go) и [проверка сертификата](https://github.com/hiddify/hiddify-sing-box/blob/1614c5edb52dd44a4cc079201ba1e790fdfad8a0/common/tls/std_client.go).

Для отдельно выданной HTTPS-подписки поддерживается шаблон `hiddify://install-config?url=<URL-encoded subscription>&name=<URL-encoded title>`. Кодировать нужно значения параметров по отдельности. Этот механизм не заменяет описанный импорт индивидуальных JSON/URI-профилей.

Проверка ссылок, происхождения и отсутствия raster-файлов в публикуемом дереве:

```bash
python3 docs/client-guides/verify_guides.py
```

Дополнительная сверка имён бинарников с текущим официальным API релиза:

```bash
python3 docs/client-guides/verify_guides.py --online
```

Дополнительная проверка всех 17 исходных изображений по отдельному локальному preview (путь задаёт оператор, он не публикуется):

```bash
python3 docs/client-guides/verify_guides.py --private-bundle /path/to/client-guides-draft
```
