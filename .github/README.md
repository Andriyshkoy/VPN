# GitHub Actions CI/CD Setup

## Обзор

Этот проект использует GitHub Actions для автоматической сборки, тестирования и деплоя VPN сервисов. Настроены три основных workflow:

1. **CI Pipeline** (`ci.yml`) - Тестирование и проверка кода
2. **Build and Push** (`build-and-push.yml`) - Сборка и пуш Docker образов
3. **Deploy** (`deploy.yml`) - Деплой в production

## Настройка переменных окружения

### Secrets (чувствительные данные)

В настройках репозитория `Settings > Environments > production` добавьте следующие secrets:

```
REGISTRY_USERNAME          # Ваш username в DockerHub
REGISTRY_PASSWORD          # Ваш пароль/токен в DockerHub
DATABASE_URL              # postgresql+asyncpg://vpn:password@db:5432/vpn
POSTGRES_DB               # vpn
POSTGRES_USER             # vpn  
POSTGRES_PASSWORD         # Pncj0a41nV2RF8@
ENCRYPTION_KEY            # enzRByUskrWIx5OhIPZx5Lul803FqBJ_UFFFVRUybsk=
BOT_TOKEN                 # 7932765077:AAHUaZD27bFszvZA75CbQ3H9quCXe1QaoH0
ADMIN_API_KEY             # 2724c70e-fd2c-4943-8904-a1fd7e66fa58
```

### Variables (публичные данные)

В том же разделе добавьте переменные окружения:

```
PER_CONFIG_COST           # 0.07
BILLING_INTERVAL          # 3600
```

## Структура CI/CD

### 1. CI Pipeline (`.github/workflows/ci.yml`)

Запускается при каждом push и pull request:

- **Тестирование**: Запуск pytest с PostgreSQL
- **Линтинг**: Black, isort, flake8, mypy
- **Безопасность**: Сканирование уязвимостей Trivy
- **Покрытие кода**: Отправка в Codecov

### 2. Build and Push (`.github/workflows/build-and-push.yml`)

Запускается при push в main/master:

- Собирает все Docker образы:
  - `vpn-base` - базовый образ с Python зависимостями
  - `vpn-admin` - админ панель на Sanic
  - `vpn-bot` - Telegram бот
  - `vpn-alembic` - миграции БД
  - `vpn-scripts` - billing daemon
  - `vpn-nginx` - прокси сервер

- Поддерживает multi-arch сборку (amd64, arm64)
- Использует GitHub Actions cache для ускорения
- Автоматический тэгинг по ветке/коммиту

### 3. Deploy (`.github/workflows/deploy.yml`)

Создает production-ready docker-compose конфигурацию:

- Генерирует `docker-compose.prod.yml` с production настройками
- Создает скрипт деплоя `deploy.sh`
- Сохраняет артефакты для скачивания

## Использование

### Автоматический деплой

1. Запушьте изменения в `main` или `master` ветку
2. GitHub Actions автоматически:
   - Запустит тесты
   - Соберет образы
   - Загрузит их в DockerHub
   - Создаст файлы деплоя

### Ручной деплой

Скачайте артефакты из последнего успешного workflow и выполните:

```bash
# Скачать docker-compose.prod.yml и deploy.sh
wget https://github.com/YOUR_USERNAME/VPN/actions/runs/WORKFLOW_RUN_ID/artifacts/deployment-files

# Выполнить деплой
./deploy.sh
```

### Локальная разработка

```bash
# Сборка базового образа
docker build -t vpn-base:latest .

# Запуск всех сервисов
docker-compose up -d

# Просмотр логов
docker-compose logs -f
```

## Docker образы

После успешной сборки образы доступны на DockerHub:

- `YOUR_USERNAME/vpn-base:latest`
- `YOUR_USERNAME/vpn-admin:latest`
- `YOUR_USERNAME/vpn-bot:latest`
- `YOUR_USERNAME/vpn-alembic:latest`
- `YOUR_USERNAME/vpn-scripts:latest`
- `YOUR_USERNAME/vpn-nginx:latest`

## Мониторинг

- **GitHub Actions**: Статус сборок в разделе Actions
- **Security**: Уязвимости в разделе Security > Code scanning
- **Codecov**: Покрытие кода (если настроен)

## Troubleshooting

### Ошибки сборки

1. Проверьте логи в GitHub Actions
2. Убедитесь что все secrets настроены
3. Проверьте синтаксис Dockerfile'ов

### Ошибки деплоя

1. Проверьте доступность Docker registry
2. Убедитесь что образы собрались успешно
3. Проверьте переменные окружения

### Ошибки тестов

1. Проверьте совместимость с PostgreSQL 16
2. Убедитесь что миграции применяются
3. Проверьте тестовые данные

## Дополнительные возможности

### Notifications

Добавьте уведомления в Slack/Discord:

```yaml
- name: Notify Slack
  uses: 8398a7/action-slack@v3
  with:
    status: ${{ job.status }}
    webhook_url: ${{ secrets.SLACK_WEBHOOK }}
```

### Staging environment

Создайте отдельное окружение для тестирования:

```yaml
environment: staging
```

### Rolling updates

Для zero-downtime deployment используйте:

```bash
docker-compose up -d --no-deps --scale service=2 service
docker-compose up -d --no-deps --scale service=1 service
```
