# UI

FWRouter UI лежит в `/opt/fwrouter-ui` и является static frontend поверх backend API. UI не должен владеть system/business logic: он только отображает состояние, вызывает `/api/v2/*` и держит легкую view-state логику.

## Файлы

- `/opt/fwrouter-ui/index.html`: единая HTML-страница с `user`, `admin`, `settings` view.
- `/opt/fwrouter-ui/static/css/base.css`: общие типовые блоки и базовые состояния.
- `/opt/fwrouter-ui/static/css/user-view.css`, `admin-view.css`, `settings-view.css`: view-specific стили.
- `/opt/fwrouter-ui/static/css/responsive.css`: только адаптивные правила.
- `/opt/fwrouter-ui/static/js/ui.js`: переключение view и общая select-обвязка.
- `/opt/fwrouter-ui/static/js/user.js`, `admin.js`, `settings.js`: основные controllers view.
- `/opt/fwrouter-ui/static/js/fwrouter-*.js`: feature modules для списков, labels, inventory, journal и server cards.
- `/opt/fwrouter-ui/static/img/user-liquid-bg.png`: единственный текущий background image, подключен из CSS.

## Layout Conventions

- Новый экран или секция сначала собирается из существующих типовых блоков.
- Если блока не хватает, расширяется общий шаблон, а не создается одноразовый стиль.
- Локальные исключения допустимы только когда общий блок реально не подходит.

Типовые блоки:

- `container`: внешняя ширина страницы и базовые отступы.
- `app-shell`, `card`, `panel`: базовые контейнеры для секций и карточек.
- `card__head`, `panel__head`, `actions`: верхний ряд с заголовком и действиями.
- `grid`, `grid--2`, `row`, `form`, `field`: layout primitives.
- `input`, `textarea.input`, `select.input`: стандартные поля.
- `btn`, `btn--primary`, `btn--secondary`, `seg`, `seg__btn`, `pill`: стандартные controls.
- `seg--liquid`, `[data-liquid-seg]`, `liquid-btn__*`, `liquid-seg__lens`: liquid-glass segmented controls.
- `user-layout`, `admin-stage`, `settings-stage`: рабочие зоны view.
- `picklist__*`, `server-table*`, `server-matrix__*`: списки и таблицы серверов.
- `settings-events-*`: журнал событий settings.
- `panel-drawer*`: выдвижная панель.

## Liquid Segment Contract

`liquid-glass.js` оборачивает кнопки с `data-liquid-seg` во внутренние слои `liquid-btn__glass` и `liquid-btn__label`, добавляет активную `liquid-seg__lens` и реагирует на resize/view switching.

Минимальная разметка:

```html
<nav class="seg seg--liquid" data-liquid-seg aria-label="режим интерфейса">
  <button class="seg__btn is-active" type="button">Первый</button>
  <button class="seg__btn" type="button">Второй</button>
  <button class="seg__btn" type="button">Третий</button>
</nav>
```
