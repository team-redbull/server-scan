/** Copies `text` to the clipboard. `navigator.clipboard` exists only in a
 * secure context (HTTPS or localhost), so a plain-HTTP deployment falls
 * back to a hidden textarea and `execCommand("copy")`. Resolves false
 * rather than throwing, so the caller can show a failure state. */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Permission denied or document unfocused: try the legacy path.
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}
