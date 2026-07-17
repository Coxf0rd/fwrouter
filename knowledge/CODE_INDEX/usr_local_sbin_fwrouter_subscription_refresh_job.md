# `/usr/local/sbin/fwrouter-subscription-refresh-job`

## Назначение

Systemd wrapper для периодического refresh VPN subscription через backend job API.

## Важные функции

- строит payload `job_type=subscription_refresh_prepare`
- использует lock `subscription_refresh`
- отправляет `POST /api/v2/jobs`
- если API возвращает промежуточный `running`, poll'ит `GET /api/v2/jobs/{job_id}` до финального статуса
- проверяет, что job завершился `success`

## Внешние зависимости

- backend API `http://127.0.0.1:5000/api/v2/jobs`
- `fwrouter-subscription-refresh.service`
- `fwrouter-subscription-refresh.timer`

## Runtime/persistent state

Сам wrapper state не пишет. Изменения делает backend job: subscription/server inventory state.

## Boot persistence relevance

Средняя. Timer поддерживает subscription/server inventory свежим после boot и между ручными refresh.

## Нюансы

- `--dry-run` печатает payload и не вызывает API.
- Ошибка API, финальный non-success job status или timeout ожидания приводит к non-zero exit code для systemd.
- По умолчанию wrapper ждёт финальный статус до 240 секунд. Переопределение: `FWROUTER_SUBSCRIPTION_REFRESH_WAIT_SECONDS`.
