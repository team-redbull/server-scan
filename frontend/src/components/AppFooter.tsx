/** Year-less on purpose: a long-lived air-gapped image is not rebuilt in
 * January. */
export function AppFooter() {
  return (
    <footer className="mx-auto max-w-7xl px-8 pt-1 pb-3 text-base text-[var(--text-secondary)]">
      © Tomer Karniol & Roi Blum
    </footer>
  );
}
