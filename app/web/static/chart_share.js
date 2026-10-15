/* Save the visible candles and DOM layers as a shareable PNG. */
'use strict';
window.ChartShare = (() => {
  function imageFrom(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('Не удалось создать снимок слоёв.'));
      image.src = src;
    });
  }

  async function capture(chart, overlay, title) {
    await document.fonts.ready;
    const shot = chart.takeScreenshot();
    const width = shot.width, height = shot.height;
    const box = overlay.getBoundingClientRect();
    if (!width || !height || !box.width || !box.height) throw new Error('График ещё не загружен.');
    const clone = overlay.cloneNode(true);
    const originals = [overlay, ...overlay.querySelectorAll('*')];
    const copies = [clone, ...clone.querySelectorAll('*')];
    originals.forEach((element, i) => {
      const styles = getComputedStyle(element);
      copies[i].setAttribute('style', Array.from(styles, name => `${name}:${styles.getPropertyValue(name)};`).join(''));
    });
    Object.assign(clone.style, {position: 'relative', inset: 'auto', width: `${box.width}px`, height: `${box.height}px`, margin: '0'});
    clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${box.width} ${box.height}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(clone)}</foreignObject></svg>`;
    const layers = await imageFrom('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg));
    const scale = width / box.width;
    const header = Math.round(48 * scale);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height + header;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = HTF.chartTheme().background;
    ctx.fillRect(0, 0, width, canvas.height);
    ctx.fillStyle = HTF.chartTheme().text;
    ctx.font = `${14 * scale}px "Segoe UI", sans-serif`;
    ctx.fillText(title, 12 * scale, 20 * scale);
    ctx.font = `${10 * scale}px "Segoe UI", sans-serif`;
    ctx.fillText('STV Market Researcher · ' + new Date().toLocaleString('ru-RU', {timeZone: 'Europe/Moscow'}) + ' МСК', 12 * scale, 37 * scale);
    ctx.drawImage(shot, 0, header);
    ctx.drawImage(layers, 0, header);
    return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Не удалось создать PNG.')), 'image/png'));
  }

  async function publish(blob) {
    const response = await fetch('/api/chart-images', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'image/png'}, body: blob});
    if (!response.ok) {
      let message = 'Не удалось сохранить снимок.';
      try { message = (await response.json()).detail || message; } catch (_) {}
      throw new Error(message);
    }
    return (await response.json()).url;
  }

  function showLink(button, url) {
    let panel = button.parentElement.querySelector('.chart-share-result');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'chart-share-result';
      panel.setAttribute('role', 'status');
      button.after(panel);
    }
    panel.replaceChildren();
    const input = document.createElement('input');
    input.readOnly = true;
    input.value = url;
    input.setAttribute('aria-label', 'Ссылка на картинку');
    input.onclick = () => input.select();
    const open = document.createElement('a');
    open.href = url;
    open.target = '_blank';
    open.rel = 'noopener';
    open.textContent = 'Открыть';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'btn small';
    copy.textContent = 'Копировать';
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(url); copy.textContent = 'Скопировано'; }
      catch (_) { input.focus(); input.select(); copy.textContent = 'Выделено'; }
    };
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn small';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Закрыть ссылку');
    close.onclick = () => panel.remove();
    panel.append(input, open, copy, close);
  }

  function bind(button, getChart) {
    button.onclick = async () => {
      const old = button.innerHTML;
      button.disabled = true;
      button.textContent = 'Создаём…';
      try {
        await document.fonts.ready;
        const {chart, overlay, title, ready} = getChart();
        if (!chart || !ready) throw new Error('Дождитесь загрузки графика.');
        const blob = await capture(chart, overlay, title);
        showLink(button, await publish(blob));
      } catch (error) {
        showLinkError(button, error.message);
      } finally { button.innerHTML = old; button.disabled = false; }
    };
  }

  function showLinkError(button, message) {
    let status = button.parentElement.querySelector('.chart-share-error');
    if (!status) { status = document.createElement('span'); status.className = 'chart-share-error'; status.setAttribute('role', 'alert'); button.after(status); }
    status.textContent = message;
    setTimeout(() => status.remove(), 6000);
  }
  return {bind, capture, publish, showLink};
})();
